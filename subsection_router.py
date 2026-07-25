"""
subsection_router.py — deterministic AD 2.x subsection routing.

WHY THIS EXISTS
---------------
Before Layer 2, an AD 2.x query could only be answered by vector search over
character-chunked text: the retriever guessed which chunk was relevant, and a
low-similarity guess could surface an entirely unrelated subsection (confirmed
live: "Abuja runway" returned AD 2.22's approach-minima table at 55% match,
because nothing routed it to AD 2.12).

vectorise_aip_v3.py changed the premise. Every AD 2.x subsection is now stored
as its OWN chunk, tagged with its exact `aip_section` ("AD 2.17", "AD 2.13"...).
That makes `database.get_section_text(icao, "AD 2.NN")` a deterministic,
exact fetch — no similarity ranking involved at all. So if we can identify WHICH
subsection a question is about, we can retrieve exactly that subsection and
never show content from a different one.

This module is that identification step: a keyword→subsection registry, checked
in specificity order, returning `"AD 2.NN"` or None.

HOW IT FITS THE EXISTING GUARDS
-------------------------------
This router runs LAST in synthesize_decision(), after every dedicated guard
(minima, procedures, declared distances, navaids, comms, runway char/data,
lighting, restrictions). Those guards are more specific and battle-tested —
they stay first and unchanged. The router only claims queries none of them
wanted, which is precisely the population that used to fall through to
undirected vector search.

DELIBERATELY CONSERVATIVE
-------------------------
Returning a subsection for EVERY query would be wrong: it would kill general
synthesis for questions that legitimately span sections or aren't AD 2 at all.
Each pattern therefore requires a DISTINCTIVE term for its subsection — a term
that subsection owns and others don't. A question with no distinctive term
returns None and flows to the existing general path, unchanged.

Ordering matters where a word is shared. "hours" appears in AD 2.3
(operational hours), AD 2.11 (MET office hours) and AD 2.18 (comms hours) — so
the MET and comms patterns require their own distinctive companion term, and
the bare-hours pattern for 2.3 sits after them.
"""
import re
from typing import Optional

# (subsection, human name, pattern). ORDER IS SIGNIFICANT — first match wins,
# so more specific patterns must precede more general ones.
AD2_ROUTES = [
    # --- AD 2.16: helicopter. Distinctive: TLOF/FATO are used nowhere else.
    ("AD 2.16", "Helicopter landing area", re.compile(
        r"\b(tlof|fato|helicopter\s+landing|helipad|helideck)\b", re.I)),

    # --- AD 2.11: MET. Checked before the generic 'hours' pattern (2.3) because
    #     "MET office hours" belongs here, not to operational hours.
    ("AD 2.11", "Meteorological information", re.compile(
        r"\b(met\s+office|meteorolog\w+|taf\b|trend\s+forecast|landing\s+forecast|"
        r"flight\s+documentation|met\s+briefing|awos|llwas)\b", re.I)),

    # --- AD 2.8: aprons/taxiways/checkpoints. 'checkpoint' is distinctive;
    #     apron/taxiway surface+strength lives here (AD 2.9 is markings/signs).
    ("AD 2.8", "Aprons, taxiways and check locations", re.compile(
        r"\b(altimeter\s+check\w*|vor\s+check\w*|ins\s+check\w*|checkpoint)\b|"
        r"\b(apron|taxiway|twy)\b.{0,25}\b(surface|strength|width|designation|pcn)\b|"
        r"\b(surface|strength|width)\b.{0,25}\b(apron|taxiway|twy)\b", re.I)),

    # --- AD 2.9: surface movement guidance and MARKINGS (distinct from 2.8's
    #     physical characteristics and 2.14's runway lighting).
    ("AD 2.9", "Surface movement guidance and markings", re.compile(
        r"\b(stop\s?bars?|stand\s+id\s+signs?|taxi\s*-?\s*link|guide\s+lines?|"
        r"docking\s+guidance|surface\s+movement\s+guidance)\b|"
        r"\b(taxiway|twy|runway|rwy)\s+markings?\b", re.I)),

    # --- AD 2.15: other lighting. MUST come after AD 2.14's lighting guard
    #     (which runs earlier, in synthesize_decision) — this catches the
    #     NON-runway lighting AD 2.14 deliberately excludes.
    ("AD 2.15", "Other lighting, secondary power supply", re.compile(
        r"\b(abn|ibn|aerodrome\s+beacon|identification\s+beacon|wind\s+direction\s+indicator|"
        r"wdi|anemometer|secondary\s+power|standby\s+power|switch-?over|apron\s+flood\w*)\b", re.I)),

    # --- AD 2.6: rescue and fire fighting.
    ("AD 2.6", "Rescue and fire fighting", re.compile(
        r"\b(rff|rescue\s+and\s+fire|fire\s+fighting|fire\s+category|"
        r"crash\s+rescue|disabled\s+aircraft)\b", re.I)),

    # --- AD 2.4: handling services. Fuel/hangar/cargo are distinctive.
    ("AD 2.4", "Handling services and facilities", re.compile(
        r"\b(fuel(?:ling|ing)?|jet\s*a-?1|avgas|oil\s+type|hangar|cargo[\s-]*handling|"
        r"repair\s+facilit\w+|de-?icing|ground\s+handling)\b", re.I)),

    # --- AD 2.5: passenger facilities.
    ("AD 2.5", "Passenger facilities", re.compile(
        r"\b(hotels?|restaurants?|passenger\s+facilit\w+|tourist|"
        r"medical\s+facilit\w+|bank\b|post\s+office|transportation)\b", re.I)),

    # --- AD 2.10: obstacles.
    ("AD 2.10", "Aerodrome obstacles", re.compile(
        r"\b(obstacles?|obstruction\w*)\b", re.I)),

    # --- AD 2.17: ATS airspace. CTR/TMA/transition altitude are distinctive.
    ("AD 2.17", "ATS airspace", re.compile(
        r"\b(ctr\b|tma\b|control\s+zone|terminal\s+(?:control\s+)?area|"
        r"airspace\s+class\w*|transition\s+(?:altitude|level)|vertical\s+limits?|"
        r"lateral\s+limits?|ats\s+airspace)\b", re.I)),

    # --- AD 2.20: local aerodrome regulations.
    ("AD 2.20", "Local aerodrome regulations", re.compile(
        r"\b(local\s+(?:aerodrome\s+)?regulations?|airport\s+regulations?|"
        r"taxiing\s+limitation\w*|school\s+(?:and\s+)?training\s+flights?|"
        r"parking\s+area|fuel\s+spillage)\b", re.I)),

    # --- AD 2.21: noise abatement.
    ("AD 2.21", "Noise abatement procedures", re.compile(
        r"\b(noise\s+abatement|noise\s+preferent\w+|noise\s+restrict\w+)\b", re.I)),

    # --- AD 2.23: additional information (bird/wildlife hazards).
    ("AD 2.23", "Additional information", re.compile(
        r"\b(bird\s+\w*|birds\b|wildlife|animal\s+hazard|animals?\s+on\s+"
        r"(?:the\s+)?runway)\b", re.I)),

    # --- AD 2.7: seasonal availability / clearing.
    ("AD 2.7", "Seasonal availability, clearing", re.compile(
        r"\b(seasonal\s+availab\w+|clearing\s+equipment|snow\s+plan|"
        r"clearance\s+priorit\w+)\b", re.I)),

    # --- AD 2.3: operational hours. LAST of the 'hours' family on purpose —
    #     MET (2.11) and comms (2.18, guarded earlier) own their own hours.
    ("AD 2.3", "Operational hours", re.compile(
        r"\b(operational\s+hours?|hours\s+of\s+operation|opening\s+hours?|"
        r"customs|immigration|health\s+and\s+sanitation|ats\s+reporting|"
        r"\baro\b|ais\s+briefing)\b", re.I)),

    # --- AD 2.2: geographic/admin data. 'ARP', 'magnetic variation',
    #     'reference temperature' are distinctive; bare 'elevation' is NOT
    #     (AD 2.12's threshold elevation is caught by an earlier guard, but a
    #     bare "elevation of X" is genuinely ambiguous and better served by the
    #     existing get_aerodrome_data path, so it is deliberately absent here).
    ("AD 2.2", "Aerodrome geographical and administrative data", re.compile(
        r"\b(arp\b|aerodrome\s+reference\s+point|magnetic\s+variation|mag\s*var|"
        r"reference\s+temperature|aftn|geoid\s+undulation|"
        r"aerodrome\s+(?:administration|operator))\b", re.I)),
]

# AD 2.22 gets its own two-way split (see detect_subsection).
# NOTE: "transition altitude" is deliberately NOT here — it is a canonical
# AD 2.17 field (AD217Extractor defines transition_altitude explicitly).
# Including it routed "transition altitude for DNMM" to AD 2.22, since this
# pattern is checked before the AD2_ROUTES list. Caught by testing.
_AD222_RE = re.compile(
    r"\b(flight\s+procedures?|holding\s+procedure|letdown|let-?down|"
    r"missed\s+approach|circling|approach\s+minima|take-?off\s+minima|"
    r"oca\b|och\b|pbn\b|rnp\s+ar)\b", re.I)

# Within AD 2.22, does the question concern an INSTRUMENT APPROACH (which has a
# corresponding AD 2.24 plate to show alongside), or other 2.22 content (take-off
# minima, PBN coding tables, VFR rules) which is text-only? Deliberately narrow:
# only genuine approach terms, so "take-off minima" is NOT treated as an approach.
_AD222_APPROACH_RE = re.compile(
    r"\b(approach\s+procedure|instrument\s+approach|holding\s+procedure|"
    r"letdown|let-?down|missed\s+approach|approach\s+plate|approach\s+chart|"
    r"\bils\b|\brnav\b|\bgnss\b|\brnp\b|\bvor\b|\bndb\b|\bloc\b)\b", re.I)


def detect_subsection(question: str) -> Optional[str]:
    """Return the exact 'AD 2.NN' this question is about, or None.

    None means "no distinctive term for any one subsection" — the caller keeps
    its existing behaviour (general synthesis over vector search). This is the
    safe default: the router only claims a query when it is confident, so it
    can never make an existing working answer worse."""
    q = question or ""
    if _AD222_RE.search(q):
        return "AD 2.22"
    for subsection, _name, pattern in AD2_ROUTES:
        if pattern.search(q):
            return subsection
    return None


def is_approach_query(question: str) -> bool:
    """True when an AD 2.22 question is about an instrument approach — which
    has a corresponding AD 2.24 plate to show alongside the procedure text.

    False for other AD 2.22 content (take-off minima, PBN coding tables, VFR
    rules within the TMA): those are answered as TEXT ONLY, with no chart,
    because no single plate corresponds to them. Showing an arbitrary approach
    plate next to a take-off-minima answer would imply a connection that
    doesn't exist."""
    return bool(_AD222_APPROACH_RE.search(question or ""))


def section_name(subsection: str) -> str:
    """Human-readable name for a subsection id, for reply headers."""
    for sub, name, _pattern in AD2_ROUTES:
        if sub == subsection:
            return name
    return {"AD 2.22": "Flight procedures"}.get(subsection, subsection)
