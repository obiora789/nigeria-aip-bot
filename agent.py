"""
agent.py — the only place the LLM is used, and only as a parameter extractor.

Two calls per message, both cheap: one structured extraction (gpt-4o-mini) and
one embedding (text-embedding-3-small). The LLM never writes the answer.
"""
import logging
import re
from typing import Optional

from openai import OpenAI

from retry import retry_call

import config
import resolver
from schemas import AIPQueryExtraction

log = logging.getLogger("vannie.agent")
client = OpenAI(api_key=config.OPENAI_API_KEY)

_DN_RE = re.compile(r"\bDN[A-Z]{2}\b")
# A prohibited/restricted/danger area id — DNP1, DNR9, DND45. Shares the "DN"
# prefix with ICAO codes but is NOT one: the fourth character is P/R/D and it
# ends in digits, which no aerodrome code does. That difference is what makes
# this safe to match — it can never capture a real aerodrome.
_ENR_AREA_RE = re.compile(r"\bDN[PRD]\s?\d{1,3}\b", re.I)
# A clear identity/mapping question — safe to rescue from a wrong out_of_scope.
_MAPPING_RE = re.compile(
    r"(icao code|what (?:city|airport|aerodrome)|what(?:'s| is)\s+dn[a-z]{2})", re.I)
# An unmistakable chart request: an explicit chart/plate noun, OR a display verb
# near a plate type. Catches 'Show the RNAV (GNSS) approach' that the model
# mislabels as a procedure lookup. Intents that are genuinely about VALUES
# (frequency/runway data) are left alone.
_CHART_NOUN_RE = re.compile(r"\b(chart|plate)\b", re.I)
_CHART_REQ_RE = re.compile(
    r"\b(show|display|pull|view|see|bring up)\b[^.?!]{0,40}?"
    r"\b(rnav|gnss|rnp|ils|vor|ndb|sid|star)\b", re.I)
_CHART_FORCEABLE = {"procedure_lookup", "aerodrome_fact", "airspace_lookup",
                    "icao_lookup", "general_query"}
# A per-aerodrome MET/comm/hours field — belongs to AD 2.11/2.18/2.3, not national.
_AD_FIELD_AT_RE = re.compile(
    r"\b(taf|metar|trend|atis|operational hours|hours of operation)\b", re.I)
# "how many / list / configuration" of runways -> a runway-data question, not
# out_of_scope or a greeting.
_RWY_INV_RE = re.compile(
    r"how many runways|list (the )?runways|runway configuration|which runways|"
    r"number of runways|how many rwy", re.I)
# Requests for approach PROCEDURES (holding/letdown/missed approach). These must
# go through the approach-chart flow (clarification + scoped-or-plate), NOT general
# synthesis over an unscoped AD 2.22 chunk — which can splice one approach's holding
# onto another's letdown and assert values that don't match the source.
_APPROACH_PROC_RE = re.compile(
    r"\b(holding|letdown|let[\s-]?down|missed[\s-]*approach|approach procedure)\b", re.I)
# A genuine greeting/smalltalk — the ONLY thing that should get the canned reply.
_REAL_GREETING_RE = re.compile(
    r"^\s*(hi|hello|hey|yo|howdy|good (morning|afternoon|evening)|greetings|"
    r"thanks?|thank you|help|start|what can you do|who are you)\b[\s!.?]*$", re.I)



# --- out_of_scope backstop ---------------------------------------------------
# The prompt already said out_of_scope was ONLY for live/commercial/foreign, and
# the model ignored it: 16 of 198 published fields were refused, and adding
# in-scope examples fixed 0 of them. That is not fixable by more examples,
# because "is this in scope?" is an UNBOUNDED question — 23 subsections times
# every field name the AIP contains, including tourist offices and bus
# services. No list covers it.
#
# "Is this live, priced, or foreign?" is bounded. It is a question about TIME
# and MONEY, not about AIP content, so it can be enumerated and stays
# enumerated as the AIP grows. That asymmetry is the whole reason this guard
# is written this way round.
#
# Deliberately NARROW, because each pattern must not catch its static
# counterpart — the confusions are real, from measured failures:
#     "landing FORECAST for Zaria"      AD 2.11 type/interval   -> in scope
#     "current METAR for Zaria"         observation now         -> out
#     "CLEARANCE PRIORITIES for Minna"  AD 2.7 snow/rain        -> in scope
#     "clearance to land at Minna"      ATC                     -> out
# So "forecast" and bare "clearance" are NOT markers; the time words are.
_LIVE_RE = re.compile(
    # (a) TIME words — the state of something now or in the future.
    r"\b(current(ly)?|right now|at the moment|as of now|this (morning|afternoon|"
    r"evening)|tonight|today'?s?|live|latest|active\s+notams?|\bnotams?\b|"
    r"tomorrow|next\s+(week|month)|this\s+week|\bslots?\b|runway\s+in\s+use)\b|"
    r"\bclearance\s+to\s+(land|take\s?off|taxi|enter)\b|"
    # (b) OBSERVED WEATHER — inherently a reading taken now, whether or not a
    # time word appears. "what is the wind at Abuja" carries no time marker but
    # can only mean the current wind; the AIP publishes no wind values.
    # NOT included: "reference temperature" (AD 2.2), "TAF validity" and
    # "landing forecast" (AD 2.11 policy) — all static, all published, and all
    # previously refused, which is the failure this whole guard exists to stop.
    r"\b(metar|speci)\b|"
    # "temperature" needs a negative lookBEHIND, not lookahead: the AIP's static
    # field is "aerodrome REFERENCE temperature" (AD 2.2), where the qualifier
    # precedes the word. A lookahead cannot see it, and flagged both
    # "aerodrome reference temperature of Kano" and "reference temperature
    # Ilorin" as live weather.
    r"\b(wind|visibility|ceiling|cloud\s?base|qnh|humidity|rainfall)\b|"
    r"(?<!reference\s)(?<!ref\s)\btemperature\b(?!\s*(policy|validity|type|interval))|"
    r"\bweather\b(?!\s*(minima|service|policy))|"
    # (c) NON-AIP ACTIONS — requests to DO something rather than look something
    # up. The AIP is a reference document; it books nothing.
    r"\b(book|reserve|cancel|check\s?in|buy|order)\b[^?]{0,20}\b(flight|ticket|seat|hotel)\b|"
    r"\bmy\s+(flight|booking|ticket)\b", re.I)

# NOTE on money: "fee"/"charge" are deliberately NOT price markers. GEN
# publishes aerodrome charges, so "what is the landing fee at Kano" is a
# legitimate national_lookup, not an out-of-scope question. Only genuinely
# unpublished commercial data (fuel PRICES, what something COSTS) qualifies.
_PRICE_RE = re.compile(
    r"\b(price[sd]?|cost[s]?|how much (does|is|to)|tariff)\b", re.I)


def _is_genuinely_out_of_scope(raw: str) -> bool:
    """True only if the query is about NOW or about MONEY. Foreign airports are
    handled separately by resolver.resolve(), which fails to match them."""
    return bool(_LIVE_RE.search(raw or "") or _PRICE_RE.search(raw or ""))


def _backstop(ex: AIPQueryExtraction, raw: str) -> AIPQueryExtraction:
    """Deterministic correction of LLM extraction failures a regex handles
    perfectly: (1) a fabricated/invalid ICAO code, (2) a real DN code or a clear
    mapping question wrongly sent to out_of_scope, (3) an obvious chart request
    mislabelled as a text/procedure lookup. Never converts a genuine out-of-scope
    query (live weather, foreign airport) into an answer."""
    resolver.load_index()
    # 0) A DN token that is NOT an aerodrome may still be a published ENR
    #    entity: DNP1/DNR9/DND45 are prohibited/restricted/danger areas. The
    #    model fills icao_code for them — correctly, by its own instruction
    #    ("a 4-letter code starting with DN") — and step 1 below then discarded
    #    it as invented, leaving nothing behind. resolve() reached its
    #    "AD-type intent but no aerodrome given" branch and asked
    #    "Which aerodrome?" for a danger area that IS in the index.
    #
    #    Moving it to aerodrome_name is what makes it reachable: that is the
    #    field resolve() inspects, and its ENR lookup is exact — a name either
    #    is a published entity or it is not, so this cannot invent one.
    if ex.icao_code and _ENR_AREA_RE.match(ex.icao_code or ""):
        ex.aerodrome_name = ex.aerodrome_name or ex.icao_code
        ex.icao_code = None

    # 1) drop a code the model invented (e.g. 'DNLM' for 'Murtala Muhammed Lagos')
    if ex.icao_code and ex.icao_code not in resolver.VALID_ICAO \
            and ex.icao_code not in resolver.OUT_OF_SCOPE_ICAO:
        ex.icao_code = None
    # 2) adopt a real published code literally present in the message
    if not ex.icao_code:
        found = [c for c in _DN_RE.findall(raw.upper()) if c in resolver.VALID_ICAO]
        if found:
            ex.icao_code = found[0]
    # 2b) ...or an ENR area id literally present, if nothing else resolved.
    #     "What is DND45?" carries no aerodrome, so without this the query has
    #     no subject at all by the time it reaches resolve().
    if not ex.icao_code and not ex.aerodrome_name:
        # Search the raw text, NOT a space-stripped copy. Collapsing spaces
        # glues the id to the preceding word ("What is DND45?" ->
        # "WHATISDND45?") so the leading \b never matches and the scan finds
        # nothing. The pattern itself tolerates an internal space, which is the
        # case the strip was meant to cover.
        area = _ENR_AREA_RE.search(raw.upper())
        if area:
            ex.aerodrome_name = re.sub(r"\s+", "", area.group(0))
    # 3) explicit identity/mapping question -> ensure it resolves, never refuse
    if _MAPPING_RE.search(raw):
        if ex.intent == "out_of_scope":
            ex.intent = "icao_lookup"
        if not ex.icao_code and not ex.aerodrome_name:
            cands = resolver.match_name(raw)
            if len(cands) == 1:
                ex.icao_code = next(iter(cands))
    # 4) unmistakable chart request mislabelled as text -> force chart_retrieval
    if ex.intent in _CHART_FORCEABLE and (
            _CHART_NOUN_RE.search(raw) or _CHART_REQ_RE.search(raw)):
        ex.intent = "chart_retrieval"
    # 5) a per-aerodrome field (TAF/METAR/ATIS/hours) asked AT a named aerodrome is
    #    an aerodrome fact (AD 2.11/2.18), not a national MET/AIS policy question.
    if ex.intent in ("national_lookup", "out_of_scope") and _AD_FIELD_AT_RE.search(raw):
        cands = resolver.match_name(raw)
        if ex.icao_code in resolver.VALID_ICAO or len(cands) == 1:
            ex.intent = "aerodrome_fact"
            if not ex.icao_code and len(cands) == 1:
                ex.icao_code = next(iter(cands))
    # 6) "how many/list runways" for a named aerodrome is runway data, never
    #    out_of_scope or a greeting.
    if _RWY_INV_RE.search(raw):
        cands = resolver.match_name(raw)
        if ex.icao_code in resolver.VALID_ICAO or ex.aerodrome_name or len(cands) == 1:
            if ex.intent in ("out_of_scope", "general_greeting", "national_lookup"):
                ex.intent = "runway_data"
            if not ex.icao_code and len(cands) == 1:
                ex.icao_code = next(iter(cands))
    # 6b) approach PROCEDURE requests (holding/letdown/missed) for a named
    #     aerodrome route through the approach-chart flow, so they inherit the
    #     clarification + scoped-or-plate safety instead of raw synthesis over an
    #     unscoped AD 2.22 chunk.
    if _APPROACH_PROC_RE.search(raw):
        cands = resolver.match_name(raw)
        if ex.icao_code in resolver.VALID_ICAO or ex.aerodrome_name or len(cands) == 1:
            ex.intent = "chart_retrieval"
            if not ex.icao_code and len(cands) == 1:
                ex.icao_code = next(iter(cands))
    # 6c) A REFUSAL needs positive evidence. If the model said out_of_scope but
    #     the query resolves to exactly one Nigerian aerodrome and contains no
    #     time or money marker, the refusal has no basis and is overridden.
    #     Measured: 16 of 198 published fields were refused this way —
    #     "tourist office at Escravos", "Kashimbila health and sanitation",
    #     "direction distance from city at Makurdi" — every one of them static,
    #     Nigerian and published.
    #
    #     This cannot rescue a genuinely out-of-scope query: live and priced
    #     ones are caught by _is_genuinely_out_of_scope, and a foreign airport
    #     never resolves to a DN aerodrome, so the condition below fails.
    if ex.intent == "out_of_scope" and not _is_genuinely_out_of_scope(raw):
        cands = resolver.match_name(raw)
        if ex.icao_code in resolver.VALID_ICAO or len(cands) == 1:
            ex.intent = "aerodrome_fact"
            if not ex.icao_code and len(cands) == 1:
                ex.icao_code = next(iter(cands))

    # 7) the model sometimes tags a follow-up ("can you list them?") as a greeting.
    #    Only a REAL greeting gets the canned reply; otherwise treat it as a normal
    #    query so conversation-context carry can resolve it.
    if ex.intent == "general_greeting" and not _REAL_GREETING_RE.match(raw.strip()):
        ex.intent = "aerodrome_fact"
    return ex


_SYSTEM = (
    "You are a parameter-extraction engine for the 2026 Nigerian AIP. Extract the "
    "schema fields from the user's message. You do NOT answer questions and you do "
    "NOT use outside knowledge.\n\n"
    "GOLDEN RULE: if a specific Nigerian aerodrome is named (by city, airport name, "
    "or DN code), the question is almost certainly about that aerodrome's published "
    "data — choose an AERODROME intent and put the place in aerodrome_name (or "
    "icao_code if a DN code was typed). Do NOT route an aerodrome's own data to "
    "airspace_lookup or national_lookup.\n\n"
    "The AIP publishes STATIC aeronautical data. IN SCOPE intents:\n"
    "- frequency_retrieval: any radio frequency at an aerodrome — Tower, Ground, "
    "Approach/Radar, ATIS, Director, Emergency.\n"
    "- runway_data: runway length/width/surface, PCN/PCR, bearings, slope, declared "
    "distances (TORA/TODA/ASDA/LDA).\n"
    "- aerodrome_fact: ANY other aerodrome (AD 2.x) fact — aerodrome elevation, "
    "reference temperature, magnetic variation, taxiway widths, apron/stands, RFFS "
    "(fire) category, fuel/oil types, de-icing, repair/handling facilities, hangar, "
    "hours of operation, customs/immigration, removal of disabled aircraft, "
    "transition altitude, VOR/DME and ILS identifiers and frequencies.\n"
    "- procedure_lookup: SID/STAR/approach/missed-approach procedure text, minima.\n"
    "- chart_retrieval: the user wants to SEE a chart/plate. Covers ILS, RNAV, "
    "RNAV(GNSS), RNP, VOR, NDB, SID/departure, STAR/arrival, aerodrome, "
    "parking/docking/stand, obstacle, terrain, area, and en-route charts. Verbs like "
    "show/pull/display/'plate for'/'chart for' signal this, as does naming a plate "
    "type even without a verb ('the RNAV (GNSS) approach for Kano').\n"
    "- icao_lookup: a pure name<->code mapping, e.g. 'what city is DNAA?', 'what is "
    "DNBC?', 'ICAO code for Port Harcourt?'. Fill icao_code or aerodrome_name.\n"
    "- airspace_lookup (ENR): FIR/UIR/TMA/CTR limits, airways/routes, waypoints, "
    "prohibited/restricted/danger areas, en-route navaids, cruising levels. Use ONLY "
    "for airspace itself, not an aerodrome's own data.\n"
    "- national_lookup (GEN): nationwide policy — aerodrome charges, MET service "
    "policy/TAF validity, SAR organisation, AIS, the AIP's publishing authority, "
    "abbreviations.\n\n"
    "IMPORTANT — these only SOUND out-of-scope but are STATIC AIP data and are IN "
    "SCOPE: 'reference temperature' (AD 2.2, not live weather), 'TAF validity / trend "
    "issuance interval' (MET service policy), aerodrome 'elevation'/'how high', ATIS, "
    "de-icing and repair facilities, taxiway widths, hours of operation. Casual or "
    "terse phrasing ('abuja twr freq', 'how high is abuja', 'longest rwy lagos') is "
    "still IN SCOPE — classify it normally.\n\n"
    "OUT OF SCOPE is a POSITIVE classification, not a leftover bucket. Before you "
    "may answer out_of_scope, one of these three must be true. Check them "
    "explicitly:\n"
    "  (a) TIME — the user wants the state of something RIGHT NOW: current "
    "weather/wind/METAR observation, active NOTAMs, the runway in use today, an "
    "ATC clearance, a slot, a flight's status.\n"
    "  (b) MONEY — the user wants a price: fuel price, what something costs, how "
    "much to land.\n"
    "  (c) PLACE — the airport is OUTSIDE Nigeria.\n"
    "Any ONE of these being YES means out_of_scope, and (c) is decisive on its "
    "own: if the airport is not Nigerian, answer out_of_scope no matter how "
    "ordinary the question is — 'ILS frequency at Heathrow' and 'runway length "
    "at JFK' are both out_of_scope, because this AIP covers Nigeria only. "
    "Likewise (a): 'current METAR', 'the wind at Abuja' and 'weather now' are "
    "observations taken at this moment, which the AIP does not publish.\n"
    "If all three are NO, you MUST NOT answer out_of_scope, however "
    "un-aeronautical the topic sounds. The AIP publishes a great deal that does "
    "not sound like aviation, and all of it is IN SCOPE: bus and taxi "
    "transportation (AD 2.5), tourist offices, hotels, restaurants and banks "
    "(AD 2.5), health and sanitation (AD 2.3), direction and distance from the "
    "city (AD 2.2), snow/rain clearance priorities and seasonal availability "
    "(AD 2.7), landing-forecast type and issuance interval (AD 2.11), medical "
    "facilities (AD 2.5). Asking WHICH of these a Nigerian aerodrome publishes "
    "is always an aerodrome_fact.\n"
    "Note the difference TIME makes: 'landing forecast for Zaria' is the "
    "published forecast TYPE (AD 2.11, in scope); 'current METAR for Zaria' is "
    "an observation happening now (out of scope). 'Clearance priorities for "
    "Minna' is AD 2.7 snow/rain clearance (in scope); 'clearance to land at "
    "Minna' is ATC (out of scope).\n\n"
    "ICAO: only set icao_code if the user literally typed a 4-letter 'DN' code; never "
    "infer a code from a name — put the name verbatim in aerodrome_name.\n\n"
    "filter_part is a coarse hint: AD for aerodrome data, ENR for airspace, GEN for "
    "national."
)


def extract_query_parameters(user_text: str) -> Optional[AIPQueryExtraction]:
    """Returns the parsed parameters, or None if extraction fails (caller abstains)."""
    try:
        response = retry_call(
            client.beta.chat.completions.parse,
            model=config.EXTRACTION_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user_text[:2000]},
            ],
            response_format=AIPQueryExtraction,
            temperature=0.0,
        )
        return _backstop(response.choices[0].message.parsed, user_text)
    except Exception:  # noqa: BLE001 — never let an LLM/API hiccup crash the request
        log.exception("extraction failed")
        return None


def get_embedding(text: str) -> Optional[list]:
    """Embeds the full query. Returns None on failure (caller abstains)."""
    cleaned = text.strip().replace("\n", " ")
    if not cleaned:
        return None
    try:
        response = retry_call(client.embeddings.create,
                              input=[cleaned], model=config.EMBEDDING_MODEL)
        return response.data[0].embedding
    except Exception:  # noqa: BLE001
        log.exception("embedding failed")
        return None
