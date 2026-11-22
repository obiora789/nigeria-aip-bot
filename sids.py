"""
sids.py — Standard Instrument Departures, chosen like approaches.

Pure and deterministic: it reads the PUBLISHED AD 2.22 text, never the
pilot's wording. Whether a question is about a SID is the model's decision
(schemas.sid_request); this module only knows what each aerodrome publishes.

Why (reported live 2026-11-21): "SID Lagos" dumped several coding tables at
once, one of them ERSED 1M — a STAR for RWY 18L, not a SID — while an approach
question asks the type, then the runway, and sends one approach. SIDs now work
the same way: ask the runway, then the SID, then send that one SID.

WHAT IT TRUSTS, AND WHAT IT DOES NOT
  * The catalogue (runway -> SIDs) comes from AD 2.22's own headings:
        2.22.7.1.4 ILLUSTRATIVE CODING SID RW18R
        2.22.7.1.4.1 IBA 1Q (IBA1Q)
        2.22.7.1.4.2 PISBA 1Q (PISB1Q)
    Same layout at all six aerodromes that publish PBN SIDs (DNAA, DNBY,
    DNKN, DNKS, DNMM, DNPO), checked on ad222_by_icao.json.
  * The coding TABLES are never shown per SID. In the stored text they are
    flattened out of order: under "SID RW18R / IBA 1Q" the first rows
    (070 TF OVNIK, 080 TF EDKUV) belong to a STAR. A table shown under a SID's
    name could be another procedure's waypoints — the misattribution this
    project exists to prevent.
  * The FORMAL DESCRIPTION is shown only when it proves itself: it must start
    with the SID's own name, occur exactly once, and end at the SID's own
    end fix (every SID is named after it: "IBA 1Q ... then 047°M to IBA",
    abbreviated "... - LAG-IBA"). A slice cut short by a page break, or
    running into the next row, does not end at that fix and is not shown.
    Otherwise the pilot gets the chart and a pointer, as for approaches.
"""
import re
from dataclasses import dataclass, field
from typing import List, Optional

from clarify import _rwy_eq, _rwy_display


@dataclass(frozen=True)
class Sid:
    runway: str          # as published, compacted: "18R", "06", "13"
    name: str            # as published: "IBA 1Q"
    code: str            # the coded designator: "IBA1Q" ("" if not given)

    @property
    def fix(self) -> str:
        """The end fix the SID is named after: 'IBA 1Q' -> 'IBA'."""
        return self.name.split()[0] if self.name else ""


@dataclass
class SidDecision:
    action: str                        # send | ask_runway | ask_sid | no_runway | none
    sids: List[Sid] = field(default_factory=list)
    options: List[str] = field(default_factory=list)
    runway: Optional[str] = None


_HEADING = re.compile(r"^[ \t]*(2\.22(?:\.\d+)+)[ \t]+(\S.*?)[ \t]*$", re.M)
# A procedure designator: fix (2-5 letters), version digit, route letter,
# then optionally the coded form in brackets. Read on the heading with spaces
# removed, because the stored text sometimes splits words ("I BA 1Q").
_DESIGNATOR = re.compile(r"^([A-Z]{2,5})(\d)([A-Z])(?:\(([A-Z0-9]+)\))?$")
_SID_PARENT_RUNWAY = re.compile(r"SID.*?RWY?(\d{2}[LRC]?)")
_FURNITURE = re.compile(
    r"^\s*(?:\d{1,2}\s+[A-Z]{3}\s+\d{2}\s*$|AIRAC\s+AMDT|AD\s*2-DN[A-Z]{2}-\d+|"
    r"NIGERIA\s+AIP|NIGERIAN\s+AIRSPACE|(?:SID|STAR)\s+Formal\s+Description|"
    r"Serial\b|2\.22(?:\.\d+)+\s)", re.I)


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s or "").upper()


def _clean(text: str) -> str:
    """Extraction artifacts only: CR, and the invisible hyphens the PDF puts
    inside words ("BUD\ufffeSI", "Fly\ufffeover"). No wording changes."""
    return (text or "").replace("\r", "").replace("\ufffe", "").replace("\u00ad", "")


def _row_start(lines: List[str], sid: "Sid") -> Optional[int]:
    """Index of the SID's own description row ("IBA 1Q Climb on course ..."),
    or None unless there is exactly one."""
    target = _squash(sid.name)
    starts = [i for i, line in enumerate(lines)
              if _squash(line).startswith(target)
              and len(_squash(line)) > len(target) + 8
              and not _HEADING.match(line)
              and "(" + sid.code + ")" not in _squash(line)]
    return starts[0] if len(starts) == 1 else None


_COURSE = re.compile(r"course\s*(\d{3})\s*(?:°|o|º)?\s*M", re.I)


def _contradicts_runway(row: str, runway: str) -> bool:
    """True when the row's first climb course is more than 45° off the
    runway's own direction (RWY 21 -> 210°). Port Harcourt publishes AKDIT 2S
    under 'SID RW21' while its description climbs on 029°M (RWY 03); such a
    SID is left out rather than offered behind the wrong runway."""
    m = _COURSE.search(row or "")
    num = re.match(r"(\d{2})", runway or "")
    if not m or not num:
        return False
    diff = abs(int(m.group(1)) - int(num.group(1)) * 10) % 360
    return min(diff, 360 - diff) > 45


def catalogue(ad222: str, contradicted: Optional[List["Sid"]] = None) -> List[Sid]:
    """Every SID the text publishes, in published order, from its headings.
    Empty when the aerodrome publishes no PBN SID headings (Vannie then keeps
    its existing behaviour rather than claim there is none). A SID whose own
    description contradicts its heading's runway is left out, and added to
    `contradicted` when a list is passed (check_sids.py reports them)."""
    ad222 = _clean(ad222)
    lines = ad222.split("\n")
    heads = [(m.group(1), m.group(2)) for m in _HEADING.finditer(ad222)]
    out: List[Sid] = []
    for i, (num, title) in enumerate(heads):
        m = _SID_PARENT_RUNWAY.search(_squash(title))
        if not m or "STAR" in _squash(title):
            continue
        rwy = m.group(1)
        for num2, title2 in heads[i + 1:]:
            if not num2.startswith(num + "."):
                break
            if num2.count(".") != num.count(".") + 1:
                continue
            d = _DESIGNATOR.match(_squash(title2))
            if not d:
                continue                       # "Description of PBN Procedures"
            fix, ver, route, code = d.groups()
            sid = Sid(runway=rwy, name=f"{fix} {ver}{route}", code=code or "")
            if sid in out:
                continue
            row = _row_start(lines, sid)
            if row is not None and _contradicts_runway(lines[row], rwy):
                if contradicted is not None:
                    contradicted.append(sid)
                continue
            out.append(sid)
    return out


def _name_matches(asked: str, sid: Sid) -> bool:
    a = _squash(asked)
    if not a:
        return True
    return a in (_squash(sid.name), sid.code) or a == sid.fix


def runways(sids: List[Sid]) -> List[str]:
    return sorted({s.runway for s in sids})


def decide(sids: List[Sid], runway: str = "", name: str = "") -> SidDecision:
    """Mirror of clarify.decide for SIDs: runway first, then the SID.
    Options always come from the published catalogue."""
    if not sids:
        return SidDecision("none")
    pool = list(sids)
    if runway:
        pool = [s for s in pool if _rwy_eq(runway, s.runway)]
        if not pool:
            return SidDecision("no_runway", options=runways(sids),
                               runway=_rwy_display(runway))
    if name:
        named = [s for s in pool if _name_matches(name, s)]
        if named:
            pool = named
    if len(pool) == 1:
        return SidDecision("send", sids=pool)
    rws = runways(pool)
    if len(rws) > 1:
        return SidDecision("ask_runway", options=rws)
    return SidDecision("ask_sid", options=[s.name for s in pool], runway=rws[0])


def description(ad222: str, sid: Sid) -> Optional[str]:
    """The SID's published formal (and abbreviated) description, verbatim, or
    None unless it proves it belongs to this SID (see module docstring)."""
    lines = _clean(ad222).split("\n")
    i = _row_start(lines, sid)
    if i is None:
        return None
    body = [lines[i].strip()]
    for line in lines[i + 1:]:
        if not line.strip() or _FURNITURE.match(line):
            break
        sq = _squash(line)
        if re.match(r"^[A-Z]{2,5}\d[A-Z][A-Z]", sq):
            break                               # the next SID's row
        body.append(line.strip())
    out = re.sub(r"[ \t]+", " ", "\n".join(body)).strip()
    # it must END at the SID's own fix; a trailing constraint "[M296]" or
    # punctuation after the fix is allowed
    tail = re.sub(r"(\s*\[[^\]]*\])+\s*$", "", out.upper())
    tail = re.sub(r"[^A-Z0-9]+$", "", tail)
    if not tail.endswith(sid.fix) or not (30 <= len(out) <= 900):
        return None
    return out
