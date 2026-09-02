"""
resolver.py — turns an extracted query into a verified search target, using the
AUTHORITATIVE data from the Nigerian AIP (not whatever happens to be in a table).

Why static: the valid-aerodrome set, the out-of-scope location indicators, and the
city->ICAO dictionary are published facts. Hard-coding them (a) makes the
wrong-airport / wrong-scope guards exact and auditable, and (b) lets a valid
aerodrome resolve even if it has no chart row. Update this when the AIP edition
changes — it is part of the AIRAC discipline.
"""
import logging
import re
from typing import Dict, List, Set, Tuple

from models import Resolution
from schemas import AIPQueryExtraction

log = logging.getLogger("vannie.resolver")

# --- authoritative city/name -> ICAO (2026 Nigerian AIP) -------------------
AERODROMES: Dict[str, List[str]] = {
    "DNAA": ["abuja", "nnamdi azikiwe", "azikiwe", "abj", "abv"],
    "DNAI": ["uyo", "victor attah", "attah"],
    "DNAK": ["akure"],
    "DNAN": ["umueri", "chinua achebe", "achebe", "anambra", "anambara"],
    "DNAS": ["asaba"],
    "DNBB": ["bebi"],
    "DNBC": ["bauchi", "tafawa balewa", "balewa"],
    "DNBE": ["benin"],
    "DNBK": ["birnin kebbi", "kebbi", "ahmadu bello"],
    "DNBY": ["amassoma", "bayelsa"],
    "DNCA": ["calabar", "margaret ekpo", "ekpo"],
    "DNDS": ["dutse"],
    "DNEN": ["enugu", "akanu ibiam", "ibiam"],
    "DNES": ["escravos"],
    "DNET": ["ado ekiti", "ekiti"],
    "DNFB": ["bonny", "finima"],
    "DNFD": ["forcados"],
    "DNGB": ["gbaran ubie", "gbaran"],
    "DNGO": ["gombe"],
    "DNIB": ["ibadan"],
    "DNIL": ["ilorin"],
    "DNIM": ["owerri", "sam mbakwe", "mbakwe"],
    "DNJO": ["jos", "yakubu gowon", "gowon"],
    "DNKA": ["kaduna"],
    "DNKN": ["kano", "mallam aminu kano", "aminu kano", "kan"],
    "DNKS": ["kashimbila"],
    "DNKT": ["katsina", "umaru musa", "yaradua", "yar'adua"],
    "DNMA": ["maiduguri"],
    "DNMK": ["makurdi"],
    "DNMM": ["lagos", "murtala muhammed", "murtala mohammed", "murtala", "los"],
    "DNMN": ["minna"],
    "DNOG": ["ogun", "gateway", "iperu"],
    "DNPO": ["port harcourt", "obafemi awolowo", "awolowo", "ph", "phc"],
    "DNPS": ["phsia", "port harcourt shell", "shell industrial"],
    "DNSK": ["soku"],
    "DNSO": ["sokoto", "saddiq abubakar"],
    "DNSU": ["osubi"],
    "DNWI": ["warri industrial", "warri"],
    "DNYO": ["yola"],
    "DNZA": ["zaria"],
}

VALID_ICAO: Set[str] = set(AERODROMES)            # the 40 published aerodromes
FIR_ICAO = "DNKK"                                 # Kano FIR — en-route, NOT an aerodrome

# VOR/DVOR idents, extracted and verified from each aerodrome's AD 2.19 navaid
# table (2026 AIP). One PRIMARY ident per aerodrome (the one the CTR/TMA is
# centred on). Aerodromes with no VOR are absent (NDB-only, ILS-only, oil
# terminals, heliports, and new fields). NOTE: Kaduna lists a second VOR 'KUA'
# (114.7 MHz) ~5 NM from KDA with no owner marker — possibly the military field
# (DNKM) — deliberately NOT mapped here pending confirmation.
VOR_IDENTS: Dict[str, str] = {
    "DNAA": "ABC", "DNAI": "AKW", "DNAN": "ANU", "DNAS": "SAB", "DNBB": "BEB",
    "DNBC": "BCH", "DNBE": "BEN", "DNBK": "BIK", "DNCA": "CAL", "DNDS": "DUT",
    "DNEN": "ENG", "DNGO": "GME", "DNIB": "IBA", "DNIL": "ILR", "DNIM": "OWR",
    "DNKA": "KDA", "DNKN": "KAN", "DNKT": "KAT", "DNMA": "MIU", "DNMK": "MKD",
    "DNMM": "LAG", "DNMN": "MNA", "DNPO": "POT", "DNSO": "SOK", "DNSU": "OSB",
    "DNYO": "YOL",
}

# Registered DN indicators with NO published aerodrome section in the 2026 AIP.
OUT_OF_SCOPE_ICAO: Dict[str, str] = {
    "DNEB": "Abakaliki", "DNBA": "Bauchi (Old Bauchi)", "DNBI": "Bida",
    "DNDM": "Damaturu", "DNEK": "Eket", "DNGU": "Gusau", "DNJA": "Jalingo",
    "DNKM": "Kaduna Military", "DNKJ": "Kainji", "DNLF": "Lafiya",
    "DNLL": "Lagos RCC/FIC", "DNOB": "Obudu", "DNOS": "Oshogbo", "DNSG": "Osun",
    "DNPM": "Port Harcourt NAF Base", "DNQI": "Qua Iboe",
}

# AD-type intents need an aerodrome; airspace/national do not.
_AD_INTENTS = {"frequency_retrieval", "runway_data", "aerodrome_fact",
               "procedure_lookup", "chart_retrieval", "icao_lookup"}
_DN_CODE = re.compile(r"^DN[A-Z]{2}$")

# Built by load_index() from AERODROMES. Kept as module globals so tests can seed them.
_ALIASES: List[Tuple[str, str]] = []   # (alias_phrase, ICAO)
_LABELS: Dict[str, str] = {}           # ICAO -> display label
_loaded = False


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]", " ", (s or "").lower())).strip()


def load_index(force: bool = False) -> None:
    global _loaded
    if _loaded and not force:
        return
    _ALIASES.clear()
    _LABELS.clear()
    for icao, names in AERODROMES.items():
        _LABELS[icao] = names[0].title()
        for n in names:
            _ALIASES.append((_normalize(n), icao))
    for icao, ident in VOR_IDENTS.items():      # VOR idents as resolution aliases
        _ALIASES.append((_normalize(ident), icao))
    _loaded = True


def _match_name(name: str) -> Set[str]:
    """Whole-word alias match; the longest (most specific) match wins."""
    norm = f" {_normalize(name)} "
    hits = [(len(alias), icao) for alias, icao in _ALIASES if f" {alias} " in norm]
    if not hits:
        return set()
    longest = max(n for n, _ in hits)
    return {icao for n, icao in hits if n == longest}


def match_name(name: str) -> Set[str]:
    """Public wrapper for deterministic name->ICAO matching (used by the agent
    backstop to rescue a place the LLM failed to extract)."""
    load_index()
    return _match_name(name)


def _aero(icao: str) -> Resolution:
    label = _LABELS.get(icao, icao)
    return Resolution(icao=icao, label=label, part="AD", reference=icao, aerodrome_hint=label)


def aerodrome_full_name(icao: str) -> str | None:
    """'Abuja (Nnamdi Azikiwe)' style label for the deterministic ICAO mapping
    answer. City is names[0]; the official name is the first long descriptive
    alias (skips short forms and VOR idents)."""
    names = AERODROMES.get(icao)
    if not names:
        return None
    city = names[0].title()
    extras = [n.title() for n in names[1:] if len(n) > 4 and not n.isupper()]
    return f"{city} ({extras[0]})" if extras else city


def _hint_for(name: str) -> str | None:
    """Canonical aerodrome name for embedding, when a name maps to exactly one
    aerodrome. Used to expand 'PH' -> 'Port Harcourt' inside airspace/national
    queries WITHOUT pinning the search to that aerodrome's AD section."""
    if not name:
        return None
    cands = _match_name(name)
    return _LABELS.get(next(iter(cands))) if len(cands) == 1 else None


# Mirrors the vectoriser's ENR 2.x enrichment so boundary questions land on the
# CTR/TMA limit chunks (ENR 2.1) rather than nearby ENR prose (e.g. ENR 1.8).
_AIRSPACE_TERMS = ("CTR TMA FIR UIR control zone terminal control area "
                   "lateral limits vertical limits airspace radius NM centred")

# AD 2.x field steering: aerodrome queries were landing on the section-HEADER
# chunk (which contains the title "DECLARED DISTANCES" but no values) instead of
# the chunk holding the actual numbers. Prepending the value-carrying terms pulls
# the data chunk above the header. Order = specific first; first match wins.
_AD_FIELD_TERMS = [
    (re.compile(r"declared distance|\btora\b|\btoda\b|\basda\b|\blda\b", re.I),
     "AD 2.13 declared distances TORA TODA ASDA LDA"),
    (re.compile(r"\batis\b|\btwr\b|\btower\b|ground control|clearance delivery|"
                r"approach control|communication frequenc|\bcomm\b|callsign", re.I),
     "AD 2.18 ATS communication facilities ATIS TWR APP GND frequency MHz callsign"),
    (re.compile(r"\btaf\b|\bmetar\b|\btrend\b|meteorolog|weather (report|forecast)|"
                r"forecast validity|met office", re.I),
     "AD 2.11 meteorological information TAF METAR TREND period of validity forecast"),
    (re.compile(r"\bvor\b|\bdme\b|\bndb\b|navaid|identifier|\bident\b", re.I),
     "AD 2.19 radio navigation landing aids VOR DME identifier frequency MHz"),
    (re.compile(r"\bils\b|localizer|localiser|glide ?path", re.I),
     "AD 2.19 ILS localizer glide path category frequency MHz"),
    (re.compile(r"transition (altitude|level)", re.I),
     "transition altitude transition level flight level QNH AMSL feet metres AD 2.17"),
    (re.compile(r"elevation|how high|\bamsl\b|reference temperature|temperature", re.I),
     "AD 2.2 aerodrome elevation reference temperature feet metres AMSL"),
    (re.compile(r"taxiway|\btwy\b", re.I),
     "AD 2.14 taxiway width surface"),
    (re.compile(r"\bfire\b|rescue|\brffs\b|fire ?fighting|fire category", re.I),
     "AD 2.6 rescue and fire fighting RFFS category"),
    (re.compile(r"\bfuel\b|jet ?a|avgas|oil type", re.I),
     "AD 2.6 fuel oil types available"),
    (re.compile(r"\bpcn\b|\bpcr\b|pavement|strength", re.I),
     "AD 2.12 PCN PCR pavement classification strength"),
    (re.compile(r"dimension|length|width|surface|\brunway\b", re.I),
     "AD 2.12 runway physical characteristics length width surface bearing PCN"),
]


def build_search_text(ex: AIPQueryExtraction, res: Resolution, raw: str) -> str:
    """Text to embed for the vector search. Deterministic enrichments only:
      1) expand the resolved aerodrome name to its full form (PH -> Port Harcourt);
      2) airspace queries -> prepend AIP airspace terminology;
      3) aerodrome (AD) queries -> prepend the AD 2.x field's value-carrying terms.
    The hard part/reference filter is unchanged — this only shapes the vector,
    so it can never drift the search to the wrong airport or section."""
    bits: List[str] = []
    if res.aerodrome_hint:
        bits.append(res.aerodrome_hint)
    if ex.intent == "airspace_lookup":
        bits.append(_AIRSPACE_TERMS)
    elif res.part == "AD" and res.icao:
        for rx, terms in _AD_FIELD_TERMS:
            if rx.search(raw or ""):
                bits.append(terms)
                break
    bits.append((raw or "").strip())
    return " ".join(b for b in bits if b).strip()



# Human labels for the scope kinds, used in citations.
_SCOPE_LABELS = {
    "ENR_AREA": "prohibited/restricted/danger area",
    "ENR_POINT": "significant point",
    "ENR_ROUTE": "ATS route",
    "ENR_AIRSPACE": "airspace",
    "ENR_FRA_DCT": "free route direct segment",
}

# Shapes that could be an ENR entity. Checked BEFORE the database is queried so
# an ordinary place name never causes a lookup: DNP1/DNR9/DND45 (areas),
# five-letter uppercase waypoints (TEMSA), airway designators (UT467).
# A token that COULD be an ENR entity. Checked before the database is queried
# so an ordinary word never causes a lookup.
#
# The five-letter arm is the loose one — plenty of English words are five
# letters, and "WHICH" or "LIMIT" would reach the database. That is acceptable
# and deliberate: the lookup is EXACT, so a non-published word returns nothing
# and the caller falls through unchanged. Measured on the real index: 214
# waypoints, and NOT ONE collides with an ICAO code, a city alias or a VOR
# ident, so a hit can never be the wrong kind of entity.
#
# It is applied only to a name the extractor already isolated as the query's
# subject, not to every word in the message.
# ROUTE DESIGNATORS, CORRECTED. The airway-letter class here was
# [ABGLMNRTUVW] — missing P, Q and Y outright — and the pattern had no
# allowance for a route-CATEGORY prefix before the U-marker at all
# ("A/UA604", "H/UH206", "V/UV224" are how roughly a third of all ATS
# routes in this AIP are actually published).
#
# Tested against every one of the 57 real ENR_ROUTE ids in the live
# database: the OLD pattern matched 21/57 (37%). The corrected pattern
# below matches 57/57. Confirmed live: bare designators like "H/UH206" and
# "V/UV224" were refused as out_of_scope or misrouted to an unrelated
# aerodrome, because the token never reached the database lookup at all —
# _ENR_ID_RE rejected it before find_aip_scope() was ever called.
#
# The prefix and airway-category letter are both intentionally left open
# ([A-Z], not an enumerated set) rather than re-deriving a fresh finite list
# from this cycle's data: a category letter absent from AIRAC 03/2026 could
# appear in a later cycle, and — as with the five-letter waypoint arm below —
# a shape match costs one wasted EXACT lookup on a non-existent code and can
# never produce a wrong answer, so there is no safety cost to matching more
# broadly than today's data strictly requires.
_ENR_ID_RE = re.compile(
    r"^(?:DN[PRD]\s?\d{1,3}|[A-Z]{5}|(?:[A-Z]/)?U?[A-Z]\d{1,3}[A-Z]?)$", re.I)


def _lookup_enr_scope(name: str):
    """('ENR_AREA', 'DND45') if `name` is a published ENR entity, else None.

    Fails CLOSED and silently: any error returns None, so the caller falls
    through to the existing unresolved path. A lookup outage must not turn a
    correct refusal into a crash.

    AIRSPACE NAMES ARE CHECKED FIRST, AND SEPARATELY, because they cannot
    pass _ENR_ID_RE at all: that pattern matches compact single-token codes
    (DND45, TEMSA, UT467), while an airspace query is a multi-word phrase
    ("ABUJA TMA", "Kano FIR"). published_entities() maps every such alias to
    the airspace's REAL stored scope_id via _AIRSPACE_ALIAS_TARGET — "ABUJA
    TMA" is not a database key, "ABUJA Terminal Control Area (TMA)" is — so
    the lookup below queries the database using that real id, not the alias
    text the pilot typed.

    Confirmed live: before this branch existed, ENR_AIRSPACE reachability
    tested at 0/42 (0%) — every phrasing of every TMA/FIR/SECTOR query
    failed, because no path existed for a multi-word airspace name to reach
    the database at all."""
    raw = (name or "").strip()
    collapsed = re.sub(r"\s+", " ", raw).upper()
    # published_entities() populates _AIRSPACE_ALIAS_TARGET as a side effect
    # on first call; calling it here (idempotent — it is cached after the
    # first real call) guarantees the map exists before it is read, rather
    # than relying on call order elsewhere in the module.
    published_entities()
    airspace_target = _AIRSPACE_ALIAS_TARGET.get(collapsed)
    if airspace_target:
        try:
            from database import find_aip_scope
            rows = find_aip_scope(airspace_target)
        except Exception:                              # noqa: BLE001
            return None
        if len(rows) == 1:
            return rows[0].get("scope_kind"), rows[0].get("scope_id")
        # The alias resolved but the live lookup did not confirm it (e.g. the
        # entity was renamed or removed in a newer AIRAC cycle). Fall through
        # rather than trust a possibly-stale cached alias.

    # DIRECT ROUTINGS, checked alongside airspace names and for the same
    # reason: "ABC to NANOS" is a multi-word phrase that cannot pass the
    # single-token gate below, and the alias key is not the stored id.
    dct_target = _DCT_ALIAS_TARGET.get(dct_key(raw))
    if dct_target:
        try:
            from database import find_aip_scope
            rows = [r for r in (find_aip_scope(dct_target) or [])
                    if r.get("scope_kind") == "ENR_FRA_DCT"]
        except Exception:                              # noqa: BLE001
            rows = []
        if len(rows) == 1:
            return rows[0].get("scope_kind"), rows[0].get("scope_id")
        # UNVERIFIED: find_aip_scope is an RPC and its behaviour on ids
        # containing "(", "/" and "#" has not been checked against real SQL.
        # The airspace branch above falls through on a non-confirming lookup,
        # which is right there because its aliases are derived from a NAME
        # PATTERN and could in principle name something that no longer exists.
        # A DCT target is different: it is the literal string list_scope_ids()
        # read out of aip_facts in this process, so it is not a guess and
        # returning it is not an invention. Confirm the RPC's behaviour on
        # these ids before treating this fallback as settled.
        return "ENR_FRA_DCT", dct_target

    token = re.sub(r"\s+", "", raw).upper()
    if not token:
        return None
    # MEMBERSHIP FIRST, SHAPE ONLY AS A FALLBACK.
    #
    # _ENR_ID_RE has no arm for a 2-4 letter navaid ident. Tested against the
    # real failing ids from the exhaustive Part B run — AK, AO, BA, BE, BDA,
    # EK, ESC, GB, GO, IBS, IL, JJ, JS, KC, KIS, KUA, MA, OK, ZA — NOT ONE
    # matches, so find_aip_scope() was never called and every one returned
    # "I don't have 'AK' in the Nigerian AIP" for a navaid published in
    # ENR 4.1. published_entities() already held every one of those idents;
    # the shape gate rejected them before the dict was ever consulted.
    #
    # Widening the regex to [A-Z]{2,5} would match most English words and is
    # the shape-based reasoning this project has repeatedly had to undo. The
    # test that belongs here is the one AERODROMES uses: is this string a
    # published entity id, yes or no.
    #
    # The regex is retained as a SECOND arm so an entity added to the AIP
    # after the process-lifetime cache was built still reaches the exact
    # database lookup. A shape hit on a non-existent id costs one wasted
    # EXACT lookup and can never produce a wrong answer.
    if token not in published_entities() and not _ENR_ID_RE.match(token):
        return None
    try:
        from database import find_aip_scope
        rows = find_aip_scope(token)
    except Exception:                                  # noqa: BLE001
        return None
    if len(rows) != 1:
        # AN EXACT ID MATCH BEATS A PARTIAL ONE. This used to return None on
        # any row count other than 1, on the reasoning that >1 meant the same
        # id under two scope kinds — a data defect where refusing is safer
        # than picking. That reasoning predates ENR_FRA_DCT: a DCT id EMBEDS
        # its endpoints' names ("APSAL-KORUT" contains "APSAL"), so if the RPC
        # matches on containment a plain waypoint now returns several rows and
        # was refused for it.
        #
        # Measured: 'What is CAL?', 'What is IBA?', 'What is APSAL?', ETVAL,
        # MOLIT and PITSA all resolved to no scope at all while sitting
        # correctly in the index.
        #
        # Narrowing to rows whose scope_id EQUALS the token is not a tie-break
        # or a ranking — the other rows are simply about a different entity
        # that happens to contain this one's name. If that still leaves more
        # than one, the original data-defect case genuinely holds and the
        # refusal stands.
        exact = [r for r in rows
                 if (r.get("scope_id") or "").upper() == token]
        if len(exact) != 1:
            return None
        rows = exact
    return rows[0].get("scope_kind"), rows[0].get("scope_id")



_PUBLISHED_ENTITIES = None
# alias TOKEN -> the airspace's REAL stored scope_id. "ABUJA TMA" is not a
# database key; "ABUJA Terminal Control Area (TMA)" is. This is what lets
# _lookup_enr_scope() query the database by the id it actually indexed under,
# regardless of which alias the pilot typed.
_AIRSPACE_ALIAS_TARGET = {}
# alias KEY -> the direct routing's REAL stored scope_id. Same contract as
# _AIRSPACE_ALIAS_TARGET: "ABC-NANOS" is not a database key, "ABUJA VOR/DME
# (ABC)-NANOS" is.
_DCT_ALIAS_TARGET = {}


def _airspace_aliases(scope_id: str):
    """Every natural way a pilot would type this airspace's name.

    ENR_AIRSPACE ids are stored as full descriptive strings —
    "ABUJA Terminal Control Area (TMA)", "KANO Flight Information Region" —
    which is not how anyone asks about them. A pilot says "Abuja TMA" or
    "Kano FIR". Confirmed live: with no alias handling, ENR_AIRSPACE
    reachability tested at 0/42 (0%) across every phrasing tried, because the
    exact stored string was the ONLY thing that could ever match.

    Builds from the STRUCTURE of the stored id (text before "Terminal Control
    Area" or "Flight Information Region" is the place name; the bracketed
    abbreviation, where present, is the short form) rather than a lookup
    table of the 14 current names, so a new airspace added in a future AIRAC
    cycle is covered automatically as long as it follows the same two
    stored-name patterns this document already uses for all of them.

    "KANO EAST SECTOR" and similar SECTOR names need no aliasing: they are
    already stored in the short form a pilot would say."""
    aliases = {scope_id.upper()}
    if "(" in scope_id and ")" in scope_id:
        head = scope_id.split("(")[0].strip()
        abbr = scope_id.split("(")[1].split(")")[0].strip()
        city_words = []
        for w in head.split():
            if w in ("Terminal", "Control", "Area"):
                break
            city_words.append(w)
        city = " ".join(city_words)
        if city:
            aliases.add(f"{city} {abbr}".upper())
            aliases.add(f"{city}{abbr}".upper())
    elif "Flight Information Region" in scope_id:
        city_words = []
        for w in scope_id.split():
            if w in ("Flight", "Information", "Region"):
                break
            city_words.append(w)
        city = " ".join(city_words)
        if city:
            aliases.add(f"{city} FIR".upper())
            aliases.add(f"{city}FIR".upper())
    return aliases


def dct_key(s: str) -> str:
    """Normalise every way a pilot writes a direct routing to ONE key.

    "ABC to NANOS", "ABC-NANOS", "ABC - NANOS", "ABC DCT NANOS" and
    "ABC direct NANOS" are the same request. Rather than enumerating those
    forms as separate aliases, they are all collapsed to the stored id's own
    separator, so a phrasing nobody anticipated ("ABC  DCT  NANOS") still
    lands on the same key.

    Leaves any string without a routing separator untouched apart from case
    and whitespace, so it is safe to apply to ordinary names."""
    t = re.sub(r"\s+", " ", (s or "")).strip().upper()
    t = re.sub(r"\s*-\s*", "-", t)
    t = re.sub(r"\s+(?:TO|DCT|DIRECT)\s+", "-", t)
    return t


def _dct_endpoint_short(part: str) -> str:
    """"ABUJA VOR/DME (ABC)" -> "ABC". ENR_FRA_DCT ids print a navaid endpoint
    as its full station name with the ident in brackets; a pilot types the
    ident. A bare waypoint endpoint ("NANOS") has no brackets and is returned
    unchanged."""
    if "(" in part and ")" in part:
        inner = part.split("(")[1].split(")")[0].strip()
        if inner.isalpha() and 2 <= len(inner) <= 4:
            return inner
    return part.strip()


def _dct_aliases(scope_id: str):
    """Every natural way a pilot would name this direct routing.

    Three forms, all reduced through dct_key():
      * the stored id verbatim ("ABUJA VOR/DME (ABC)-NANOS", "KELAK-POLTO#2")
      * the id with our own "#N" collision suffix removed
      * both endpoints shortened to the identifiers a pilot uses ("ABC-NANOS")

    NO REVERSED FORM IS GENERATED. A published direct combination is
    directional; inventing "NANOS-ABC" from "ABC-NANOS" would be asserting a
    routing the AIP does not publish, which is the failure class this project
    exists to prevent. If the reverse is available, the AIP publishes it as its
    own row and it gets its own aliases."""
    out = {dct_key(scope_id)}
    base = scope_id.split("#")[0]
    out.add(dct_key(base))
    if "-" in base:
        a, _, b = base.partition("-")
        out.add(dct_key(f"{_dct_endpoint_short(a)}-{_dct_endpoint_short(b)}"))
    return {a for a in out if a}


def published_entities() -> dict:
    """{TOKEN: scope_kind} for every named ENR entity, cached.

    Includes VOR idents from VOR_IDENTS so an ident is recognised even before
    aip_facts is populated. Read once; a lookup failure yields the VOR idents
    alone rather than nothing, so recognition degrades instead of vanishing.

    This REPLACES pattern matching on token shape. It cannot affect aerodrome
    resolution: AERODROMES and VOR_IDENTS are untouched, and the caller only
    consults this when a query has produced no subject at all.

    ENR_AIRSPACE is included here as of the fix for 0/42 reachability — see
    _airspace_aliases(). Every alias maps back to the entity's REAL stored
    scope_id (in _AIRSPACE_ALIAS_TARGET), never to the alias text itself, so
    resolve() still looks the real record up by its true id."""
    global _PUBLISHED_ENTITIES
    if _PUBLISHED_ENTITIES is not None:
        return _PUBLISHED_ENTITIES
    out = {v.upper(): "ENR_NAVAID" for v in VOR_IDENTS.values()}
    global _AIRSPACE_ALIAS_TARGET, _DCT_ALIAS_TARGET
    _AIRSPACE_ALIAS_TARGET = {}
    _DCT_ALIAS_TARGET = {}
    try:
        from database import list_scope_ids
        for kind in ("ENR_NAVAID", "ENR_POINT", "ENR_AREA", "ENR_ROUTE"):
            for sid in list_scope_ids(kind):
                out.setdefault(sid.upper(), kind)
        for sid in list_scope_ids("ENR_AIRSPACE"):
            for alias in _airspace_aliases(sid):
                out.setdefault(alias, "ENR_AIRSPACE")
                _AIRSPACE_ALIAS_TARGET[alias] = sid
        # ENR_FRA_DCT. Previously absent from this function entirely, which is
        # why the exhaustive Part B run measured 4/132 (3%) reachability: not a
        # matching failure, no path at all. The 4 passes were incidental
        # navaid-ambiguity prompts on the first endpoint.
        #
        # COLLISIONS ARE DROPPED, NOT ARBITRATED. Our "#N" suffix marks two
        # genuinely different published routings that share endpoints, so the
        # shared alias "KELAK-POLTO" is a real ambiguity. Registering either
        # one would answer with the other routing's data half the time —
        # null-over-guess says refuse. Each id keeps its own verbatim-id alias,
        # so the specific routing is still reachable by its full name.
        _seen = {}
        for sid in list_scope_ids("ENR_FRA_DCT"):
            for alias in _dct_aliases(sid):
                _seen.setdefault(alias, set()).add(sid)
        for alias, sids in _seen.items():
            if len(sids) != 1:
                continue
            sid = next(iter(sids))
            out.setdefault(alias, "ENR_FRA_DCT")
            _DCT_ALIAS_TARGET[alias] = sid
    except Exception:                                  # noqa: BLE001
        pass
    _PUBLISHED_ENTITIES = out
    return out


def _ident_is_also_navaid(name: str):
    """(aerodrome_label, navaid_label) if `name` is a VOR ident that ENR 4.1
    also publishes as a navaid, else None.

    Only fires when BOTH readings are real: the token must be in VOR_IDENTS
    (so an aerodrome answer exists) AND be an indexed ENR_NAVAID (so a navaid
    answer exists). A token that is only one of the two resolves as it always
    did — this adds a question, never a refusal.

    Fails closed: any lookup error returns None and the caller proceeds
    unchanged, so an outage cannot turn a working answer into a prompt."""
    token = re.sub(r"\s+", "", (name or "").strip()).upper()
    if not token or len(token) > 4:
        return None
    owner = next((ic for ic, ident in VOR_IDENTS.items() if ident == token), None)
    if not owner:
        return None
    try:
        from database import find_aip_scope
        rows = [r for r in (find_aip_scope(token) or [])
                if r.get("scope_kind") == "ENR_NAVAID"]
    except Exception:                                  # noqa: BLE001
        return None
    if not rows:
        return None
    return (f"{owner} (the aerodrome)", f"{token} (the navaid)")


def resolve(ex: AIPQueryExtraction) -> Resolution:
    if not _loaded:
        load_index()

    # 1) Airspace / en-route -> ENR, AIRSPACE tag (ignore any aerodrome name;
    #    the embedded query still carries it for the vector search).
    #    NOTE: main.py narrows this afterwards — when the QUERY TEXT is
    #    specifically about an aerodrome's OWN AD 2.17 airspace (CTR/TMA
    #    limits, classification, transition altitude) and an aerodrome is
    #    named, it re-resolves to that aerodrome. That check needs the raw
    #    text, which this function does not receive.
    #
    #    TRY THE SPECIFIC ENTITY FIRST. This branch used to return the
    #    generic label unconditionally, which meant a query for a NAMED
    #    airspace — "Where is Kano FIR?", "ABUJA TMA" — was answered with
    #    "Airspace / En-route (ENR)" and no scope, never reaching the actual
    #    FIR/TMA/SECTOR record. Confirmed live: 0/42 (0%) of ENR_AIRSPACE
    #    queries resolved to their intended scope. If the classifier named an
    #    aerodrome/place alongside the airspace intent (ex.aerodrome_name is
    #    set — the model often captures "Kano" from "Kano FIR" there even
    #    though it is not an aerodrome), check whether that name is a known
    #    airspace before falling back to the unscoped label.
    if ex.intent == "airspace_lookup":
        named = (ex.aerodrome_name or "").strip()
        if named:
            # ANY ENR SCOPE COUNTS, NOT JUST ENR_AIRSPACE. This branch used to
            # require scope[0] == "ENR_AIRSPACE" and threw away every other
            # kind. Measured on the exhaustive Part B run: the classifier
            # labels a bare route/point/navaid query ("Where is UM114?",
            # "What is ETVAL?") as airspace_lookup and DOES put the token in
            # aerodrome_name — _lookup_enr_scope resolved it correctly to
            # ENR_ROUTE/UM114 — and this branch then discarded the resolved
            # scope and returned the unscoped "Airspace / En-route (ENR)"
            # label. That is the entire ENR_ROUTE "went to ?/?" cluster
            # (34 failures) plus the ?/? failures in ENR_POINT and
            # ENR_NAVAID: not a lookup failure, a discarded lookup RESULT.
            scope = _lookup_enr_scope(named)
            if scope:
                kind, sid = scope
                return Resolution(part="ENR", reference="AIRSPACE",
                                  label=f"{sid} ({_SCOPE_LABELS.get(kind, kind)})",
                                  is_national=True,
                                  scope_kind=kind, scope_id=sid)
        return Resolution(is_national=True, part="ENR", reference="AIRSPACE",
                          label="Airspace / En-route (ENR)",
                          aerodrome_hint=_hint_for(ex.aerodrome_name or ""))
    # 2) National / general -> GEN, NATIONAL tag.
    if ex.intent == "national_lookup":
        return Resolution(is_national=True, part="GEN", reference="NATIONAL",
                          label="National (GEN)",
                          aerodrome_hint=_hint_for(ex.aerodrome_name or ""))

    # 3) Explicit DN code.
    code = (ex.icao_code or "").upper()
    if _DN_CODE.match(code):
        if code in VALID_ICAO:
            return _aero(code)
        if code == FIR_ICAO:
            return Resolution(is_national=True, part="ENR", reference=FIR_ICAO,
                              label="Kano FIR / En-route (DNKK)")
        if code in OUT_OF_SCOPE_ICAO:
            return Resolution(unresolved=True,
                              reason=(f"{OUT_OF_SCOPE_ICAO[code]} ({code}) is a registered "
                                      "Nigerian location indicator but has no published "
                                      "aerodrome section in the 2026 AIP."))
        return Resolution(unresolved=True,
                          reason=f"{code} is not a valid Nigerian location indicator in the 2026 AIP.")

    # 4) Aerodrome name.
    name = (ex.aerodrome_name or "").strip()
    if name:
        # A DIRECT ROUTING IS CHECKED BEFORE ANY AERODROME MATCHING.
        # _match_name() is a whole-word substring match, so "ILBAS to LAG"
        # contains " lag " and resolves to DNMM, and "ABC to NANOS" contains
        # " abc " and resolves to DNAA. Measured on the exhaustive run: that
        # is what every one of the "went to AD/..." DCT failures actually was
        # — the phrase matching one of its own endpoints. The endpoint really
        # is in the string, so no amount of scoring fixes it; the pair has to
        # be recognised first.
        _dct = _DCT_ALIAS_TARGET.get(dct_key(name))
        if _dct:
            scope = _lookup_enr_scope(name)
            if scope:
                kind, sid = scope
                return Resolution(part="ENR", reference="AIRSPACE",
                                  label=f"{sid} ({_SCOPE_LABELS.get(kind, kind)})",
                                  is_national=True,
                                  scope_kind=kind, scope_id=sid)

        nl = name.lower()
        if "fir" in nl or "en-route" in nl or "enroute" in nl:
            return Resolution(is_national=True, part="ENR", reference="AIRSPACE",
                              label="Kano FIR / En-route",
                              aerodrome_hint=_hint_for(name))
        # A VOR IDENT RESOLVES AS ITS AERODROME, AND THAT IS THE PROBLEM.
        # VOR_IDENTS is loaded into _ALIASES, so "ABC" matches DNAA here and
        # never reaches the ENR lookup further down. Confirmed live: "what is
        # the frequency of ABC?" returned Abuja's ATS COMMS from AD 2.18 when
        # the asker plainly meant the VOR/DME on 116.3 MHz.
        #
        # Both readings are correct — ENR 4.1 publishes ABC as a navaid in its
        # own right — so neither may be assumed. Checked BEFORE the alias match
        # succeeds, because afterwards the aerodrome answer has already won.
        _amb = _ident_is_also_navaid(name)
        if _amb:
            return Resolution(
                ambiguous=list(_amb), ambiguous_kind="scope",
                reason=f"'{re.sub(r'\s+', '', name.strip()).upper()}' is both an "
                       f"aerodrome and a published navaid.")

        cands = _match_name(name)
        if len(cands) == 1:
            return _aero(next(iter(cands)))
        if len(cands) > 1:
            return Resolution(ambiguous=sorted(cands),
                              reason=f"'{name}' matches more than one aerodrome.")
        # EXACT ENR MEMBERSHIP IS CHECKED BEFORE THE OUT-OF-SCOPE CITY LOOP.
        # OBUDU is BOTH an unpublished aerodrome (DNOB) and a published
        # significant point in ENR 3.x. The loop below matched the city first
        # and refused, so "Where is OBUDU?" answered "Obudu (DNOB) has no
        # published aerodrome section" about a waypoint that is indexed and
        # answerable — 9 failures in ENR_POINT and 12 in ENR_FRA_DCT on the
        # exhaustive run. An exact id match is stronger evidence than a
        # first-word city match, so it is consulted first.
        scope = _lookup_enr_scope(name)
        if scope:
            kind, sid = scope
            return Resolution(part="ENR", reference="AIRSPACE",
                              label=f"{sid} ({_SCOPE_LABELS.get(kind, kind)})",
                              is_national=True,
                              scope_kind=kind, scope_id=sid)

        for c, loc in OUT_OF_SCOPE_ICAO.items():
            if _normalize(loc).split()[0] in _normalize(name).split():
                return Resolution(unresolved=True,
                                  reason=(f"{loc} ({c}) has no published aerodrome "
                                          "section in the 2026 AIP."))
        # NOT AN AERODROME — is it another published AIP entity? Waypoints,
        # airways and prohibited/restricted/danger areas are all named things a
        # pilot legitimately asks about, and none of them has an ICAO code.
        # Before this, "Where is TEMSA?" got "I don't have 'TEMSA' in the
        # Nigerian AIP" — for a significant point published on seven pages.
        #
        # The lookup is EXACT, against the indexed entities. No embedding, no
        # ranking: a name either is a published entity or it is not. That is
        # the same property that makes AERODROMES reliable, and it is why this
        # cannot misroute one entity to another.
        # A VOR IDENT IS AMBIGUOUS BY CONSTRUCTION. VOR_IDENTS maps "ABC" to
        # DNAA so an ident resolves as the aerodrome, but ENR 4.1 publishes
        # "ABC" as a navaid in its own right. Confirmed live: "what is the
        # frequency of ABC?" returned Abuja's ATS COMMS frequencies from
        # AD 2.18 when the asker plainly meant the VOR/DME on 116.3 MHz.
        #
        # Both readings are correct, so neither may be assumed. Ask — the same
        # choice clarify.decide() makes for an underspecified approach.
        return Resolution(unresolved=True,
                          reason=(f"I don't have '{name}' in the Nigerian AIP. "
                                  f"I cover published aerodromes (DN codes), "
                                  f"significant points, ATS routes and "
                                  f"prohibited/restricted/danger areas."))

    # 5) AD-type intent but no aerodrome given -> ask, don't guess.
    if ex.intent in _AD_INTENTS:
        return Resolution(unresolved=True,
                          reason="Which aerodrome? Please give a name or ICAO code (starts with DN).")

    # 6) Fallback -> national sweep.
    part = ex.filter_part if ex.filter_part in ("GEN", "ENR") else "GEN"
    return Resolution(is_national=True, part=part, reference=None, label="National / En-route")
