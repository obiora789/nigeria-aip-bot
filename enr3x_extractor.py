#!/usr/bin/env python3
"""
enr3x_extractor.py — ENR 3.1 / 3.2 ATS routes, by BAND SLICING.

WHY THIS IS NOT A LINE PARSER
-----------------------------
ENR 3.x is a seven-column table whose columns interleave when the page is
flattened to text. A line-based parser cannot tell the point-name column from
the remarks column, and three successive attempts to do so with heuristics each
produced a FALSE POINT that then absorbed the next point's coordinate row:

    USKIP  read out of the remark "LUKRO - USKIP -", given XUVLA's position
    LUKRO  read out of a wrapped remark
    ARDEX  published at two different positions within one route

A point shown at another point's position is the misattribution this project
exists to prevent, and each fix created the next instance.

The cause was reading a TWO-DIMENSIONAL table as a one-dimensional line
sequence. The fix is to stop doing that.

THE BAND
--------
Measured on the real pages, the columns sit at fixed x-positions:

    x0  83-193   Route designator / Name of significant points / Coordinates
    x0 214-268   Track magnetic, VOR RDL, DIST (NM)
    x0 279-358   Upper/Lower limit, Airspace classification
    x0 365-389   Lateral limits
    x0 396-437   Direction of cruising levels
    x0 447-493   Navigation accuracy
    x0 506-566   REMARKS  <- the column that produced every false point

Everything needed — designator, ordered point names, coordinates — is in the
FIRST column. Slicing x0 < _COL1_MAX excludes remarks outright, so a remark can
no longer be mistaken for a point at all. That is a structural guarantee, not a
better heuristic.

Same approach segment_page.py uses for AD 2.22, applied to columns rather than
subsection bands.

POINT-SEQUENCE DESIGN
---------------------
A route record is an ORDERED LIST OF POINT NAMES. Coordinates already exist per
point in ENR 4.4 (214, validated), so this section answers "what is on UT467,
in order" and leaves "where is POLTO" to 4.4. Coordinates are still captured
from the route's own row for cross-checking — never as the authoritative
position.

ENR 3.3 IS NOT HANDLED HERE
---------------------------
It is not a route table. It publishes Free Route Airspace DCT combinations
("KELAK DCT POSIB DCT GURAP DCT IBA DCT POLTO") with time availability — a
different entity with no route designator. Treating it as a route produced one
fake route called "H24" with 98 points. It needs its own extractor.
"""
import re

from extractor_base import ExtractResult, SubsectionExtractor, ValidationIssue

# Column 1's right edge. Column 1 ends at x1~193, column 2 begins at x0~214;
# 200 sits in the gutter between them.
_COL1_MAX = 200.0
# The table body starts below the column-header block (headers end at top~185).
_BODY_TOP = 185.0
# Visual-line grouping tolerance, in points.
_YTOL = 3.0

_ROUTE_HDR_RE = re.compile(
    r"^([A-Z]?/?U?[A-Z]\d{2,3}[A-Z]?)\s*(\((?:RNAV|RNP)[^)]*\))?\s*$")

_POINT_NAME_RE = re.compile(r"^([A-Z]{5})$")
_NAVAID_NAME_RE = re.compile(
    r"^(.{2,50}?\b(?:VOR|DVOR|DME|NDB|TACAN)(?:/DME)?)\s*(?:\(([A-Z]{2,4})\))?$",
    re.I)
_COORD_RE = re.compile(r"(\d{6}(?:\.\d+)?[NS])\s*(\d{7}(?:\.\d+)?[EW])")

# Column 2's values bleed into the right-hand edge of column 1 on some pages
# ("RALUX 90 NM", "092852N 0033044E 099"). They are track, distance and
# tolerance figures — never part of a name or a coordinate — so they are
# stripped rather than parsed.
_COL2_BLEED_RE = re.compile(
    r"\s*(?:\d{1,3}\s*\u00b0|\+/-\s*\d+\s*NM|\b\d{1,3}\s*NM\b)\s*")

_CHROME_RE = re.compile(
    r"^(?:ENR\s*3\.\d\s*-?\s*\d*|NIGERIA AIP|NIGERIAN AIRSPACE.*|"
    r"AIRAC\s+AMDT.*|\d{2}\s+[A-Z]{3}\s+\d{2}|[\d\s]{1,10}|"
    r"Route\s+designator.*|Name\s+of\s+significant.*|Coordinates|"
    r"RCP/RSP.*|\(RNP/RNAV\))\s*$", re.I)


def column1_lines(page) -> list:
    """The first column of one pdfplumber page, as visual lines in reading
    order. Selecting on x-position is what keeps remarks out of the data."""
    words = [w for w in page.extract_words()
             if w["x0"] < _COL1_MAX and w["top"] > _BODY_TOP]
    rows = {}
    for w in words:
        rows.setdefault(round(w["top"] / _YTOL), []).append(w)
    out = []
    for key in sorted(rows):
        text = " ".join(w["text"] for w in sorted(rows[key], key=lambda z: z["x0"]))
        text = _COL2_BLEED_RE.sub(" ", text).strip()
        if text and not _CHROME_RE.match(text):
            out.append(text)
    return out


class ENR3XExtractor(SubsectionExtractor):
    """ENR 3.1 / 3.2 — one record per ATS route, holding its point sequence."""

    subsection = "3.1"
    kind = "tabular"

    def __init__(self, subsection="3.1"):
        self.subsection = subsection

    def extract_from_lines(self, lines, scope_id="ENR") -> ExtractResult:
        warnings = []
        records, by_id = [], {}
        current = None
        pending = None

        for ln in lines:
            hm = _ROUTE_HDR_RE.match(ln)
            if hm:
                designator = hm.group(1)
                nav_spec = (hm.group(2) or "").strip("() ") or None
                if designator in by_id:
                    # A route continues across pages. MERGE rather than
                    # overwrite: dropping the continuation truncates the route.
                    current = by_id[designator]
                    warnings.append(f"{designator}: continuation block merged")
                else:
                    current = {"scope_kind": "ENR_ROUTE", "scope_id": designator,
                               "navigation_spec": nav_spec, "points": []}
                    by_id[designator] = current
                    records.append(current)
                pending = None
                continue

            if current is None:
                continue

            cm = _COORD_RE.search(ln)
            if cm and pending:
                # A pending name is confirmed ONLY by a coordinate line.
                pending["coordinates"] = f"{cm.group(1)} {cm.group(2)}"
                if not (current["points"]
                        and current["points"][-1]["name"] == pending["name"]):
                    current["points"].append(pending)
                pending = None
                continue
            if cm:
                continue                # a coordinate with no name before it

            pm = _POINT_NAME_RE.match(ln)
            if pm:
                pending = {"name": pm.group(1), "kind": "significant_point",
                           "ident": None}
                continue
            nm = _NAVAID_NAME_RE.match(ln)
            if nm:
                pending = {"name": nm.group(1).strip(), "kind": "navaid",
                           "ident": nm.group(2)}
                continue

        for rec in records:
            rec["point_sequence"] = " \u2192 ".join(p["name"] for p in rec["points"])
            rec["point_count"] = len(rec["points"])

        return ExtractResult(
            icao=scope_id, subsection=self.subsection, kind=self.kind,
            scope_kind="ENR_ROUTE", records=records,
            embed_text="; ".join(
                f"{r['scope_id']} ATS route via {r['point_sequence']}"
                for r in records),
            warnings=warnings)

    def extract(self, scope_id: str, segments: list) -> ExtractResult:
        """`segments` is a list of column-1 line lists, one per page."""
        lines = []
        for seg in segments or []:
            lines.extend(seg if isinstance(seg, list) else [seg])
        return self.extract_from_lines(lines, scope_id)

    def validate(self, result: ExtractResult) -> list:
        """A route needs two points to define a track, and a point may not be
        published at two positions within one route — that is a false name
        having absorbed another point's row, which the band slice exists to
        prevent."""
        issues = []
        if not result.records:
            issues.append(ValidationIssue("error", "records",
                                          f"ENR {self.subsection}: no routes"))
            return issues
        for rec in result.records:
            rid = rec.get("scope_id")
            if rec.get("point_count", 0) < 2:
                issues.append(ValidationIssue(
                    "error", rid,
                    f"{rid}: only {rec.get('point_count')} point — a route "
                    f"needs at least two to define a track"))
            positions = {}
            for p in rec.get("points", []):
                if not p.get("coordinates"):
                    issues.append(ValidationIssue(
                        "error", rid,
                        f"{rid}: point {p.get('name')!r} has no coordinates"))
                    continue
                prior = positions.get(p["name"])
                if prior and prior != p["coordinates"]:
                    issues.append(ValidationIssue(
                        "error", rid,
                        f"{rid}: {p['name']} appears at TWO positions "
                        f"({prior} and {p['coordinates']}) — a false name has "
                        f"absorbed another point's row"))
                positions[p["name"]] = p["coordinates"]
        return issues
