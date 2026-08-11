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
# Longitude is SEVEN digits in this document's format, but GANDA is published
# with six ("092846.26N 003100.60E"). Requiring seven meant GANDA was never
# confirmed as a point, so both combinations containing it came out with one
# point and were dropped entirely — two published direct routings missing from
# the index because of one malformed figure.
#
# Accepting 6-7 captures it VERBATIM, exactly as printed; validate_enr33.py
# flags the short form so it can be raised with NAMA. Silently padding it would
# make this file the source of a coordinate rather than the AIP.
_COORD_RE = re.compile(r"(\d{6}(?:\.\d+)?[NS])\s*(\d{6,7}(?:\.\d+)?[EW])")

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


def column1_lines(page, col_max=None) -> list:
    """The first column of one pdfplumber page, as visual lines in reading
    order. Selecting on x-position is what keeps remarks out of the data.

    `col_max` widens the slice. ENR 3.3 needs it: its availability ("H24") and
    vertical band ("UNL" / "FL 245") are published in columns 2 and 3, so the
    route-table slice excluded them and every combination came out with no
    availability at all — which is the one thing a pilot needs, since it says
    WHEN the direct routing may be flown.

    Widening is safe there because those values are unmistakable — H24, a
    clock range, UNL/GND/FL nnn — and cannot be confused with a point name or a
    coordinate. It is NOT safe for the route table, where the remarks column
    holds point names that look exactly like the real ones."""
    limit = _COL1_MAX if col_max is None else col_max
    words = [w for w in page.extract_words()
             if w["x0"] < limit and w["top"] > _BODY_TOP]
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

            # A POINT NAME IS ANYTHING ELSE IN THIS COLUMN.
            #
            # The band slice guarantees column 1 holds only three things: route
            # designators, point names and coordinates. Designators and
            # coordinates are matched above, so whatever remains IS a name —
            # and the confirmation rule (a coordinate line must follow) discards
            # anything that is not.
            #
            # This replaces two SHAPE patterns: exactly five letters for a
            # significant point, and a "…VOR/DME" suffix for a navaid. Both
            # missed the bare 2-4 letter ident, so UT467 — published as
            # "LAG / 064227.4N 0031938.8E / TEMSA / 064912N 0024442E" — lost
            # LAG and came out as a one-point route, which is not a route.
            #
            # Shape was never the right test here. Position in the column is.
            nm = _NAVAID_NAME_RE.match(ln)
            if nm:
                pending = {"name": nm.group(1).strip(), "kind": "navaid",
                           "ident": nm.group(2)}
                continue
            if len(ln) <= 60:
                pending = {"name": ln.strip(),
                           "kind": ("significant_point"
                                    if _POINT_NAME_RE.match(ln) else "navaid"),
                           "ident": ln.strip() if len(ln.strip()) <= 4 else None}
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


# ---------------------------------------------------------------------------
# ENR 3.3 — Free Route Airspace DCT combinations
# ---------------------------------------------------------------------------
# NOT a route table, which is why it is a separate class. It publishes
# permitted DIRECT combinations —
#     "KELAK DCT POSIB DCT GURAP DCT IBA DCT POLTO"   H24
# — each with its time availability and vertical band, and NO route designator
# at all. Parsing it with the route extractor produced one fake route called
# "H24" holding 98 points, because "H24" was the only thing that looked like a
# designator.
#
# THE IDENTIFIER IS DERIVED, AND THAT IS STATED. The AIP gives these
# combinations no name, so a scope_id has to be constructed. It is built from
# the FIRST and LAST point ("KELAK-POLTO"), which is what a pilot would use to
# describe the routing, with a numeric suffix if two combinations share
# endpoints. Nothing about the derivation is inferred from the data itself —
# the points, coordinates and availability are all verbatim.
_DCT_HDR_RE = re.compile(r"^([A-Z0-9/]{3,}(?:\s+DCT\s+[A-Z0-9/]{3,})+)\s*(H24|\d{4}\s*-\s*\d{4})?\s*$")
_DCT_CONT_RE = re.compile(r"^DCT\s+[A-Z0-9/]{3,}(?:\s+DCT\s+[A-Z0-9/]{3,})*\s*$")
_AVAIL_RE = re.compile(r"^(H24|\d{4}\s*-\s*\d{4})$", re.I)
_LEVEL_RE = re.compile(r"^(UNL|GND|MSL|FL\s?\d{2,3})$", re.I)


def _normalise_longitude(lon: str):
    """(longitude, was_corrected). Pads a six-digit longitude to seven.

    THE ONLY PLACE THIS PROJECT CORRECTS THE DOCUMENT, and it is done on the
    publisher's confirmation rather than on inference.

    ENR 3.3 publishes GANDA as "003100.60E" while ENR 4.4 and ENR 3.2 publish
    the same point as "0031000.60E". Read with DDDMMSS field widths those are
    000°31'00.60"E and 003°10'00.60"E — about 155 NM apart, and the six-digit
    reading falls outside Nigeria altogether, which runs from roughly 2.7°E.
    Two independent sections agree on the seven-digit form, it is consistent
    with GANDA's latitude of 09°28'N, and NAMA confirmed it as correct.

    The seconds field is what is short, so a zero is inserted there. Every
    correction is FLAGGED in the extractor warnings and marked on the record,
    so a reader can always see that the stored value differs from this
    section's printed one."""
    m = re.match(r"^(\d{6})(\.\d+)?([EW])$", (lon or "").strip())
    if not m:
        return lon, False
    digits, frac, hemi = m.group(1), m.group(2) or "", m.group(3)
    # DDD MM SS -> the seconds field carries one digit instead of two.
    padded = f"{digits[:5]}0{digits[5]}"
    return f"{padded}{frac}{hemi}", True


def _dct_point_names(dct: str) -> list:
    """The points the header string itself declares, in order."""
    return [t for t in re.split(r"\s+DCT\s+", (dct or "").strip()) if t]


class ENR33Extractor(SubsectionExtractor):
    """ENR 3.3 — one record per published DCT combination."""

    subsection = "3.3"
    kind = "tabular"

    def extract_from_lines(self, lines, scope_id="ENR") -> ExtractResult:
        warnings = []
        records, seen = [], {}
        current = None

        for ln in lines:
            hm = _DCT_HDR_RE.match(ln)
            if hm:
                current = {"scope_kind": "ENR_FRA_DCT", "dct": hm.group(1).strip(),
                           "availability": (hm.group(2) or "").strip() or None,
                           "points": [], "levels": []}
                records.append(current)
                pending = None
                continue
            if current is None:
                continue
            if _DCT_CONT_RE.match(ln):
                # The combination wrapped to a second line. Dropping it would
                # silently shorten the permitted routing.
                current["dct"] = f"{current['dct']} {ln.strip()}"
                continue
            if _AVAIL_RE.match(ln):
                current["availability"] = current["availability"] or ln.strip()
                continue
            if _LEVEL_RE.match(ln):
                current["levels"].append(ln.strip())
                continue
            cm = _COORD_RE.search(ln)
            if cm and current.get("_pending"):
                _lon, _fixed = _normalise_longitude(cm.group(2))
                if _fixed:
                    warnings.append(
                        f"{current['dct'][:30]}: longitude {cm.group(2)!r} "
                        f"padded to {_lon!r} — six digits in ENR 3.3 against "
                        f"seven in ENR 4.4/3.2, confirmed by NAMA as the error")
                current["points"].append({
                    "name": current.pop("_pending"),
                    "coordinates": f"{cm.group(1)} {_lon}",
                    "longitude_corrected": _fixed or None})
                # The UPPER limit shares a line with the first point's
                # coordinates ("120518N 0143758E UNL") because it sits in the
                # next column at the same height. An anchored level pattern
                # missed it, so every combination reported FL 245 as its upper
                # limit when FL 245 is the LOWER one and the band runs to UNL —
                # understating the airspace by everything above FL 245.
                tail = ln[cm.end():].strip()
                if tail and _LEVEL_RE.match(tail):
                    current["levels"].insert(0, tail)
                continue
            if cm:
                continue
            if len(ln) <= 60:
                # STOP AT THE DECLARED COUNT. The header states exactly which
                # points the combination has ("ARDEX DCT EDUKO" = two), and the
                # rows that follow list them. Without this bound, the FIRST row
                # of the NEXT combination was absorbed before its header line
                # was reached: ARDEX-TAKUM declared ARDEX-EDUKO but carried
                # ARDEX, EDUKO and TAKUM, and two combinations lost their
                # opening point and were dropped for having fewer than two.
                #
                # The count is published, not inferred — it is the number of
                # names in the header string.
                if len(current["points"]) >= len(_dct_point_names(current["dct"])):
                    continue
                current["_pending"] = ln.strip()

        out = []
        for rec in records:
            rec.pop("_pending", None)
            names = [p["name"] for p in rec["points"]]
            if len(names) < 2:
                warnings.append(f"{rec['dct'][:40]}: fewer than two points — skipped")
                continue
            base = f"{names[0]}-{names[-1]}"
            n = seen.get(base, 0)
            seen[base] = n + 1
            rec["scope_id"] = base if not n else f"{base}#{n + 1}"
            rec["point_sequence"] = " \u2192 ".join(names)
            rec["point_count"] = len(names)
            rec["upper_limit"] = rec["levels"][0] if rec["levels"] else None
            rec["lower_limit"] = rec["levels"][1] if len(rec["levels"]) > 1 else None
            rec.pop("levels", None)
            out.append(rec)

        return ExtractResult(
            icao=scope_id, subsection=self.subsection, kind=self.kind,
            scope_kind="ENR_FRA_DCT", records=out,
            embed_text="; ".join(
                f"{r['scope_id']} free route {r['dct']} available {r['availability']}"
                for r in out),
            warnings=warnings)

    def extract(self, scope_id: str, segments: list) -> ExtractResult:
        lines = []
        for seg in segments or []:
            lines.extend(seg if isinstance(seg, list) else [seg])
        return self.extract_from_lines(lines, scope_id)

    def validate(self, result: ExtractResult) -> list:
        """A combination needs its endpoints, its availability and its band —
        a pilot asking whether a direct routing is permitted needs all three."""
        issues = []
        if not result.records:
            issues.append(ValidationIssue("error", "records",
                                          "ENR 3.3 produced no DCT combinations"))
            return issues
        for rec in result.records:
            rid = rec.get("scope_id")
            if rec.get("point_count", 0) < 2:
                issues.append(ValidationIssue("error", rid,
                                              f"{rid}: fewer than two points"))
            if not rec.get("availability"):
                issues.append(ValidationIssue(
                    "error", rid, f"{rid}: no time availability — a pilot cannot "
                                  f"tell when this routing is permitted"))
            for p in rec.get("points", []):
                if not p.get("coordinates"):
                    issues.append(ValidationIssue(
                        "error", rid, f"{rid}: {p['name']!r} has no coordinates"))
        return issues
