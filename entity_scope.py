"""
entity_scope.py — the phrasing-independent safety chokepoint.

WHY THIS EXISTS
---------------
Every safety guard in synthesize.py asks the same question:

    "Does this QUESTION look dangerous?"

That question is unanswerable. English is unbounded, so a keyword list is a
guess about what a pilot might type, and every guess eventually misses. The
confirmed live failure: a pilot typed "let down" instead of "letdown". Three
independent regexes (agent._APPROACH_PROC_RE, synthesize._PROC_RE,
subsection_router._AD222_RE) all keyed on the same closed vocabulary, so all
three missed simultaneously, and the query reached free synthesis over an
AD 2.22 chunk containing BOTH of Maiduguri's runways. The reply spliced
RWY 05's letdown onto RWY 23's data. The number-verifier passed it, because
every number quoted really was in the source -- it just belonged to the other
runway. Widening the regex fixes that one phrasing and nothing else.

This module asks a different question, of the SOURCE rather than the query:

    "Does the retrieved content contain procedure entities that free synthesis
     could splice together?"

That question IS answerable, because the AIP's structure does not change based
on how a pilot phrases a request. A query with no recognisable keyword at all
("how do I get down to maiduguri for rwy 05") is caught identically to one
that spells out "letdown procedure", because neither the question nor its
wording is consulted.

WHY NOT JUST THE APPROACH HEADER
--------------------------------
The first version of this guard keyed on procedures.py's
"Instrument approach procedures for RWY <nr> based on <type>" header. That was
wrong, and real data proved it: DNKS (Kashimbila) publishes no such header at
all. Its approaches live entirely under PBN headings --

    2.22.7.3.4.1  Holding procedure
    2.22.7.3.5    Missed approach
    2.22.7.4.3.1  Holding Procedure RWY 31
    2.22.7.4.3.2  Missed Approach Procedure RWY 31

-- two distinct runway entities, zero matching headers. A header-based guard
returns False for DNKS and lets exactly the same splice through on a different
aerodrome. Structure varies between aerodromes; the PRESENCE OF A PROCEDURE
LABEL does not. So this module keys on the label, which is the one thing every
publication style shares.

DELIBERATE ASYMMETRY: THIS GUARD OVER-FIRES ON PURPOSE
------------------------------------------------------
This is a REFUSAL guard, so its two failure modes have wildly different costs:

  * false POSITIVE  -> an answer is shown verbatim or as a plate instead of
                       being synthesized. The pilot still gets the published
                       text. Cost: mild verbosity.
  * false NEGATIVE  -> a spliced procedure with another runway's altitudes is
                       asserted as fact. Cost: the exact thing this project
                       exists to prevent.

Given that asymmetry, the matching here is deliberately generous. It is NOT
tuned for precision, and it should not be "improved" by tightening it.

WHERE IT SITS
-------------
Last, immediately before generate_grounded_answer(). Every dedicated guard and
the subsection router run FIRST and are untouched, so paths that already answer
correctly (minima -> subsection_verbatim, take-off minima -> subsection, the
structured lookups) never reach this code. It only claims queries that would
otherwise have fallen through to free synthesis -- precisely the population
that produced the Maiduguri failure.
"""
import re
from typing import List, Optional

# A procedure LABEL. Structure-independent by design: it does not require a
# section number, an approach header, or any particular heading hierarchy,
# because real aerodromes differ on all three (DNMA numbers its labels;
# DNKS nests them under PBN coding sections; some carry a trailing RWY).
#
# "missed approach" needs no "procedure" suffix -- DNKS publishes a bare
# "2.22.7.3.5 Missed approach". "letdown" tolerates the space-separated
# spelling for the same reason the live bug existed in the first place.
_PROC_LABEL_RE = re.compile(
    r"\b(holding\s+procedures?|let\s?-?\s?down\s+procedures?|"
    r"missed\s+approach(?:\s+procedures?)?)\b", re.I)

# A runway designator carried on a procedure/approach heading. Tolerates the
# PDF character-split artifacts already confirmed in this project ("18 R",
# "0 3") -- the same tolerance procedures._HDR needed.
_RWY_ON_HEADING_RE = re.compile(r"\bRWY\s*(\d\s*\d\s*[LRC]?)\b", re.I)

# AD 2.22 is the only subsection whose free synthesis can splice procedures.
_AD222_SECTION_RE = re.compile(r"^AD\s*2\.22\b", re.I)


def _norm_rwy(raw: str) -> str:
    """'1 8 R' -> '18R'. Strips the internal whitespace PDF extraction leaves."""
    return re.sub(r"\s+", "", raw or "").upper()


def is_ad222(section: str) -> bool:
    """True if an aip_section tag names AD 2.22."""
    return bool(_AD222_SECTION_RE.match((section or "").strip()))


def procedure_labels(text: str) -> List[str]:
    """Every procedure label occurrence in the text, in document order.

    Used both as the safety signal (any label at all) and as the ambiguity
    signal (more than one distinct entity)."""
    return [m.group(0) for m in _PROC_LABEL_RE.finditer(text or "")]


def has_procedure_content(text: str) -> bool:
    """True if this text contains ANY approach-procedure content.

    Deliberately not "more than one" -- see the asymmetry note in the module
    docstring. Even a single procedure block sits adjacent to minima tables and
    neighbouring sections, so free synthesis over it is never the right path;
    procedures.py's scoped extractor or the plate is."""
    return bool(_PROC_LABEL_RE.search(text or ""))


def runway_entities(text: str) -> List[str]:
    """Distinct runway designators appearing ON procedure/approach headings.

    NOT VALIDATED FOR THE REQUEST PATH -- diagnostic use only for now.

    It reads DNKS correctly ('31' from 'Holding Procedure RWY 31') but returns
    nothing for DNMA, whose designators sit on 'MAPt: THR RWY 23' lines rather
    than on the headings themselves. So it under-reports on at least one real
    publication style and must be proven against all 36 aerodromes before
    anything user-facing depends on it.

    Nothing needs it yet: the clarification buttons are already driven by the
    aip_charts catalogue (clarify.decide), which is validated and which cannot
    offer an approach the AIP does not publish. This function exists for the
    validation script and as the foundation if catalogue-independent
    clarification is ever wanted -- it is deliberately not wired in."""
    seen, out = set(), []
    for line in (text or "").splitlines():
        if not _PROC_LABEL_RE.search(line) and "approach" not in line.lower():
            continue
        for m in _RWY_ON_HEADING_RE.finditer(line):
            rwy = _norm_rwy(m.group(1))
            if rwy and rwy not in seen:
                seen.add(rwy)
                out.append(rwy)
    return out


def blocks_free_synthesis(results) -> bool:
    """THE CHOKEPOINT. True if any retrieved chunk is AD 2.22 content carrying
    approach-procedure text -- meaning free synthesis over these results could
    splice two entities together, and must not run.

    Takes the retrieved results rather than the question, which is the whole
    point: no phrasing, however novel, can route around this."""
    for r in results or []:
        section = getattr(r, "aip_section", "") or ""
        if is_ad222(section) and has_procedure_content(getattr(r, "content", "") or ""):
            return True
    return False


def scope_report(text: str) -> dict:
    """Diagnostic summary of one aerodrome's AD 2.22 text. Not used in the
    request path -- this exists so the 36-aerodrome validation script can show
    what was detected per aerodrome, in keeping with the project's rule that
    every extractor is proven against all 36 before sign-off."""
    labels = procedure_labels(text)
    rwys = runway_entities(text)
    return {
        "has_procedure_content": bool(labels),
        "label_count": len(labels),
        "labels": labels,
        "runway_entities": rwys,
        "ambiguous": len(rwys) > 1 or len(labels) > 1,
    }
