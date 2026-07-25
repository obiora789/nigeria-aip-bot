"""
entity_scope.py — the phrasing-independent safety chokepoint.

THE PROBLEM
-----------
Every safety guard in synthesize.py asks:

    "Does this QUESTION look dangerous?"

That is unanswerable. English is unbounded, so a keyword list is a guess about
what a pilot might type, and every guess eventually misses. Confirmed live: a
pilot typed "let down" instead of "letdown". Three independent regexes
(agent._APPROACH_PROC_RE, synthesize._PROC_RE, subsection_router._AD222_RE)
all keyed on the same closed vocabulary, so all three missed at once, and the
query reached free synthesis over an AD 2.22 chunk holding BOTH of Maiduguri's
runways. The reply spliced RWY 05's letdown onto RWY 23's figures. The
number-verifier passed it, because every number really was in the source -- it
just belonged to the other runway.

TWO WRONG ANSWERS BEFORE THIS ONE (both caught by real data, recorded so the
mistake is not repeated a third time)
-------------------------------------------------------------------------
ATTEMPT 1 -- key on procedures.py's approach header,
"Instrument approach procedures for RWY <nr> based on <type>".
Measured against the real AIP: only 9 of 30 aerodromes publishing approach
procedures use that phrasing. It missed 21, including DNKN (Kano), DNEN, DNBE,
DNKT and DNKS. DNKS publishes no such header at all -- its approaches sit under
PBN headings (2.22.7.3.4.1 Holding procedure). 30% coverage.

ATTEMPT 2 -- key on procedure LABELS (holding / letdown / missed approach).
Better: 30 of 30. But it declared six aerodromes as publishing "no procedures",
and five of those six were wrong. They publish PBN/RNAV coding, radar
procedures, VFR procedures, approach minima and takeoff minima -- none of which
contain those three labels. DNIL additionally publishes per-runway DEPARTURE
procedures where RWY 05 turns RIGHT and RWY 23 turns LEFT: a splice there is a
wrong-way turn on departure.

Attempt 2 was the same mistake as the query regexes, moved one level down. A
closed vocabulary is a closed vocabulary whether it is applied to a pilot's
sentence or to a document's headings.

THE INVARIANT THAT NEEDS NO VOCABULARY
--------------------------------------
AD 2.22 IS "Flight Procedures". Its identity, not its wording, is the fact.
Every subsection of it -- holding, letdown, missed approach, departure,
emergency, radar, VFR, PBN coding, approach minima, takeoff minima -- is
per-runway or per-category operational content that free synthesis could
attribute to the wrong entity. There is no sub-vocabulary to enumerate, so
there is nothing left to miss. The check is section identity and nothing else.

That is why this module is short. Its brevity is the point: the two longer,
cleverer versions were both wrong, and each was wrong because it tried to
recognise content rather than trust structure.

WHAT THIS COSTS
---------------
Nothing that is not already covered, plus verbatim instead of prose for a
narrow band of queries:
  * General / Runway in use / Radar / VFR minima / VFR flights -> already
    answered deterministically and verbatim by clarify.info_block_answer(),
    which runs BEFORE synthesis and is untouched;
  * approach minima / decision heights -> already "subsection_verbatim";
  * approach procedures -> procedures.py's scoped extractor, or the plate;
  * everything else in AD 2.22 (PBN, departure, emergency) -> verbatim,
    focused to ~700 characters around the query terms by responder._focus.
For procedures, verbatim is arguably the better answer regardless: a pilot
should read the published words, not a paraphrase of them.

WHY A REFUSAL GUARD SHOULD OVER-FIRE
------------------------------------
  * false POSITIVE -> published text is shown verbatim instead of synthesized.
    The pilot still gets the answer. Cost: mild verbosity.
  * false NEGATIVE -> a spliced procedure carrying another runway's altitudes
    or turn direction is asserted as fact. Cost: the thing this project exists
    to prevent.
Given that asymmetry this is deliberately blunt, and should not be "improved"
by making it more selective.
"""
import re
from typing import List

# Sections whose content is per-entity operational data that free synthesis
# could misattribute, and which therefore must never be synthesized over.
#
# AD 2.22 (Flight Procedures) is here because EVERY subsection of it is a
# procedure tied to a specific runway, approach type or aircraft category --
# see the module docstring for the measured evidence.
#
# Other misattribution-prone sections (AD 2.12 asymmetric fields, AD 2.13
# declared distances, AD 2.18 comms, AD 2.19 navaids) are deliberately NOT
# listed: each already has a dedicated, battle-tested guard in synthesize.py
# that routes to a structured per-entity lookup, which is a better answer than
# refusing. This set is for sections with no such structured path.
NEVER_SYNTHESIZE_SECTIONS = ("AD 2.22",)

_SECTION_RE = re.compile(r"^\s*(AD\s*\d+\.\d+)", re.I)


def _canon(section: str) -> str:
    """'ad 2.22', ' AD2.22 ', 'AD 2.22 FLIGHT PROCEDURES' -> 'AD 2.22'.

    Matches on the section id only, so a stored aip_section that carries a
    title suffix still resolves. Anything unrecognised returns '' and is
    treated as not-in-the-set."""
    m = _SECTION_RE.match(section or "")
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1).upper().replace("AD", "AD ")).strip()


def is_ad222(section: str) -> bool:
    """True if an aip_section tag names AD 2.22."""
    return _canon(section) == "AD 2.22"


def is_never_synthesize(section: str) -> bool:
    """True if this section must never be free-synthesized over."""
    return _canon(section) in NEVER_SYNTHESIZE_SECTIONS


def blocks_free_synthesis(results) -> bool:
    """THE CHOKEPOINT. True if any retrieved chunk belongs to a section that
    must never be synthesized over.

    Takes the retrieved RESULTS, never the question. That is the whole point:
    no phrasing, however novel, can route around a check that does not read
    phrasing. A query with no recognisable keyword at all ("how do I get down
    to maiduguri for rwy 05") is treated identically to one that spells out
    "letdown procedure", because neither is consulted."""
    return blocking_section(results) is not None


def blocking_section(results):
    """The section id that triggered the block, or None.

    READ THIS BEFORE WIRING THIS MODULE IN.

    Callers must send a blocked query to that SECTION, shown verbatim -- NOT
    to the approach plate. Holding, letdown and missed approach are only the
    three procedures that appear ON an approach plate. AD 2.22 also publishes
    departure procedures (DNIL: RWY 05 turns right, RWY 23 turns left),
    emergency procedures (DNIL, DNPO), radar procedures (35 aerodromes), VFR
    procedures (35), PBN/RNAV coding (35), approach minima (34) and takeoff
    minima (32). None of those are on an approach plate.

    An earlier version of this patch returned "approach_procedure" for every
    blocked query, which would have answered "what is the departure procedure
    for Ilorin" by offering an ILS/RNAV/VOR approach plate -- a confident
    artifact answering a different question, which is the same failure class
    this module exists to stop.

    Approach-plate queries are already routed to the plate earlier, by
    synthesize._PROC_RE and agent._APPROACH_PROC_RE. What reaches this guard
    is, by definition, everything else -- so verbatim from the named section
    is the correct destination for it."""
    for r in results or []:
        section = getattr(r, "aip_section", "") or ""
        if is_never_synthesize(section):
            return _canon(section)
    return None


# ---------------------------------------------------------------------------
# Diagnostics only. NOT used by the request path and NOT load-bearing for
# safety -- the guard above deliberately does not look at content. These exist
# so validate_entity_scope.py can report what each aerodrome actually
# publishes, which is how attempt 2's blind spot was found.
# ---------------------------------------------------------------------------

_PROCEDURE_KINDS = [
    ("holding", r"holding\s+procedure"),
    ("letdown", r"let\s?-?\s?down\s+procedure"),
    ("missed approach", r"missed\s+approach"),
    ("departure", r"departure\s+procedure"),
    ("emergency", r"emergency\s+procedure"),
    ("pbn/rnav", r"\bPBN\s+procedure|RNAV\s*\(\s*GNSS\s*\)|RNP\s+APCH"),
    ("radar", r"radar\s+procedure"),
    ("vfr", r"procedures?\s+for\s+VFR"),
    ("approach minima", r"approach\s+minima"),
    ("takeoff minima", r"take\s?off\s+minima"),
]


def procedure_kinds(text: str) -> List[str]:
    """Which kinds of published procedure appear in this text. Diagnostic."""
    t = text or ""
    return [name for name, pat in _PROCEDURE_KINDS if re.search(pat, t, re.I)]
