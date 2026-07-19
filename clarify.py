"""
clarify.py — the brain of the AD 2.22/2.24 clarification and dispatch feature
(pure + deterministic wherever possible; no LLM, no network — unit-testable
in isolation).

MERGED FROM ad222_respond.py — CRITICAL LOOK, NOT A BLIND COMBINE
------------------------------------------------------------------
ad222_respond.py was never wired into main.py at all (confirmed:
`grep -c "ad222_respond" main.py` returns 0). Auditing it function by
function before merging, rather than keeping it wholesale:

  CONFIRMED DEAD, DROPPED ENTIRELY:
    * parse_chart_index() parsed an AD 2.24 index page's RAW PDF WORDS at
      query time — but the live bot never has PDF access during a query,
      only during ingestion (extract_charts.py already does this job, at
      the right time, and populates aip_charts directly).
    * find_charts() operated on parse_chart_index()'s OUTPUT shape — chart
      records with approach_types/runways as SETS. That is a different,
      incompatible shape from ChartRef's actual fields (procedure_type/
      runway as singular strings) — the shape decide() below already
      handles correctly, via the real aip_charts table. Since its only
      input (parse_chart_index) is dead, find_charts was unreachable too.
    * answer_procedure() duplicated what main.py's
      _send_approach_procedures() + _run_chart_decision() already do,
      correctly, against the real chart catalogue.
    * _TYPE_KEYS / _type_key() were only used by the two dead functions
      above. This file's own norm_type()/_TYPE_LABELS already served the
      live vocabulary (see the note on the two competing vocabularies,
      below) — kept, not duplicated.

  CONFIRMED VALUABLE, KEPT AND NOW ACTUALLY WIRED (see info_block_answer()
  below): _INFO_BLOCKS / _slice() / answer_info(). This is the ONLY
  deterministic, zero-LLM path for AD 2.22's known non-approach headings
  (General, Runway in use, Radar Procedures, VFR minima, VFR flights). It is
  strictly SAFER than an LLM-synthesis round-trip for these specific
  headings — nothing is generated, the answer is either a verbatim slice or
  nothing at all — so main.py's subsection-routing tries this FIRST for
  AD 2.22 non-approach queries, falling back to LLM synthesis only when the
  slice finds no matching heading.

TWO COMPETING TYPE VOCABULARIES — RESOLVED, NOT LEFT DUPLICATED
------------------------------------------------------------------
ad222_respond.py's _TYPE_KEYS mapped to LOWERCASE labels ("vor", "ils"),
matching procedures.py's OWN internal _type_match() vocabulary exactly. This
file's norm_type() maps to UPPERCASE labels ("VOR", "ILS"), used for display
("Which approach? Tap: ILS / RNAV / VOR") and for matching against
ChartRef.procedure_type strings like "ILS Approach Chart" (case-insensitive
internally). These serve genuinely different purposes — one is a display/
chart-matching label, the other is procedures.py's own matching key — so
BOTH are kept, but as one clearly-named pair (norm_type for display/charts,
norm_type_for_procedures for procedures.py's own vocabulary) rather than two
separately-named, easily-confused mappings living in two different files.
"""
import re
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Decision:
    action: str                       # send | ask_type | ask_runway | not_found
    charts: List = field(default_factory=list)
    options: List[str] = field(default_factory=list)
    type: Optional[str] = None        # the (already-chosen) type when asking runway
    runway: Optional[str] = None


# procedure_type substring -> short DISPLAY label. Only APPROACH charts
# qualify. Used for: the tappable ILS/RNAV/VOR buttons, and matching a
# pilot's requested type against a chart's own procedure_type string.
_TYPE_LABELS = [("ils", "ILS"), ("rnav", "RNAV"), ("gnss", "RNAV"),
                ("rnp", "RNAV"), ("vor", "VOR"), ("ndb", "NDB")]

# procedures.py's OWN internal vocabulary (from its _type_match()) — kept
# distinct from _TYPE_LABELS because it is a matching KEY into that other
# module's header text ("...based on VOR/DME..."), not a display label.
# Merged in from ad222_respond.py's _TYPE_KEYS, which existed for exactly
# this reason (aligning with procedures._type_match), not duplicated.
_PROCEDURE_TYPE_KEYS = {
    "vor": "vor", "vor/dme": "vor", "vordme": "vor",
    "ils": "ils", "ils/dme": "ils", "loc": "ils", "localiser": "ils",
    "rnav": "rnav", "gnss": "rnav", "rnp": "rnav", "lnav": "rnav", "vnav": "rnav",
    "ndb": "ndb",
}


def approach_label(procedure_type: str) -> Optional[str]:
    """'ILS Approach Chart' -> 'ILS'; non-approach charts -> None."""
    p = (procedure_type or "").lower()
    if "approach" not in p:
        return None
    for key, label in _TYPE_LABELS:
        if key in p:
            return label
    return None


def norm_type(term: str) -> Optional[str]:
    """A requested type term ('vor', 'RNAV (GNSS)', 'ils') -> canonical
    DISPLAY label ('VOR', 'RNAV', 'ILS'). Used for chart matching and the
    tappable-button labels."""
    t = (term or "").lower()
    for key, label in _TYPE_LABELS:
        if key in t:
            return label
    return None


def norm_type_for_procedures(term: str) -> str:
    """A requested type term -> procedures.py's OWN matching key (lowercase:
    'vor'/'ils'/'rnav'/'ndb'). Kept separate from norm_type() because the two
    serve different consumers — this one only ever feeds procedures.extract(),
    never a chart match or a display label."""
    s = (term or "").lower().strip()
    return _PROCEDURE_TYPE_KEYS.get(s, s)


def _rwy_tok(r):
    m = re.match(r"\s*(\d{1,2})\s*([LRC])?", str(r or "").strip(), re.I)
    return (f"{int(m.group(1)):02d}", (m.group(2) or "").upper()) if m else (None, "")


def _rwy_eq(spec, field_val) -> bool:
    """Side-aware: '18L' matches '18L' and combined '18L/36R', not '18R'. If either
    side is unspecified, match on heading only."""
    sn, ss = _rwy_tok(spec)
    if not sn or not field_val:
        return not sn            # no runway specified -> matches anything
    for part in re.split(r"[/,]", str(field_val)):
        fn, fs = _rwy_tok(part)
        if fn == sn and not (ss and fs and ss != fs):
            return True
    return False


def _rwy_display(field_val) -> str:
    return re.sub(r"\s+", "", str(field_val or "")).upper()


def decide(charts, specified_type: str = "", specified_runway: str = "") -> Decision:
    """Given all of an aerodrome's charts and whatever the pilot already
    specified, decide whether to send a plate or ask ONE clarifying question.
    Options are always derived from the real catalogue — never invented — so
    a pilot can't be offered an approach that doesn't exist.

    Decision.action is one of:
      "send"        -> Decision.charts holds the plate(s) to send
      "ask_type"    -> Decision.options holds the distinct approach TYPES to offer
      "ask_runway"  -> Decision.options holds the distinct RUNWAYS to offer (for a type)
      "not_found"   -> nothing matched

    Rules:
      • operate ONLY over approach charts (ILS/VOR/RNAV/NDB) — clarification is for
        "approach plate" requests; other chart kinds go the normal route;
      • apply what the pilot specified (type and/or runway) first;
      • exactly one match  -> send it (this is what stops us asking pointless questions);
      • several, differing on a dimension the pilot DIDN'T give -> ask about that
        dimension only, type before runway;
      • can't narrow further -> send all matches (never guess one silently).
    """
    # keep only approach charts, tagged with their label
    appr = [(c, approach_label(getattr(c, "procedure_type", None))) for c in charts]
    appr = [(c, lbl) for c, lbl in appr if lbl]

    st = norm_type(specified_type) if specified_type else None
    if st:
        appr = [(c, lbl) for c, lbl in appr if lbl == st]
    if specified_runway:
        appr = [(c, lbl) for c, lbl in appr
                if _rwy_eq(specified_runway, getattr(c, "runway", None))]

    if not appr:
        return Decision("not_found")
    if len(appr) == 1:
        return Decision("send", charts=[appr[0][0]])

    types = sorted({lbl for _, lbl in appr})
    if len(types) > 1 and not st:
        return Decision("ask_type", options=types)

    chosen_type = st or (types[0] if len(types) == 1 else None)
    rwys = sorted({_rwy_display(c.runway) for c, _ in appr if getattr(c, "runway", None)})
    if len(rwys) > 1 and not specified_runway:
        return Decision("ask_runway", options=rwys, type=chosen_type)

    # can't narrow further -> send all matches (never silently pick one)
    return Decision("send", charts=[c for c, _ in appr])


# ============================================================================
# AD 2.22 non-approach info-block slicer — merged in from ad222_respond.py.
# The ONLY deterministic, zero-LLM path for these five known headings.
# ============================================================================

# Ordered by specificity; each maps a query intent to its AD 2.22 heading
# anchor and the heading that ends the block. A query must contain ALL of an
# intent's own words (see info_block_answer()) to match that block — this is
# deliberately conservative: a near-miss returns no block, and the caller
# falls back to LLM synthesis over the whole AD 2.22 section rather than
# risk slicing the wrong heading.
# Ordered by specificity; each maps a query intent to its AD 2.22 heading
# anchor. The end boundary is DELIBERATELY GENERIC — the next "2.22.N"
# heading, OR the start of the per-approach procedure sections (procedures.py's
# own header pattern: "Instrument approach procedures for RWY..."), whichever
# comes first — rather than a hardcoded next-section number.
#
# Confirmed a real bug in the original hardcoded version (inherited from
# ad222_respond.py): its VFR-minima entry stopped only at a literal "2.22.7",
# assuming every aerodrome numbers what follows VFR minima as section 7. On
# an aerodrome using a different numbering, the slice bled straight through
# into the entire Instrument Approach Procedures section — Holding, Letdown,
# and Missed Approach text all leaking into what should have been a short
# VFR-minima answer. The generic boundary below is robust to any aerodrome's
# own numbering scheme.
_NEXT_HEADING_RE = (
    r'2\.22\.\d+(?:\.\d+)*\s+[A-Z]|'
    r'Instrument\s+approach\s+procedures?\s+for\s+RWY'
)
_INFO_BLOCKS = [
    ("runway in use", r'2\.22\.2\s+Runway\s+in\s+use', _NEXT_HEADING_RE),
    ("general", r'2\.22\.1\s+General', _NEXT_HEADING_RE),
    ("radar", r'2\.22\.\d+\s+Radar\s+Procedures', _NEXT_HEADING_RE),
    ("vfr minima", r'2\.22\.\d+(?:\.\d+)?\s+VFR\s+weather\s+minima', _NEXT_HEADING_RE),
    ("vfr", r'2\.22\.\d+\s+Procedures\s+for\s+VFR\s+flights', _NEXT_HEADING_RE),
]


def _slice(text, start_re, end_re):
    """Cut the source text between its own heading and the next one,
    stripping page furniture (headers, AIRAC stamps, section numbers) —
    never rewording. Returns None if the start heading isn't found."""
    m = re.search(start_re, text, re.IGNORECASE)
    if not m:
        return None
    rest = text[m.end():]
    e = re.search(end_re, rest, re.IGNORECASE)
    body = rest[:e.start()] if e else rest
    body = re.sub(r'AD\s*2-DN[A-Z]{2}-\d+|NIGERIAN AIRSPACE MANAGEMENT AGENCY|'
                  r'AIRAC\s+AMDT[^\n]*', '', body, flags=re.IGNORECASE)
    body = re.sub(r'\b2\.22(?:\.\d+)+\b', '', body)
    body = re.sub(r'[ \t]{2,}', ' ', body)
    body = re.sub(r'\n{2,}', '\n', body)
    return body.strip(" ;:.\n\u2022")


def info_block_answer(ad222_text: str, query: str) -> Optional[str]:
    """Deterministic, zero-LLM answer for one of AD 2.22's known non-approach
    headings (General, Runway in use, Radar Procedures, VFR minima, VFR
    flights). Returns the verbatim block text, or None if the query doesn't
    clearly name one of these headings OR that heading isn't present in this
    aerodrome's own AD 2.22 text — in either case the caller falls back to
    LLM synthesis over the whole section, never a guess at which heading was
    meant.

    This is the SAFEST possible answer for these specific headings: nothing
    is generated, so there is no hallucination surface at all — the text is
    either the source's own words, verbatim, or this returns None."""
    q = (query or "").lower()
    for intent, start_re, end_re in _INFO_BLOCKS:
        if all(tok in q for tok in intent.split()):
            body = _slice(ad222_text, start_re, end_re)
            if body:
                return body
    return None
