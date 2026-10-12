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
# in-scope examples fixed 0 of them. So the rescue steps in _apply_corrections
# override a refusal that names a published aerodrome or ENR entity.
#
# What VETOES a rescue is the model's own reasoned judgement, never a word in
# the question. An earlier version vetoed on a regex of time and weather words
# ("current", "today", "latest", "slot", "wind", "visibility", "runway in
# use"...). That is exactly the wrong test: the AIP holds information of
# lasting character, and "what is Lagos tower frequency now" asks for the same
# published frequency as the question without "now". A word list kept every
# such refusal in place. Removed.
#
# A refusal is kept when the model gave a reason that genuinely puts the answer
# outside the AIP: a live source is needed (asks_live below), the price is not
# one GEN 4 publishes, or the place is not Nigerian. A refusal with no reason,
# or "other", has no basis and the rescues may override it.
_REASONED_REFUSALS = frozenset({"charges", "outside_nigeria"})


def _refusal_has_basis(ex) -> bool:
    """True if the extraction gives a reason the AIP cannot answer this."""
    return (asks_live(ex)
            or getattr(ex, "out_of_scope_reason", None) in _REASONED_REFUSALS)


# --- the live-query rule ----------------------------------------------------
# The AIP publishes information of lasting character: what stays true until the
# next amendment. Only a state that changes from day to day — a closure, a
# serviceability state, an active NOTAM, a weather observation, a clearance for
# a particular flight, a slot — belongs to NOTAM, ATIS, METAR, ATC or flow
# management, and Vannie must not answer it from the AIP.
#
# Words do not decide this. "Current Lagos tower frequency", "Kano TA today"
# and "which runway is in use at Lagos now" are published facts (AD 2.18,
# AD 2.17, AD 2.22.2) whatever the wording, and are answered.
#
# The extraction LLM reports the judgement in two places, and either is
# enough: needs_live_source (asked of EVERY question, about the ANSWER rather
# than the wording) and out_of_scope_reason (set when it refuses).
#
# outside_nigeria and charges are refusals but NOT live, so they are not here:
# they do not need the live gate, and outside_nigeria is enforced separately
# below so a foreign charges question cannot be answered from GEN 4.
_LIVE_REASONS = frozenset({"transient_status", "current_weather",
                           "atc_clearance", "planning_or_flow"})


def asks_live(ex) -> bool:
    """True if the extraction says this question asks for live information.

    getattr with defaults throughout: extraction objects built before the
    needs_live_source field existed (old tests, a partial deploy) read as
    not-live, which is exactly the pre-change behaviour rather than a crash."""
    if ex is None:
        return False
    return (getattr(ex, "needs_live_source", None) is True
            or getattr(ex, "out_of_scope_reason", None) in _LIVE_REASONS)


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
    """Corrections first, then the scope rules — in that order, always.

    Every correction below can CHANGE an intent, and seven of them can turn an
    out_of_scope refusal into an in-scope answer (steps 2c-pre0, 3, 5, 6, 6b,
    6c and 7). That is their job: the model over-refuses published data. But
    it also means a correct refusal of a live question could be undone by any
    one of them, and patching each step separately leaves the next new step
    unguarded. Confirmed live: step 5 turned "current METAR for Kano" into an
    aerodrome_fact because it lacked the check step 6c has.

    So the live rule is enforced ONCE, AFTER all corrections, in
    _enforce_scope(). Nothing that runs earlier can undo it, and a correction
    added later is covered without anyone remembering to guard it. The
    corrections also return early in one place (a recognised direct routing),
    which is why this is a wrapper rather than a final step inside them."""
    ex = _apply_corrections(ex, raw)
    return _enforce_scope(ex)


def _enforce_scope(ex):
    """The scope rules the corrections may not override.

    1. LIVE -> out_of_scope. If the model judged the answer needs a live source, or
       gave a live refusal reason, it is refused whatever intent the
       corrections arrived at. The refusal reason is filled in when missing so
       the reply names the right authority rather than falling back to the
       generic string.
    2. A charges question about a FOREIGN airport is not a GEN 4 question.
       Without this, "landing fee at Heathrow" with gen4_charge set would be
       answered from Nigeria's schedule — a confident answer about the wrong
       State.
    3. A published GEN 4 charge is IN scope. If the model set gen4_charge but
       also refused, the refusal is overridden to national_lookup, so the
       measured intent matches what main.py will actually do with it."""
    if ex is None:
        return ex

    if asks_live(ex):
        ex.intent = "out_of_scope"
        # Fill the reason ONLY when the model gave none. A more fundamental
        # reason it did give must survive: "is Accra airport open?" is live AND
        # outside_nigeria, and overwriting that with transient_status would
        # send the pilot to Nigerian NOTAMs about a Ghanaian airport.
        if getattr(ex, "out_of_scope_reason", None) in (None, "other"):
            try:
                ex.out_of_scope_reason = "transient_status"
            except Exception:  # noqa: BLE001 — a frozen/legacy object must not
                pass           # turn a refusal into a crash
        if hasattr(ex, "gen4_charge"):
            ex.gen4_charge = None     # live wins over a charges reading
        return ex

    if getattr(ex, "out_of_scope_reason", None) == "outside_nigeria" \
            and getattr(ex, "gen4_charge", None):
        ex.gen4_charge = None
        return ex

    if getattr(ex, "gen4_charge", None) in ("aerodrome", "air_navigation"):
        ex.intent = "national_lookup"
        if hasattr(ex, "out_of_scope_reason"):
            ex.out_of_scope_reason = None
    return ex


def _apply_corrections(ex: AIPQueryExtraction, raw: str) -> AIPQueryExtraction:
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
        if _up in _vor_idents and not resolver._ident_is_also_navaid(_up):
            # MEMBERSHIP IN published_entities() IS NOT EVIDENCE FOR A VOR IDENT.
            # That dict SEEDS every value of VOR_IDENTS as ENR_NAVAID up front
            # (resolver line 520) so an ident is recognised before the index
            # loads — it is an assumption, not a database read. Taking it as
            # proof claims idents that ENR 4.1 does not actually publish:
            # test_vor_ident_is_not_silently_converted_to_its_aerodrome caught
            # "BEB frequency"/DNBB being rewritten to aerodrome_name='BEB' with
            # the code dropped, turning a working aerodrome answer into a
            # refusal for a navaid that does not exist.
            #
            # _ident_is_also_navaid is the test that was already correct here:
            # it requires BOTH readings to be real — in VOR_IDENTS *and* an
            # indexed ENR_NAVAID — and fails closed on any lookup error. Only
            # a genuine collision gets taken away from the aerodrome.
            #
            # Tokens that are not VOR idents need no such check: their
            # membership came from list_scope_ids(), which is a real read.
            continue
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

    # STEP 5 MUST NOT RESCUE A LIVE QUESTION. This step exists for the static
    # AD 2.11 / AD 2.18 / AD 2.3 fields — "TAF validity at Lagos", "ATIS hours
    # at Kano" — which the model sometimes files as national or refuses. But
    # _AD_FIELD_AT_RE also matches "metar", so "current METAR for Kano" — a
    # live observation, correctly refused — was rewritten to aerodrome_fact and
    # answered. Confirmed in measure_intent.py.
    #
    # The veto is the model's reasoned refusal (_refusal_has_basis), not a word
    # list: "current ATIS frequency at Kano" is published AD 2.18 data and must
    # be rescued, while "current METAR for Kano" carries current_weather.
    # _enforce_scope() repeats the live check after every step regardless.
    if ex.intent in ("national_lookup", "out_of_scope") and _AD_FIELD_AT_RE.search(raw) \
            and not _refusal_has_basis(ex):
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
    #     the query resolves to exactly one Nigerian aerodrome and the model
    #     gave no reason that puts the answer outside the AIP (live source,
    #     unpublished price, foreign place), the refusal has no basis and is
    #     overridden.
    #     Measured: 16 of 198 published fields were refused this way —
    #     "tourist office at Escravos", "Kashimbila health and sanitation",
    #     "direction distance from city at Makurdi" — every one of them static,
    #     Nigerian and published.
    #
    #     This cannot rescue a genuinely out-of-scope query: live, priced and
    #     foreign ones carry a reason (_refusal_has_basis), and a foreign
    #     airport never resolves to a DN aerodrome anyway.
    if ex.intent == "out_of_scope" and not _refusal_has_basis(ex):
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
        # queries carry a reason (_refusal_has_basis), and an unpublished
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
    "airspace_lookup or national_lookup.\n"
    "ONE EXCEPTION: charges and fees. GEN 4 publishes one national schedule "
    "(landing, parking, passenger service, cargo, en-route, terminal "
    "navigation), not a schedule per aerodrome — so a charges question is "
    "national_lookup with gen4_charge set, even when an aerodrome is named "
    "('landing fee at Kano').\n\n"
    "The AIP publishes STATIC aeronautical data. IN SCOPE intents:\n"
    "- frequency_retrieval: any radio frequency at an aerodrome — Tower, Ground, "
    "Approach/Radar, ATIS, Director, Emergency.\n"
    "- runway_data: runway length/width/surface, PCN/PCR, bearings, slope, declared "
    "distances (TORA/TODA/ASDA/LDA).\n"
    "- aerodrome_fact: ANY other aerodrome (AD 2.x) fact — aerodrome elevation, "
    "reference temperature, magnetic variation, taxiway widths, apron/stands, RFFS "
    "(fire) category, fuel/oil types, de-icing, repair/handling facilities, hangar, "
    "hours of operation, customs/immigration, removal of disabled aircraft, "
    "transition altitude, VOR/DME and ILS identifiers and frequencies, the runway "
    "in use (AD 2.22.2 publishes a runway-in-use statement for every aerodrome — "
    "set ad2_subsection 2.22).\n"
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
    "- national_lookup (GEN): nationwide policy — aerodrome and air navigation "
    "charges (GEN 4: also set gen4_charge), MET service policy/TAF validity, SAR "
    "organisation, AIS, the AIP's publishing authority, abbreviations.\n\n"
    "IMPORTANT — these only SOUND out-of-scope but are STATIC AIP data and are IN "
    "SCOPE: 'reference temperature' (AD 2.2, not live weather), 'TAF validity / trend "
    "issuance interval' (MET service policy), aerodrome 'elevation'/'how high', ATIS, "
    "de-icing and repair facilities, taxiway widths, hours of operation, the runway "
    "in use. Casual or "
    "terse phrasing ('abuja twr freq', 'how high is abuja', 'longest rwy lagos') is "
    "still IN SCOPE — classify it normally.\n\n"
    "OUT OF SCOPE is a POSITIVE classification, not a leftover bucket. Before you "
    "may answer out_of_scope, one of these three must be true. Check them "
    "explicitly:\n"
    "  (a) LIVE — the ANSWER is a state that changes from day to day and is "
    "promulgated by NOTAM, ATIS, METAR or a controller, so the AIP cannot hold "
    "it: whether a runway, taxiway, aerodrome, navaid or facility is open, "
    "closed, serviceable, usable or restricted at the moment; active NOTAMs; "
    "a weather observation (METAR, the wind, QNH right now); a clearance or "
    "instruction for a particular flight; a slot, delay or flight status.\n"
    "      WORDS DO NOT MAKE A QUESTION LIVE. The AIP holds information of "
    "lasting character, true until the next amendment. 'Now', 'current', "
    "'currently', 'today', 'tonight', 'at the moment', 'latest' or 'live' "
    "attached to a published fact still asks for the published fact: 'what is "
    "Lagos tower frequency now', 'current ATIS frequency at Kano', 'Abuja "
    "transition altitude today', 'Lagos tower hours today', 'the current "
    "landing fee', 'which AIRAC amendment is this AIP on' — all IN SCOPE. A "
    "question that only names a topic or published field ('fuel spillage at "
    "Yola', 'removal of disabled aircraft at Kano') asks for the AIP entry, "
    "not for an incident happening now — IN SCOPE. The runway in use is IN SCOPE, with or without such a "
    "word: every aerodrome's AD 2.22.2 publishes a runway-in-use statement, "
    "and that statement is the answer. Standing rules are IN SCOPE too: 'ATC "
    "clearance requirements for VFR flights at Lagos' (AD 2.22), flight plan "
    "filing procedures (ENR 1.10).\n"
    "  (b) MONEY — the user wants a price the AIP does NOT publish: the price "
    "of fuel, a handling agent's quote, a ticket. Aerodrome and air navigation "
    "CHARGES ARE published in GEN 4 — landing, parking, passenger service, "
    "en-route, terminal navigation — and are IN SCOPE (national_lookup, set "
    "gen4_charge).\n"
    "  (c) PLACE — the airport is OUTSIDE Nigeria.\n"
    "Any ONE of these being YES means out_of_scope, and (c) is decisive on its "
    "own: if the airport is not Nigerian, answer out_of_scope no matter how "
    "ordinary the question is — 'ILS frequency at Heathrow' and 'runway length "
    "at JFK' are both out_of_scope, because this AIP covers Nigeria only. "
    "Likewise (a): 'current METAR', 'the wind at Abuja' and 'weather now' are "
    "observations taken at this moment, which the AIP does not publish; 'is "
    "runway 18R open right now', 'can I use 18R at Lagos tonight' and 'is "
    "Lagos tower open now' ask the present state of a runway or unit, which "
    "is a NOTAM matter.\n"
    "If all three are NO, you MUST NOT answer out_of_scope, however "
    "un-aeronautical the topic sounds. The AIP publishes a great deal that does "
    "not sound like aviation, and all of it is IN SCOPE: bus and taxi "
    "transportation (AD 2.5), tourist offices, hotels, restaurants and banks "
    "(AD 2.5), health and sanitation (AD 2.3), direction and distance from the "
    "city (AD 2.2), snow/rain clearance priorities and seasonal availability "
    "(AD 2.7), landing-forecast type and issuance interval (AD 2.11), medical "
    "facilities (AD 2.5). Asking WHICH of these a Nigerian aerodrome publishes "
    "is always an aerodrome_fact.\n"
    "Note the difference LIVE makes: 'landing forecast for Zaria' is the "
    "published forecast TYPE (AD 2.11, in scope); 'current METAR for Zaria' is "
    "an observation happening now (out of scope). 'Clearance priorities for "
    "Minna' is AD 2.7 snow/rain clearance (in scope); 'clearance to land at "
    "Minna' is ATC (out of scope).\n\n"
    "needs_live_source: answer it for EVERY question, in scope or not. True "
    "exactly when (a) LIVE above is YES — the answer can only come from a NOTAM, "
    "ATIS, a weather report or a controller. False for every published fact, "
    "whatever words the question uses. Decide on what the answer is, not on a "
    "word in the question.\n\n"
    "When you answer out_of_scope, or set needs_live_source true, you MUST also set "
    "out_of_scope_reason. The "
    "test is PERMANENCE: the AIP publishes information of lasting character, "
    "what stays true until the next "
    "amendment; only a state that changes from day to day is somebody else's "
    "to publish. A runway's dimensions are AIP; whether that runway is open this "
    "afternoon is a NOTAM. Pick the reason the question actually turns on:\n"
    "  transient_status - whether a runway, taxiway, aerodrome, navaid or "
    "facility is open/closed/serviceable/usable/restricted at the moment, works "
    "in progress, active NOTAMs. NOT the runway in use (published, AD 2.22.2).\n"
    "  current_weather - an observed or forecast weather VALUE: a METAR, SPECI "
    "or TAF report, the wind, visibility, ceiling or QNH right now.\n"
    "  atc_clearance - a clearance or instruction for a particular flight, "
    "which only a controller can give.\n"
    "  planning_or_flow - a slot, delay, flight status or schedule for a "
    "particular flight.\n"
    "  charges - a price the AIP does NOT publish (fuel price, handling quotes).\n"
    "  outside_nigeria - an aerodrome or airspace outside Nigeria.\n"
    "  other - out of scope for none of the above reasons.\n"
    "This reports a reason for a decision you have ALREADY made. It does NOT "
    "widen out_of_scope: everything listed as IN SCOPE above stays in scope, "
    "with out_of_scope_reason null.\n\n"
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
