"""
clarify.py — the brain of the chart-clarification feature (pure + deterministic).

Given all of an aerodrome's charts and whatever the pilot already specified, decide
whether to send a plate or ask ONE clarifying question. Options are always derived
from the real catalogue — never invented — so a pilot can't be offered an approach
that doesn't exist. No LLM, no network: unit-testable in isolation.

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


# procedure_type substring -> short label. Only APPROACH charts qualify.
_TYPE_LABELS = [("ils", "ILS"), ("rnav", "RNAV"), ("gnss", "RNAV"),
                ("rnp", "RNAV"), ("vor", "VOR"), ("ndb", "NDB")]


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
    """A requested type term ('vor', 'RNAV (GNSS)', 'ils') -> canonical label."""
    t = (term or "").lower()
    for key, label in _TYPE_LABELS:
        if key in t:
            return label
    return None


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
