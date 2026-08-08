#!/usr/bin/env python3
"""
enr44_extractor.py — ENR 4.4 name-code designators for significant points.

THIS IS THE ONE THAT STARTED IT. A pilot asked "Where is TEMSA?" and Vannie
answered "I don't have 'TEMSA' in the Nigerian AIP." TEMSA is published here:
06°49'12"N 002°44'42"E, on airway UT467 at the Kano/Accra FIR boundary.

WHY THIS SECTION IS EASY (verified against the real pages, not assumed)
----------------------------------------------------------------------
Unlike ENR 3.x, this table extracts in reading order — one row per line, in
column order: designator, latitude, longitude, routes, FRA relevance, remarks.
Measured on the real document: 212 of 219 candidate lines parse cleanly with a
single pattern.

The designator is also a perfect key. All 212 are exactly five uppercase
letters, and NONE collides with an ICAO code, a city alias or a VOR ident —
checked against resolver's tables. So a lookup can be exact and cannot be
confused with an aerodrome.

THE SEVEN THAT DO NOT PARSE — ALL ACCOUNTED FOR
-----------------------------------------------
  * 5 are the page footer "AIRAC AMDT 02/2026", which is not a waypoint at all
    and is excluded by the header/footer filter.
  * 2 are REAL DEFECTS IN THE SOURCE: IRNAG and USLUP have a longitude with no
    hemisphere letter ("0073557" rather than "0073557E").

Those two are captured with the longitude EXACTLY as published and flagged, not
silently repaired. Nigeria is entirely east of Greenwich so "E" is inferable —
but inferring it would mean this file, not the AIP, is asserting a coordinate,
and a pilot reading the value would have no way to know. The warning tells the
operator to raise it with NAMA, which is where a document defect belongs.
"""
import re

from extractor_base import ExtractResult, SubsectionExtractor, ValidationIssue

# A row: five-letter designator, then latitude, then longitude, then whatever
# the remaining columns hold. Anchored to the line start so a designator
# appearing INSIDE a remarks column can never open a spurious row.
_ROW_RE = re.compile(
    r"^([A-Z]{5})\s+"
    r"(\d{6}(?:\.\d+)?[NS])\s*"
    r"(\d{7}(?:\.\d+)?[EW])\s*"
    r"(.*)$")

# The same row, but with a longitude missing its hemisphere letter. Two real
# rows are published this way (IRNAG, USLUP). Matched separately so they are
# captured and FLAGGED rather than silently dropped by the strict pattern —
# a waypoint absent from the index is a waypoint a pilot cannot look up.
_ROW_NO_HEMI_RE = re.compile(
    r"^([A-Z]{5})\s+"
    r"(\d{6}(?:\.\d+)?[NS])\s*"
    r"(\d{7}(?:\.\d+)?)(?![NSEW\d])\s*"
    r"(.*)$")

# Page furniture and the table's own column headers.
_CHROME_RE = re.compile(
    r"^(?:ENR\s*4\.4-\d+|NIGERIA AIP|NIGERIAN AIRSPACE MANAGEMENT AGENCY|"
    r"AIRAC\s+AMDT.*|\d{2}\s+[A-Z]{3}\s+\d{2}|Name-code designator.*|"
    r"\d(?:\s+\d){1,6}|Significant points used.*|Legend for FRA.*|"
    r"\([ADEIX]\):.*|are categorized.*|one or more functions.*|"
    r"is shown in column.*)\s*$", re.I)

# FRA relevance is a bracketed function code — A, D, E, I or X — published in
# its own column. It arrives as a bare letter or letters after the routes.
_FRA_RE = re.compile(r"\b([ADEIX](?:\s*[ADEIX])*)\b\s*(.*)$")

# Route designators, so the routes column can be separated from remarks. The
# AIP writes them as "G/UG660", "UT457", "Q/UQ324".
_ROUTE_TOKEN_RE = re.compile(r"[A-Z]?/?U?[A-Z]\d{2,3}[A-Z]?")


def _clean_lines(text: str) -> list:
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or _CHROME_RE.match(line):
            continue
        out.append(line)
    return out


# FRA relevance codes, published in their own column: A (arrival connecting),
# D (departure connecting), E (horizontal entry), I (intermediate), X
# (horizontal exit). A point may carry several — "AI" is arrival + intermediate.
#
# They arrive as bare letters between the routes and the remarks, which makes
# them ambiguous with prose. Anchoring on the KNOWN five-letter alphabet and
# requiring the whole token to consist of them is what separates
# "AI" (FRA codes) from "FIR BDRY" (a remark).
# ...but NOT when the token opens a remark. "IAD FIR BDRY" is a remark
# ("Inside Airspace Delegated" at a FIR boundary) whose first word happens to
# consist only of FRA letters — matching it as three FRA codes stripped the
# word out of the remark and asserted a function the AIP never gave the point.
# 10 of 214 rows were affected.
#
# The distinction is structural, not a vocabulary guess: an FRA code stands
# alone or is followed by a remark that does NOT continue the same word, while
# "IAD" here is the first token of "IAD FIR BDRY". Requiring the FRA token to
# be the whole of what remains, or to be followed by a remark that starts a
# new clause, is unreliable — so instead exclude the known remark openers,
# which the AIP publishes in a closed set (FIR BDRY, IAD FIR BDRY).
_FRA_TOKEN_RE = re.compile(r"^([ADEIX]{1,5})(?=\s|$)")
# Remark prefixes that would otherwise be read as FRA codes.
_REMARK_OPENER_RE = re.compile(r"^(?:IAD|AD)\s+FIR\b", re.I)


def _split_tail(tail: str):
    """Separate the routes column from FRA relevance and remarks.

    Conservative by design: the routes are whatever LOOKS like route
    designators at the head of the tail, and everything after the last one is
    returned verbatim. Nothing is dropped — an unrecognised tail becomes
    remarks in full rather than being discarded.

    FRA IS PARSED EVEN WHEN THERE ARE NO ROUTES. An earlier version returned
    early on a tail with no route token, so OGDIX ("084400N 0075019E I") put
    its FRA code I into REMARKS, and BUDSI's "AI" — arrival + intermediate —
    became the remark "AI". Both are published function codes that tell a pilot
    how the point may be used in Free Route Airspace; filing them as free text
    loses that meaning."""
    tail = (tail or "").strip()
    if not tail:
        return "", "", ""

    tokens = list(_ROUTE_TOKEN_RE.finditer(tail))
    if tokens:
        end = tokens[-1].end()
        routes = tail[:end].strip(" ,;.")
        rest = tail[end:].strip(" ,;.")
    else:
        # No routes column — the tail is FRA and/or remarks. Do NOT return
        # early: that is what mis-filed OGDIX.
        routes, rest = "", tail

    fra = ""
    if not _REMARK_OPENER_RE.match(rest):
        m = _FRA_TOKEN_RE.match(rest)
        if m:
            fra, rest = m.group(1), rest[m.end():].strip(" ,;.")
    return routes, fra, rest


class ENR44Extractor(SubsectionExtractor):
    """ENR 4.4 — one record per significant point."""

    subsection = "4.4"
    kind = "tabular"

    def extract(self, scope_id: str, segments: list) -> ExtractResult:
        text = self.segment_text(segments) if segments else ""
        warnings = []
        records = []
        seen = set()

        for line in _clean_lines(text):
            m = _ROW_RE.match(line)
            missing_hemisphere = False
            if not m:
                m = _ROW_NO_HEMI_RE.match(line)
                missing_hemisphere = bool(m)
            if not m:
                continue

            name, lat, lon, tail = m.group(1), m.group(2), m.group(3), m.group(4)
            if name in seen:
                warnings.append(f"{name}: duplicate designator — second row ignored")
                continue
            seen.add(name)

            if missing_hemisphere:
                # Published without the hemisphere letter. Kept EXACTLY as
                # printed. Nigeria is entirely east of Greenwich so "E" is
                # inferable, but adding it would make this file the source of a
                # coordinate rather than the AIP, and the pilot would have no
                # way to tell. Flag it for the operator to raise with NAMA.
                warnings.append(
                    f"{name}: longitude {lon!r} has no hemisphere letter in the "
                    f"source — captured verbatim, NOT corrected. Query with NAMA.")

            routes, fra, remarks = _split_tail(tail)
            records.append({
                "scope_kind": "ENR_POINT",
                "scope_id": name,
                "latitude": lat,
                "longitude": lon,
                "coordinates": f"{lat} {lon}",
                "routes": routes or None,
                "fra_relevance": fra or None,
                "remarks": remarks or None,
                "coordinate_defect": missing_hemisphere or None,
            })

        return ExtractResult(
            icao=scope_id, subsection=self.subsection, kind=self.kind,
            scope_kind="ENR_POINT", records=records,
            embed_text="; ".join(
                f"{r['scope_id']} significant point at {r['coordinates']}"
                f"{(' on ' + r['routes']) if r['routes'] else ''}"
                for r in records),
            warnings=warnings)

    def validate(self, result: ExtractResult) -> list:
        """A point with no position is not storable.

        The whole purpose of this section is to say WHERE a named point is; a
        record that names one without locating it answers the pilot's question
        with a confident non-answer."""
        issues = []
        if not result.records:
            issues.append(ValidationIssue("error", "records",
                                          "ENR 4.4 produced no points"))
            return issues
        for rec in result.records:
            nm = rec.get("scope_id")
            if not rec.get("latitude") or not rec.get("longitude"):
                issues.append(ValidationIssue(
                    "error", nm, f"{nm}: no coordinates — a significant point "
                                 f"without a position cannot be located"))
            if not re.fullmatch(r"[A-Z]{5}", nm or ""):
                issues.append(ValidationIssue(
                    "error", nm, f"{nm!r}: not a five-letter designator"))
        return issues
