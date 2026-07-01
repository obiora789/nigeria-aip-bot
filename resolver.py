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


def resolve(ex: AIPQueryExtraction) -> Resolution:
    if not _loaded:
        load_index()

    # 1) Airspace / en-route -> ENR, AIRSPACE tag (ignore any aerodrome name;
    #    the embedded query still carries it for the vector search).
    if ex.intent == "airspace_lookup":
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
        nl = name.lower()
        if "fir" in nl or "en-route" in nl or "enroute" in nl:
            return Resolution(is_national=True, part="ENR", reference="AIRSPACE",
                              label="Kano FIR / En-route",
                              aerodrome_hint=_hint_for(name))
        cands = _match_name(name)
        if len(cands) == 1:
            return _aero(next(iter(cands)))
        if len(cands) > 1:
            return Resolution(ambiguous=sorted(cands),
                              reason=f"'{name}' matches more than one aerodrome.")
        for c, loc in OUT_OF_SCOPE_ICAO.items():
            if _normalize(loc).split()[0] in _normalize(name).split():
                return Resolution(unresolved=True,
                                  reason=(f"{loc} ({c}) has no published aerodrome "
                                          "section in the 2026 AIP."))
        return Resolution(unresolved=True,
                          reason=f"I don't have '{name}' in the Nigerian AIP.")

    # 5) AD-type intent but no aerodrome given -> ask, don't guess.
    if ex.intent in _AD_INTENTS:
        return Resolution(unresolved=True,
                          reason="Which aerodrome? Please give a name or ICAO code (starts with DN).")

    # 6) Fallback -> national sweep.
    part = ex.filter_part if ex.filter_part in ("GEN", "ENR") else "GEN"
    return Resolution(is_national=True, part=part, reference=None, label="National / En-route")
