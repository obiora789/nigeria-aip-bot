"""
ad217_extractor.py — AD 2.17 AIR TRAFFIC SERVICES AIRSPACE.

Seventeenth Layer 2 subsection extractor. Tabular kind, one record per
declared item — canonical-label pattern (like AD 2.3/2.4/2.9), since field
COUNT genuinely varies: confirmed across all 36, a clean 20-vs-16 split
between aerodromes that publish "Hours of applicability (or activation)"
as its own field and those that don't (going straight from Transition
altitude to Remarks). "Vertical limit" vs "Vertical limits" is the same
field, a spelling variant (18+17=35, matching every other universal field's
count) — not two different concepts.

A REAL, DISTINCT BUG FOUND HERE — not chrome, not a spacing/font quirk, but
a genuine STANDARD BOILERPLATE FOOTNOTE bleeding into structured data:
confirmed on 11 of 36 aerodromes, a fixed ICAO Annex 11 disclaimer about
coordinate-transformation accuracy ("Note: An asterisk (*) will be used to
identify those published geographical co-ordinates which have been
transformed into WGS-84 co-ordinates but whose accuracy of original field
work may not meet the requirements in ICAO Annex 11, Chapter 2.") appears
as two continuation lines directly after the real "Remarks" row. Confirmed
directly on DNAN: the real content is cleanly "7 Remarks Transition level:
FL 50" on its own line — the Note text is two SEPARATE, WIDE, page-spanning
lines after it. Because they're prose, not real two-column data, the
gutter-splitter (correctly designed for genuine label|value rows) forces
them into label/value columns and their words get interleaved with the
real remark during reading-order reconstruction, producing a scrambled
mess like "Transition level: FL 50 those published geographical
co-ordinates which have been transformed..." — genuinely unreadable, not
merely untidy.

FIX: filter out any line starting with "Note:" (and its continuation,
tracked until the next real numbered row) BEFORE the segment's lines ever
reach parse_key_value_rows's gutter calculation — this is subsection-
specific boilerplate, not universal page chrome, so it's handled here
rather than in segment_page.py's shared chrome filter.
"""
import re

from extractor_base import SubsectionExtractor, ExtractResult, ValidationIssue

CANONICAL_FIELDS = [
    (re.compile(r'^Designation and lateral limits', re.I), "designation_lateral_limits"),
    (re.compile(r'^Vertical limits?', re.I), "vertical_limits"),
    (re.compile(r'^Airspace classification', re.I), "airspace_classification"),
    (re.compile(r'^ATS unit call ?sign', re.I), "ats_unit_callsign"),
    (re.compile(r'^Transition altitude', re.I), "transition_altitude"),
    (re.compile(r'^Hours of applicability', re.I), "hours_of_applicability"),
    (re.compile(r'^Remarks?', re.I), "remarks"),
]

NOTE_START_RE = re.compile(r'^Note:', re.I)
ROW_START_RE = re.compile(r'^\d\s+[A-Z]')


def _canonicalize(label):
    for pattern, key in CANONICAL_FIELDS:
        if pattern.match(label):
            return key
    return None


class AD217Extractor(SubsectionExtractor):
    subsection = "2.17"
    kind = "tabular"

    def extract(self, icao: str, segments: list) -> ExtractResult:
        from segment_page import _line_groups
        all_words = self.combine_segment_words(segments)
        lines = _line_groups(all_words)
        warnings = []

        # Strip the standard ICAO Annex 11 boilerplate note before it ever
        # reaches gutter-based column splitting — confirmed necessary (see
        # module docstring), not optional cleanup.
        filtered_words = []
        in_note = False
        for line in lines:
            text = " ".join(w[4] for w in line)
            if NOTE_START_RE.match(text):
                in_note = True
                continue
            if in_note:
                if ROW_START_RE.match(text):
                    in_note = False
                else:
                    continue
            filtered_words.extend(line)

        pairs = self.parse_key_value_rows(filtered_words)

        records = []
        seen_keys = set()
        for raw_label, raw_value in pairs:
            label = self.clean_text(raw_label)
            value = self.clean_text(raw_value)
            key = _canonicalize(label)
            if key is None:
                warnings.append(f"unrecognized field label (new in this AIRAC cycle?): {label!r}")
            elif key in seen_keys:
                warnings.append(f"duplicate canonical field {key!r} on one page "
                                 f"(label {label!r}) — check for a mislabeled row")
            seen_keys.add(key)
            records.append({
                "icao": icao, "field": key, "raw_label": label, "detail": value or None,
            })

        embed_text = f"{icao} AD 2.17 ATS airspace: " + "; ".join(
            f"{r['raw_label']}={r['detail']}" for r in records)

        return ExtractResult(
            icao=icao, subsection=self.subsection, kind=self.kind,
            records=records, text="", embed_text=embed_text, warnings=warnings,
        )

    def validate(self, result: ExtractResult) -> list:
        issues = []
        if not result.records:
            issues.append(ValidationIssue("error", "records",
                                          f"{result.icao}: no AD 2.17 records produced"))
            return issues
        for rec in result.records:
            if rec["field"] is None:
                issues.append(ValidationIssue(
                    "error", "field",
                    f"{result.icao}: unrecognized field label {rec['raw_label']!r}"))
        return issues
