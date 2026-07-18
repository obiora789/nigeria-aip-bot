"""
ad24_extractor.py — AD 2.4 HANDLING SERVICES AND FACILITIES.

Fourth Layer 2 subsection extractor. Tabular kind, one record per declared
service (same shape as AD23Extractor) — not one record per aerodrome, since
field count genuinely varies (DNKA's source consecutively numbers only 6
rows, omitting De-icing facilities entirely — confirmed by direct page
inspection, matching the exact pattern already found in AD 2.3).

EVIDENCE, gathered across all 36 standard aerodromes: label text is clean and
consistent here (unlike AD 2.3, no spelling-variant reconciliation needed) —
exactly 7 canonical labels, appearing 36/36 or 35/36 times each. No ordered-
prefix ambiguity like AD 2.3's DNJO case.

TWO REAL LAYER-1/LAYER-2 BUGS FOUND AND FIXED WHILE BUILDING THIS EXTRACTOR
(both in segment_page.py / extractor_base.py, so they benefit every
subsection, not just this one):

1. Page chrome (footer boilerplate, and the next page's own header/date-stamp
   at a page break) had no filtering at all in the new pipeline, unlike the
   older extract_page_text_fixed.py which already solved this. Confirmed
   directly: DNAA's real "7 Remarks NIL" was immediately followed by the
   unfiltered footer "NIGERIAN AIRSPACE MANAGEMENT AGENCY AIRAC AMDT
   03/2026" bleeding into the Remarks value; DNBB's continuation page had its
   own header chrome ("AD 2-DNBB-2", "6 DEC 18", "NIGERIA AIP") glued onto
   whatever segment was still open. Fixed in segment_page.py with substring-
   based chrome stripping (tolerant of verso/recto mirrored header layouts
   that can put multiple chrome fragments on one joined line).

2. The gutter-detection helper (parse_key_value_rows) was including the
   subsection's own title line in its "widest gap" calculation. Confirmed
   directly: DNFB's page-spanning header "DNFB AD 2.4 HANDLING SERVICES AND
   FACILITIES" (continuous from x0=218 to x1=419.7) sat directly through the
   0.30-0.66 width band the gutter scan searches — with the header included,
   every value on the page (short strings like "Not available", "NIL")
   landed on the wrong side of the miscalculated gutter, merging into the
   label. Fixed by restricting the gutter scan to words at or below the
   first row anchor (the header is always above row 1).

Both fixes were re-validated against AD 2.1, 2.2, and 2.3 (36/36 each, no
regression) before this extractor was built on top of them.
"""
import re

from extractor_base import SubsectionExtractor, ExtractResult, ValidationIssue

CANONICAL_SERVICES = [
    (re.compile(r'^Cargo-?handling', re.I), "cargo_handling"),
    (re.compile(r'^Fuel and oil types', re.I), "fuel_oil_types"),
    (re.compile(r'^Fuelling facilities', re.I), "fuelling_facilities"),
    (re.compile(r'^De-?icing', re.I), "de_icing_facilities"),
    (re.compile(r'^Hangar space', re.I), "hangar_space"),
    (re.compile(r'^Repair facilities', re.I), "repair_facilities"),
    (re.compile(r'^Remarks?', re.I), "remarks"),
]


def _canonicalize(label: str):
    for pattern, key in CANONICAL_SERVICES:
        if pattern.match(label):
            return key
    return None


class AD24Extractor(SubsectionExtractor):
    subsection = "2.4"
    kind = "tabular"

    def extract(self, icao: str, segments: list) -> ExtractResult:
        all_words = self.combine_segment_words(segments)
        pairs = self.parse_key_value_rows(all_words)
        warnings = []

        records = []
        seen_keys = set()
        for raw_label, raw_value in pairs:
            label = self.clean_text(raw_label)
            value = self.clean_text(raw_value)
            key = _canonicalize(label)
            if key is None:
                warnings.append(f"unrecognized service label (new in this AIRAC cycle?): {label!r}")
            elif key in seen_keys:
                warnings.append(f"duplicate canonical service {key!r} on one page "
                                 f"(label {label!r}) — check for a mislabeled row")
            seen_keys.add(key)
            records.append({
                "icao": icao,
                "service": key,
                "raw_label": label,
                "detail": value or None,
            })

        embed_text = f"{icao} handling services: " + "; ".join(
            f"{r['raw_label']}={r['detail']}" for r in records)

        return ExtractResult(
            icao=icao, subsection=self.subsection, kind=self.kind,
            records=records, text="", embed_text=embed_text, warnings=warnings,
        )

    def validate(self, result: ExtractResult) -> list:
        issues = []
        if not result.records:
            issues.append(ValidationIssue("error", "records",
                                          f"{result.icao}: no AD 2.4 records produced"))
            return issues
        for rec in result.records:
            if rec["service"] is None:
                issues.append(ValidationIssue(
                    "error", "service",
                    f"{result.icao}: unrecognized service label {rec['raw_label']!r}"))
        return issues
