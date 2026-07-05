"""
agent.py — the only place the LLM is used, and only as a parameter extractor.

Two calls per message, both cheap: one structured extraction (gpt-4o-mini) and
one embedding (text-embedding-3-small). The LLM never writes the answer.
"""
import logging
import re
from typing import Optional

from openai import OpenAI

import config
import resolver
from schemas import AIPQueryExtraction

log = logging.getLogger("vannie.agent")
client = OpenAI(api_key=config.OPENAI_API_KEY)

_DN_RE = re.compile(r"\bDN[A-Z]{2}\b")
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
# A genuine greeting/smalltalk — the ONLY thing that should get the canned reply.
_REAL_GREETING_RE = re.compile(
    r"^\s*(hi|hello|hey|yo|howdy|good (morning|afternoon|evening)|greetings|"
    r"thanks?|thank you|help|start|what can you do|who are you)\b[\s!.?]*$", re.I)


def _backstop(ex: AIPQueryExtraction, raw: str) -> AIPQueryExtraction:
    """Deterministic correction of LLM extraction failures a regex handles
    perfectly: (1) a fabricated/invalid ICAO code, (2) a real DN code or a clear
    mapping question wrongly sent to out_of_scope, (3) an obvious chart request
    mislabelled as a text/procedure lookup. Never converts a genuine out-of-scope
    query (live weather, foreign airport) into an answer."""
    resolver.load_index()
    # 1) drop a code the model invented (e.g. 'DNLM' for 'Murtala Muhammed Lagos')
    if ex.icao_code and ex.icao_code not in resolver.VALID_ICAO \
            and ex.icao_code not in resolver.OUT_OF_SCOPE_ICAO:
        ex.icao_code = None
    # 2) adopt a real published code literally present in the message
    if not ex.icao_code:
        found = [c for c in _DN_RE.findall(raw.upper()) if c in resolver.VALID_ICAO]
        if found:
            ex.icao_code = found[0]
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
    "OUT OF SCOPE -> out_of_scope ONLY for: live/real-time info (current weather, "
    "active NOTAMs, today's runway-in-use, ATC clearances, slots), commercial info "
    "the AIP does not publish (fuel PRICES), and any airport OUTSIDE Nigeria. When "
    "unsure but a Nigerian aerodrome or AIP topic is named, DO NOT use out_of_scope.\n\n"
    "ICAO: only set icao_code if the user literally typed a 4-letter 'DN' code; never "
    "infer a code from a name — put the name verbatim in aerodrome_name.\n\n"
    "filter_part is a coarse hint: AD for aerodrome data, ENR for airspace, GEN for "
    "national."
)


def extract_query_parameters(user_text: str) -> Optional[AIPQueryExtraction]:
    """Returns the parsed parameters, or None if extraction fails (caller abstains)."""
    try:
        response = client.beta.chat.completions.parse(
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
        response = client.embeddings.create(input=[cleaned], model=config.EMBEDDING_MODEL)
        return response.data[0].embedding
    except Exception:  # noqa: BLE001
        log.exception("embedding failed")
        return None
