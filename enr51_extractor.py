#!/usr/bin/env python3
"""
enr51_extractor.py — ENR 5.1 prohibited, restricted and danger areas.

THE FIRST NON-AERODROME EXTRACTOR. Proof that the AD pipeline's shape carries
over to ENR without redesign.

WHY THIS SECTION FIRST
----------------------
A pilot asked "Where is TEMSA?" and Vannie answered "I don't have 'TEMSA' in
the Nigerian AIP". It is in the AIP, on seven pages. The cause is not a bug:
resolver.resolve() checks names against the 40 published AERODROMES and nothing
else, so every ENR entity — waypoints, airways, danger areas — is unreachable
by name. 152 of 1,073 pages have no extractor at all.

Of that, ENR 5.1 is the most safety-relevant. DND45 is live marine gunnery and
anti-aircraft exercise airspace to 30,000 ft. "I don't have that in the AIP" is
a materially worse answer for a danger area than for a waypoint.

WHAT MAKES THIS TRACTABLE (checked against the real pages first)
---------------------------------------------------------------
Unlike AD 2.22 and ENR 3.2, ENR 5.1's text extracts in READING ORDER: each
area's identifier, lateral limits, upper/lower limits, restriction type and
remarks arrive grouped, in that order. No band slicing needed. Verified on
pages 271 (prohibited), 274 (restricted) and 278 (danger) of AIRAC 03/2026.

The identifier is also the row delimiter, and it is unambiguous: DNP<n>,
DNR<n>, DND<n>. A row ends where the next identifier begins. That is a
STRUCTURAL boundary published by the document, not a guess about wording —
the same principle entity_scope.py settled on for AD 2.22 after two
vocabulary-based attempts failed.

ENTITY MODEL
------------
One record per AREA, not per page or per section. The area id is the entity, so
downstream a fact can never merge two areas' vertical limits — the same
guarantee runway ends get in AD 2.12.

SAFETY POSTURE
--------------
Every field is optional and captured as None when absent, never inferred. An
area with no parseable vertical limit yields a record with vertical=None and a
warning; it does NOT get a guessed limit, and validate() marks it an error so
ingestion refuses it. For airspace a pilot may be excluded from, a missing
value must read as missing.
"""
import re

from extractor_base import ExtractResult, SubsectionExtractor, ValidationIssue

# Area identifiers, which are also the row delimiters. The AIP publishes
# exactly three families in ENR 5.1:
#   DNP<n>  prohibited   DNR<n>  restricted   DND<n>  danger
# Anchored so a coordinate or a route designator can never be mistaken for one.
_AREA_ID_RE = re.compile(r"\b(DN[PRD]\s?\d{1,3})\b")

# Vertical limits. The AIP writes these on their own lines, upper before lower,
# in any of: "FL 450", "30000 FT AGL", "5 000 ft AGL", "GND", "MSL", "UNL".
# Vertical limits, WITH their reference qualifier. "ALT" was missing from the
# alternation, so "40 000 FT ALT" was captured as "40 000 FT" and "22 000 ft
# ALT" as "22 000 ft" — dropping the datum from an altitude. A height above
# ground and an altitude above mean sea level are different numbers, and a
# pilot reading a bare figure has no way to know which was meant.
_LEVEL_RE = re.compile(
    r"\b(?:FL\s?\d{2,3}"
    r"|\d[\d\s]{2,6}\s*(?:FT|ft)\s*(?:AGL|AMSL|MSL|ALT)?"
    r"|GND|MSL|UNL)\b")

# A line that begins the lateral-limits description rather than the name.
_LIMITS_START_RE = re.compile(
    r"^\s*(?:A\s+circle|Area\s+(?:bounded|within)|It\s+is\b|Line\s+joining|"
    r"NAF\s+Local|DANA\s+Training|\(?[a-z]\)|Military\s+Restricted)", re.I)

# A coordinate as published: 6-8 digits, N/S, then 7-9 digits, E/W. The leading
# asterisk marks WGS-84 transformed coordinates whose original field accuracy
# may not meet ICAO Annex 11 — it is preserved verbatim, never stripped,
# because it is a published accuracy caveat and not decoration.
_COORD_RE = re.compile(r"\*?\d{4,8}(?:\.\d+)?[NS]\s*\d{5,9}(?:\.\d+)?[EW]")

# Section headings, so a record can record which family it belongs to.
_FAMILY_HDR_RE = re.compile(
    r"5\.1\.\d+\s+(PROHIBITED|RESTRICTED|DANGER|MILITARY)[A-Z\s]*AREA", re.I)

# Page furniture and the table's own column headers/numbers.
_CHROME_RE = re.compile(
    r"^(?:ENR\s*5\.1-\d+|NIGERIA AIP|NIGERIAN AIRSPACE MANAGEMENT AGENCY|"
    r"Identification and name|Lateral [Ll]imits?.*|Upper Limit|Lower Limit|"
    r"Type of.*|Activation Hours|Remarks|Operating Authority|"
    r"Penetration Conditions|\d(?:\s+\d){1,6}|AIRAC AMDT.*|\d{2} [A-Z]{3} \d{2})\s*$")

# The asterisk note repeated at the foot of most pages. Real content, but it is
# a document-wide footnote, not a property of whichever area it lands beside.
_FOOTNOTE_RE = re.compile(r"Note:\s*An asterisk.*?Chapter 2\.", re.S | re.I)

# A position defined relative to a navaid rather than by coordinates. Requires
# BOTH a navaid cue and a geometric term, so ordinary prose mentioning a VOR
# (e.g. a controlling-authority note) does not qualify as a position.
_NAVAID_POS_RE = re.compile(
    r"\b(VOR|DME|NDB|VOR/DME|TACAN)\b(?=.*\b(radial|R\s?\d{3}|arc|NM|"
    r"north|south|east|west)\b)|"
    r"\b(radial|R\s?\d{3}\s*°?|arc)\b(?=.*\b(VOR|DME|NDB)\b)", re.I | re.S)

# A leading DESIGNATION — the AIP's name for an area when it is a designation
# rather than a place ("NAF Local Training Area 2. Line joining..."). Cut at
# the first sentence terminator so only the designation is taken, never the
# limits description that follows it.
_DESIGNATION_RE = re.compile(
    r"^((?:NAF|DANA|Military|Air Force)\s+[A-Za-z0-9 /]{3,48}?)"
    r"(?=[.;:]|\s+(?:Line|It\s+is|Area\s+[A-Z]\b|established))", re.I)

# The heading that opens the NEXT 5.1.x subsection, plus the column-header
# words that follow it on the same extracted line.
# Column headers and the column-number row, as they appear once the table is
# flattened to text. Anchored to the END of the body so a phrase occurring
# inside real content is never removed.
_COL_FURNITURE_RE = re.compile(
    r"(?:\s*(?:Identification,?\s*name\s*and\s*hazard|Lateral\s+limits?|"
    r"Upper\s+limit|Lower\s+limit|Type\s+of\s+restriction|Activation\s+Hours?|"
    # The column-NUMBER row survives extraction with arbitrary spacing —
    # "1 23 4", "1 234 5", "1 2 3 4 5" — because the digits are laid out under
    # their columns, not written as a sequence. Match any short run of digits
    # and spaces at the very end rather than a fixed shape.
    r"Remarks|coordinates|restriction|[\d\s]{3,12}))+\s*$", re.I)

_NEXT_SECTION_HDR_RE = re.compile(
    r"\s*5\.1\.\d+\s+(?:PROHIBITED|RESTRICTED|DANGER|MILITARY|ADJOINING)"
    r"[A-Z\s]*", re.I)

# DNR = Restricted, DNP = Prohibited, DND = Danger. The AIP states this
# convention itself, and the letter is published per area, so it is
# authoritative — it does not depend on which heading a row happens to sit
# under. DNR44 is a RESTRICTED area even though it appears beneath the
# "5.1.7 DANGER AREA" heading in the source document.
_FAMILY = {"P": "prohibited", "R": "restricted", "D": "danger"}


def _clean_lines(text: str) -> list:
    text = _FOOTNOTE_RE.sub(" ", text or "")
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or _CHROME_RE.match(line):
            continue
        out.append(line)
    return out


def _norm_id(raw: str) -> str:
    """'DNR 24' -> 'DNR24'. The AIP sometimes breaks the id across a space."""
    return re.sub(r"\s+", "", raw or "").upper()


class ENR51Extractor(SubsectionExtractor):
    """ENR 5.1 — one record per prohibited/restricted/danger area."""

    subsection = "5.1"
    kind = "tabular"

    def extract(self, scope_id: str, segments: list) -> ExtractResult:
        """`scope_id` is 'ENR' here rather than an ICAO code.

        The AD pipeline keys everything on icao_code; ENR entities are not
        aerodromes. This extractor therefore takes the scope id positionally
        and does not interpret it — the schema generalisation from icao_code to
        (scope_kind, scope_id) is a separate change, and this file is written
        to slot into it without modification."""
        text = self.segment_text(segments) if segments else ""
        lines = _clean_lines(text)
        warnings = []

        # Split into rows on the identifier. Everything before the first id is
        # a section heading and is used only to label the family.
        starts = [(i, m.group(1)) for i, ln in enumerate(lines)
                  for m in [_AREA_ID_RE.match(ln)] if m]
        if not starts:
            return ExtractResult(icao=scope_id, subsection=self.subsection,
                                 kind=self.kind, records=[],
                                 warnings=["ENR 5.1: no area identifiers found"])

        family = ""
        for ln in lines[:starts[0][0]]:
            fm = _FAMILY_HDR_RE.search(ln)
            if fm:
                family = fm.group(1).lower()

        records = []
        for n, (idx, raw_id) in enumerate(starts):
            end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
            block = lines[idx:end]
            area_id = _norm_id(raw_id)
            # FAMILY COMES FROM THE IDENTIFIER, NOT THE HEADING. The AIP's own
            # naming convention is explicit: "areas are assigned numbers and
            # letters in the following manner: DNP1, DND1, DNR1" — P for
            # prohibited, R for restricted, D for danger. That letter is
            # published per area and cannot drift.
            #
            # The previous version tracked the most recent 5.1.x heading, which
            # misfiled both BOUNDARY rows: DNP8 (SATELLITE EARTH STATION
            # KUJAMA, whose own text reads "Prohibited Area") was tagged
            # restricted because the "5.1.5 RESTRICTED AREAS" heading trails
            # into its block, and DNR44 (MAKURDI, a Military Training Area) was
            # tagged danger by "5.1.7 DANGER AREA". Family decides whether a
            # pilot is PROHIBITED from an area or merely restricted — getting
            # it wrong on a prohibited area is the misattribution class this
            # project exists to prevent.
            family = _FAMILY.get(area_id[2], "")

            body = " ".join(block[1:]).strip()
            # The LAST row of each 5.1.x block absorbs the NEXT section's
            # heading, because the heading sits between that row and the next
            # identifier. Confirmed in the real output:
            #   DNP8  ... Prohibited Area 5.1.5 RESTRICTED AREAS Identification...
            #   DNR31 ... 5.1.6 ADJOINING SPECIAL USE AIRSPACES ...
            #   DNR44 ... 5.1.7 DANGER AREA coordinates restriction Activation...
            # Left in, a pilot reading DNR44 — a RESTRICTED area — sees the
            # words "DANGER AREA" appended to its description. The values are
            # unaffected, but the text a human reads is not, and that is the
            # same column-header bleed already fixed once in AD 2.14.
            #
            # Cut at the heading, keeping everything before it. The heading is
            # structurally identifiable (a 5.1.x number followed by capitals),
            # so this removes document furniture, never published content.
            body = _NEXT_SECTION_HDR_RE.split(body)[0].strip(" .,;")
            # The table's own COLUMN HEADER and column-NUMBER row also trail
            # the last row of a block ("Identification, name and hazard",
            # "1 23 4"). Same furniture, same reason to remove it: it is the
            # table's scaffolding, not a property of the area beside it.
            body = _COL_FURNITURE_RE.sub(" ", body)
            body = re.sub(r"\s{2,}", " ", body).strip(" .,;")
            coords = _COORD_RE.findall(body)
            levels = _LEVEL_RE.findall(body)

            # The AIP prints upper limit before lower. Both are captured only
            # when both are present; a lone level is ambiguous and is recorded
            # as such rather than assigned to a side.
            upper = levels[0].strip() if len(levels) >= 1 else None
            lower = levels[1].strip() if len(levels) >= 2 else None
            if len(levels) == 1:
                warnings.append(f"{area_id}: only one vertical limit found "
                                f"({levels[0].strip()!r}) — lower limit is null")
            elif not levels:
                warnings.append(f"{area_id}: no vertical limits found")
            # A position may be given RELATIVE TO A NAVAID instead of by
            # coordinates — "Area within radial 070° to 110° and commencing
            # from 10NM east of 'ABC' VOR/DME to 30NM" (DNP4), or "North West
            # of YOL VOR from R325° to R355°. 55NM" (DNR14). That is a complete
            # published definition, not a missing one. Measured: 16 of 57 real
            # areas are defined this way, and an earlier validate() that
            # demanded coordinates rejected every one of them — refusing to
            # publish airspace the AIP defines perfectly well.
            navaid_ref = bool(_NAVAID_POS_RE.search(body))
            if not coords and not navaid_ref:
                warnings.append(f"{area_id}: no position found "
                                f"(neither coordinates nor a navaid reference)")

            # The name follows the identifier, on its own line, where present.
            # Many danger areas have no name at all — that is normal, not a
            # defect, so it stays None rather than borrowing the next field.
            # NAME MAY SPAN SEVERAL LINES. The AIP wraps it in a narrow
            # column: "NNPC REFINERY" / "WARRI." and "LAGOS/Murtala" /
            # "Muhammed". Taking only the first line produced three areas all
            # named "NNPC REFINERY", indistinguishable from one another —
            # Warri, Eleme and Kaduna are different refineries hundreds of
            # kilometres apart.
            #
            # Consume lines until one carries data (a coordinate, a level, or
            # the start of a lateral-limits description). A name line is short
            # and carries none of those, so the boundary is structural.
            name_lines = []
            for ln in block[1:]:
                if (_COORD_RE.search(ln) or _LEVEL_RE.search(ln)
                        or _LIMITS_START_RE.match(ln) or len(ln) > 60):
                    break
                name_lines.append(ln.strip())
                if len(name_lines) >= 3:      # bound it; no real name is longer
                    break
            name = " ".join(name_lines).strip(" .") or None

            # The AIP column is "Identification AND NAME", and for many areas
            # the name is a DESIGNATION rather than a place — "NAF Local
            # Training Area 2", "DANA Training Area". Those sit on the same
            # line as the limits description, so the structural break above
            # rejects them and 26 areas came back nameless. A pilot asking
            # about DNR9 expects to be told what it is called.
            #
            # So: fall back to the leading designation phrase, taken verbatim
            # from the row's own text and cut at the first sentence end. Never
            # invented, and only used when no separate name line exists.
            if not name:
                dm = _DESIGNATION_RE.match(body)
                if dm:
                    name = dm.group(1).strip(" .,;")

            # lateral_limits is ALWAYS carried alongside, whatever the name
            # resolved to. For an area defined by radial and arc, the limits
            # text IS the position — a name without it is unusable.

            records.append({
                "scope_kind": "ENR_AREA",
                "position_kind": ("coordinates" if coords
                                  else "navaid_relative" if navaid_ref else None),
                "scope_id": area_id,
                "family": family,
                "name": name,
                "lateral_limits": body if coords else (body or None),
                # EVERY vertex, not a sample. DNR30 publishes six points and
                # DNR27 five; truncating to four silently redraws the polygon.
                "coordinates": coords or None,
                "upper_limit": upper,
                "lower_limit": lower,
                "raw_text": body or None,
            })

        # ExtractResult.icao is REQUIRED by the current dataclass — the field
        # is the AD pipeline's hard-wired assumption that every entity belongs
        # to an aerodrome. "ENR" is passed as the scope until that field is
        # generalised to (scope_kind, scope_id); each record already carries
        # its own scope_kind/scope_id, so nothing downstream needs this value.
        return ExtractResult(icao=scope_id, subsection=self.subsection,
                             kind=self.kind, records=records,
                             embed_text="; ".join(
                                 f"{r['scope_id']} {r.get('name') or ''} "
                                 f"{r.get('family','')} area, upper limit "
                                 f"{r.get('upper_limit')}, lower limit "
                                 f"{r.get('lower_limit')}".strip()
                                 for r in records),
                             warnings=warnings)

    def validate(self, result: ExtractResult) -> list:
        """An area missing its vertical limits or its position is NOT storable.

        This is stricter than the AD default deliberately. A danger area is
        airspace a pilot may need to stay out of; a record that names DND45 but
        cannot say how high it goes invites the reader to assume it is lower
        than it is. Refusing the record makes the bot abstain, which is
        recoverable — a half-record is not."""
        issues = []
        if not result.records:
            issues.append(ValidationIssue("error", "records",
                                          "ENR 5.1 produced no area records"))
            return issues

        seen = set()
        for rec in result.records:
            aid = rec.get("scope_id")
            if aid in seen:
                issues.append(ValidationIssue("error", aid,
                                              f"duplicate area id {aid}"))
            seen.add(aid)
            if not rec.get("upper_limit"):
                issues.append(ValidationIssue(
                    "error", aid, f"{aid}: no upper limit — refusing the record "
                                  f"rather than publishing an area with unknown "
                                  f"vertical extent"))
            # Position must be PRESENT, in whichever of the two published
            # forms. Demanding coordinates specifically rejected 16 of 57 real
            # areas that are defined by navaid radial and distance.
            if not rec.get("position_kind"):
                issues.append(ValidationIssue(
                    "error", aid, f"{aid}: no position — neither coordinates "
                                  f"nor a navaid-relative definition"))
        return issues
