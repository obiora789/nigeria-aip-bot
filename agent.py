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


def _known_dct(key: str) -> bool:
    """True if `key` is a published ENR_FRA_DCT alias.

    Exact membership against the built index — never a shape test. A collided
    pair (two published routings sharing endpoints, our "#N" suffix) is absent
    from the index by construction, so this returns False for it and the query
    falls through to the existing behaviour rather than picking one."""
    try:
        return resolver.published_entities().get(key) == "ENR_FRA_DCT"
    except Exception:                                  # noqa: BLE001
        return False


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

    # 0b) A VOR IDENT MUST NOT BE SILENTLY TURNED INTO ITS AERODROME.
    #     Measured: "What is the frequency of ABC?" extracts as
    #         icao_code = DNAA, aerodrome_name = None
    #     The model resolved the ident to its aerodrome — against schemas.py's
    #     own instruction never to infer a code from a name — so the ident
    #     never reached resolve(), the scope-ambiguity check never saw it, and
    #     the query was answered from Abuja's AD 2.17 airspace table.
    #
    #     ABC is genuinely both: Abuja's VOR ident AND a navaid published in
    #     ENR 4.1 with its own frequency. Putting the ident back into
    #     aerodrome_name lets resolve() see the collision and ASK, which is the
    #     only honest answer when two readings are equally correct.
    #
    #     The database is only consulted when the raw text actually contains a
    #     known VOR ident, so an ordinary query costs nothing.
    if ex.icao_code in resolver.VOR_IDENTS:
        _want = resolver.VOR_IDENTS[ex.icao_code]
        if re.search(rf"\b{re.escape(_want)}\b", raw or "", re.I):
            try:
                from database import find_aip_scope
                if any(r.get("scope_kind") == "ENR_NAVAID"
                       for r in (find_aip_scope(_want) or [])):
                    ex.aerodrome_name = _want
                    ex.icao_code = None
            except Exception:      # noqa: BLE001 — a lookup outage must not
                pass               # turn a working answer into a failure

    # 1) drop a code the model invented (e.g. 'DNLM' for 'Murtala Muhammed Lagos')
    if ex.icao_code and ex.icao_code not in resolver.VALID_ICAO \
            and ex.icao_code not in resolver.OUT_OF_SCOPE_ICAO:
        ex.icao_code = None
    # 2) adopt a real published code literally present in the message
    if not ex.icao_code:
        found = [c for c in _DN_RE.findall(raw.upper()) if c in resolver.VALID_ICAO]
        if found:
            ex.icao_code = found[0]
    # 2c) A PUBLISHED ENTITY NAMED IN THE QUERY, recognised by MEMBERSHIP.
    #
    #     Fires ONLY when the query produced no subject at all — both fields
    #     empty. Such a query is refused today ("Which aerodrome?"), so this can
    #     only turn a refusal into an answer; anything that already resolves
    #     took an earlier branch and never reaches here. AERODROMES and
    #     VOR_IDENTS are untouched.
    #
    #     Measured: "What is the frequency of MIU?" extracted NOTHING — MIU is
    #     Maiduguri's VOR ident, but the model does not know that — so a query
    #     naming a published navaid was answered "Which aerodrome?".
    #
    #     This REPLACES two shape-based scans: a five-letter waypoint pattern
    #     and a stop-list of English words that pattern wrongly matched
    #     ("WHERE", "TOWER"). Shape cannot separate a 2-4 letter ident from an
    #     ordinary word, so the test is membership in the set the AIP actually
    #     publishes — the same test AERODROMES applies, and the one part of
    #     this system that has never misrouted.
    # AIRSPACE PHRASES ("Abuja TMA", "Kano FIR", "Kano East Sector") ARE
    # CHECKED FIRST AND UNCONDITIONALLY — even when the model already set
    # icao_code/aerodrome_name/intent to something else. Confirmed live via
    # three DISTINCT failure modes on the exact same class of query:
    #
    #   "What is ABUJA TMA?"        -> aerodrome_name="ABUJA" (dropped "TMA")
    #   "KANO EAST SECTOR"          -> aerodrome_name=None (nothing extracted)
    #   "What is KANO EAST SECTOR?" -> intent=aerodrome_fact, icao_code=DNKN
    #                                  (misread as the KANO AERODROME)
    #
    # The third case is the dangerous one: it does not fail, it SUCCEEDS at
    # the wrong answer, because "Kano" also happens to name a real aerodrome.
    # A guard that only fires "if nothing was extracted" (the pattern every
    # other backstop check in this function uses) would never catch it —
    # something WAS extracted, just the wrong thing. So this checks the raw
    # text structurally and, when it finds an unambiguous airspace phrase,
    # OVERRIDES whatever the model produced rather than deferring to it.
    #
    # This is safe to do unconditionally because the phrase shape itself is
    # the evidence: "<PLACE> TMA/FIR/SECTOR" cannot mean anything else in
    # this AIP's vocabulary, so there is no query where overriding could turn
    # a correct classification into a wrong one.
    # The place-name capture is UNANCHORED backward, so a naive \bWORD+\b
    # scan swallowed the question's lead words too: "What is ABUJA TMA?"
    # captured group(1)="What is ABUJA" instead of "ABUJA", and the resulting
    # phrase "WHAT IS ABUJA TMA" was never a key in published_entities().
    # Confirmed live: this was the reason 4 of 7 diagnosed queries still
    # failed on the FIRST version of this check — every one of them used
    # "What is X?" or "Where is X?" phrasing, while bare "X" phrasing already
    # worked, which is the exact signature of an unanchored capture eating
    # the interrogative prefix.
    #
    # Fixed by stripping the interrogative lead first, then anchoring the
    # place-name capture to the START of what remains. This only handles
    # "What/Where is/are X" — a narrower net than ideal, but a MISS here
    # costs nothing (the query falls through to existing behaviour exactly as
    # it did before this check existed) so there is no safety cost to being
    # conservative about which lead phrasings are stripped.
    _stripped = re.sub(r"^(?:where|what)\s+(?:is|are)\s+", "", raw.strip(),
                       flags=re.I)
    _airspace_phrase = re.search(
        r"^([A-Za-z][A-Za-z ]{2,24}?)\s+"
        r"(TMA|FIR|CTR|UIR|(?:EAST|WEST|NORTH|SOUTH)\s+SECTOR|SECTOR)\b",
        _stripped, re.I)
    if _airspace_phrase:
        phrase = re.sub(r"\s+", " ",
                        f"{_airspace_phrase.group(1)} {_airspace_phrase.group(2)}"
                        ).strip().upper()
        known_now = resolver.published_entities()
        if phrase in known_now and known_now[phrase] == "ENR_AIRSPACE":
            ex.intent = "airspace_lookup"
            ex.aerodrome_name = phrase
            ex.icao_code = None

    # 2c-pre0) A PUBLISHED DIRECT ROUTING ("ABC to NANOS") IS A PAIR, AND MUST
    #     BE RECOGNISED BEFORE ANY SINGLE-TOKEN CHECK RUNS.
    #
    #     Both endpoints are themselves published entities, so every
    #     single-token scan in this function — including 2c-pre immediately
    #     below — matches the FIRST endpoint and stops, which is exactly the
    #     "ARDEX to EDUKO -> ENR_POINT/ARDEX" signature across the exhaustive
    #     Part B run. The model does the same thing for the same reason.
    #
    #     Overrides unconditionally, on the pattern the airspace-phrase check
    #     above already establishes: the pair shape IS the evidence, and the
    #     membership test is exact, so a phrase that is not a published
    #     routing simply does not match and nothing changes.
    _dct_hit = None
    for _m in re.finditer(
            r"\b([A-Za-z][A-Za-z0-9/]{1,7})\s*(?:-|\bto\b|\bdct\b|\bdirect\b)\s*"
            r"([A-Za-z][A-Za-z0-9/]{1,7})\b", raw or "", re.I):
        _key = resolver.dct_key(f"{_m.group(1)}-{_m.group(2)}")
        if _known_dct(_key):
            _dct_hit = _key
            break
    if _dct_hit:
        ex.aerodrome_name = _dct_hit
        ex.icao_code = None
        if ex.intent in ("out_of_scope", "general_greeting"):
            ex.intent = "airspace_lookup"

    # 2c-pre) A PUBLISHED ENR ENTITY NAMED VERBATIM IN THE RAW TEXT OVERRIDES
    #     WHATEVER THE MODEL PUT IN THE SUBJECT FIELDS.
    #
    #     Every other membership check in this function fires only when the
    #     model extracted NOTHING. That misses the dangerous case, exactly as
    #     the airspace-phrase check above already documents: the model does not
    #     fail, it SUCCEEDS at the wrong subject by mapping an ident or a
    #     waypoint onto a similar-sounding aerodrome it does know. Measured on
    #     the exhaustive Part B run:
    #
    #       "Where is ABC?"    -> AD/DNAA   (ident expanded to its aerodrome)
    #       "AKLIS"            -> AD/DNAA
    #       "POLTO"            -> AD/DNPO
    #       "GUSUS"            -> refused as Gusau (DNGU)
    #       "Where is JOS?"    -> AD/DNJO
    #
    #     Step 0b already guards the ident case, but only by inspecting
    #     ex.icao_code — when the model writes aerodrome_name="ABUJA" instead
    #     of icao_code="DNAA" the guard never fires, which is why AKW passed
    #     under two phrasings and failed under the third. Reading the RAW TEXT
    #     removes that dependency on which field the model happened to use.
    #
    #     SAFETY: fires only when the token is an exact published entity id
    #     AND is not itself an aerodrome alias. The one deliberate exception is
    #     a VOR ident (ABC, LAG, POT), which IS an aerodrome alias and IS a
    #     published navaid — routing it here is what lets resolve() see the
    #     collision and ASK, instead of silently answering as the aerodrome.
    #     Measured: none of the 214 indexed waypoints collides with an ICAO
    #     code or a city alias, so no aerodrome query can be captured here.
    _entity_hit = False
    _vor_idents = set(resolver.VOR_IDENTS.values())
    _known_raw = {} if _dct_hit else resolver.published_entities()
    for _tok in re.findall(r"\b[A-Za-z](?:[A-Za-z0-9]|/)*[A-Za-z0-9]\b", raw or ""):
        _up = _tok.upper()
        if _up in resolver.VALID_ICAO or _up not in _known_raw:
            continue
        if resolver.match_name(_up) and _up not in _vor_idents:
            continue          # an ordinary aerodrome alias — leave it alone
        ex.aerodrome_name = _up
        ex.icao_code = None
        _entity_hit = True
        break

    if not ex.icao_code and not ex.aerodrome_name:
        known = resolver.published_entities()
        # ROUTE DESIGNATORS WITH A CATEGORY PREFIX ("A/UA604", "H/UH206",
        # "V/UV224") are checked FIRST, as a single combined token, and
        # separately from the bare-word scan below.
        #
        # The bare-word regex uses \b word boundaries, and "/" IS a word
        # boundary to \b — so it splits "A/UA604" into "A" and "UA604" as two
        # independent matches, never producing the combined string
        # "A/UA604" that is the ACTUAL database key. published_entities()
        # correctly contains "A/UA604" (after the matching resolver.py fix),
        # but nothing could ever produce that exact token to look it up with.
        #
        # Confirmed live: roughly a third of all ENR_ROUTE designators use
        # this prefixed form, and every one of them failed for this reason —
        # not because they were unrecognised, but because the token that
        # would have recognised them was never assembled from the raw text.
        prefixed = re.search(r"\b([A-Za-z])/(U?[A-Za-z]\d{1,3}[A-Za-z]?)\b",
                             raw or "")
        if prefixed:
            combined = f"{prefixed.group(1)}/{prefixed.group(2)}".upper()
            if combined in known:
                ex.aerodrome_name = combined
        if not ex.aerodrome_name:
            for tok in re.findall(r"\b[A-Za-z][A-Za-z0-9]{1,5}\b", raw or ""):
                up = tok.upper()
                if up in resolver.VALID_ICAO:
                    continue
                if up in known:
                    ex.aerodrome_name = up
                    break

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

    # A CLAIMED ENR ENTITY IS NOT AN UNDER-EXTRACTED AERODROME QUERY.
    # resolver.match_name() knows aerodromes only, and VOR_IDENTS is loaded
    # into its alias list, so match_name('ABC') -> {DNAA} and match_name('POT')
    # -> {DNPO}. Every step from here down uses that as evidence that a
    # subjectless query really meant one aerodrome, and writes ex.icao_code
    # from it. When 2c-pre has already claimed the subject as a published ENR
    # entity, that write is not a rescue — it is a demotion, and resolve()
    # branch 3 then answers as the aerodrome before the scope-ambiguity check
    # in branch 4 is ever reached.
    #
    # Measured on the exhaustive run: all 17 surviving ENR_NAVAID failures are
    # this, and every one of them is a VOR ident — ABC, JOS, POT, ILR, BEN,
    # ENG, GME, LAG, OSB. 2c-pre set aerodrome_name correctly; step 6c then
    # set icao_code from the same ident's aerodrome and won.
    #
    # The INTENT rescue in 6c still runs (is_known_entity already covers a
    # known entity); only the icao_code write is suppressed.
    _may_adopt_icao = not _entity_hit
    # 5) a per-aerodrome field (TAF/METAR/ATIS/hours) asked AT a named aerodrome is
    #    an aerodrome fact (AD 2.11/2.18), not a national MET/AIS policy question.
    # A RECOGNISED DIRECT ROUTING IS NOT AN AERODROME QUERY. Steps 5-6c below
    # all rescue an under-extracted query via resolver.match_name(raw), which
    # is a whole-word substring match — and a DCT pair CONTAINS an endpoint
    # that is also an aerodrome alias. Measured: match_name("Where is ABC to
    # NANOS?") -> {DNAA} and match_name("ILBAS to LAG") -> {DNMM}, so step 6c
    # would set ex.icao_code=DNAA on a pair that 2c-pre0 had already resolved
    # correctly, and resolve() branch 3 would then answer as the aerodrome
    # before branch 4 ever saw the pair. Every one of these steps exists to
    # give a SUBJECTLESS query a subject; a recognised routing already has
    # one, so they are skipped rather than allowed to overwrite it.
    if _dct_hit:
        return ex

    if ex.intent in ("national_lookup", "out_of_scope") and _AD_FIELD_AT_RE.search(raw):
        cands = resolver.match_name(raw)
        if ex.icao_code in resolver.VALID_ICAO or len(cands) == 1:
            ex.intent = "aerodrome_fact"
            if not ex.icao_code and len(cands) == 1 and _may_adopt_icao:
                ex.icao_code = next(iter(cands))
    # 6) "how many/list runways" for a named aerodrome is runway data, never
    #    out_of_scope or a greeting.
    if _RWY_INV_RE.search(raw):
        cands = resolver.match_name(raw)
        if ex.icao_code in resolver.VALID_ICAO or ex.aerodrome_name or len(cands) == 1:
            if ex.intent in ("out_of_scope", "general_greeting", "national_lookup"):
                ex.intent = "runway_data"
            if not ex.icao_code and len(cands) == 1 and _may_adopt_icao:
                ex.icao_code = next(iter(cands))
    # 6b) approach PROCEDURE requests (holding/letdown/missed) for a named
    #     aerodrome route through the approach-chart flow, so they inherit the
    #     clarification + scoped-or-plate safety instead of raw synthesis over an
    #     unscoped AD 2.22 chunk.
    if _APPROACH_PROC_RE.search(raw):
        cands = resolver.match_name(raw)
        if ex.icao_code in resolver.VALID_ICAO or ex.aerodrome_name or len(cands) == 1:
            ex.intent = "chart_retrieval"
            if not ex.icao_code and len(cands) == 1 and _may_adopt_icao:
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
        # A PUBLISHED ENR ENTITY rescues the query exactly like a matched
        # aerodrome does. Confirmed live: "Where is Kelak?" — KELAK is a real
        # ENR 3.3 route point — was refused as out_of_scope. Step 2c above had
        # ALREADY found it and set ex.aerodrome_name = "KELAK", but that fixes
        # the SUBJECT, not the INTENT: this override only ever checked
        # resolver.match_name(), which knows aerodromes only, so a query naming
        # a real waypoint, navaid, area or route point still fell through
        # refused, even though the entity had already been positively
        # identified two steps earlier in the same function.
        #
        # Still cannot rescue a genuinely out-of-scope query: live/priced
        # queries are caught by _is_genuinely_out_of_scope, and an unpublished
        # name matches neither an aerodrome nor a known ENR entity.
        is_known_entity = (
            ex.aerodrome_name
            and ex.aerodrome_name.upper() in resolver.published_entities())
        if ex.icao_code in resolver.VALID_ICAO or len(cands) == 1 or is_known_entity:
            ex.intent = "aerodrome_fact"
            if not ex.icao_code and len(cands) == 1 and _may_adopt_icao:
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
    "for airspace itself, not an aerodrome's own data. When the query NAMES a "
    "specific FIR/TMA/SECTOR (\"Abuja TMA\", \"Kano FIR\", \"Kano East Sector\"), put "
    "the FULL phrase — place name AND the airspace-type word (TMA/FIR/SECTOR) — "
    "in aerodrome_name VERBATIM. Do NOT drop \"TMA\"/\"FIR\"/\"SECTOR\" as if it were "
    "filler, and do NOT treat the city name alone as identifying the aerodrome — "
    "\"Kano East Sector\" is en-route airspace, never the Kano aerodrome, even "
    "though \"Kano\" also names an aerodrome elsewhere in this AIP.\n"
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
    "infer a code from a name — put the name verbatim in aerodrome_name. For an "
    "airspace_lookup naming a specific FIR/TMA/SECTOR, aerodrome_name is the FULL "
    "airspace phrase (see above), not the aerodrome the place name might otherwise "
    "identify.\n\n"
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
