"""
ad211_extractor.py — AD 2.11 METEOROLOGICAL INFORMATION PROVIDED.

Eleventh Layer 2 subsection extractor. Tabular kind, one record per
aerodrome, POSITIONAL (fixed 10-field structure) — REBUILT from an earlier
text-only version after a user directly challenged that decision with real
evidence (DNKT's page rendering as a clean, well-organized table) and the
challenge turned out to be right.

WHY THIS WAS TEXT-ONLY BEFORE, AND WHY THAT WAS WRONG:
The original version gave up on structured extraction after finding short
values ("NIMET", "H24", "Kano", "English") bleeding into label text across
many aerodromes, and concluded the table's compound-field layout was too
unreliable to parse safely. That conclusion was PREMATURE — the real cause
was a genuine, fixable bug in the SHARED gutter-detection logic
(SubsectionExtractor.parse_key_value_rows), not a property of this
subsection's data. Confirmed directly on DNMK: its AD 2.11 spans two pages
with genuinely different column layouts (page 1's values start at x0=266,
page 2's at x0=309). The old gutter detection computed ONE gutter across
the combined multi-page word set (297.6) — high enough that every value on
page 1 fell on the wrong side, misclassified as label text. Fixed in
parse_key_value_rows by computing the gutter separately per page (see
extractor_base.py). Re-tested DNKT (single-page, always worked) and DNMK
(multi-page, previously broken) directly after the fix: both now parse
perfectly, including every compound field, before this extractor was
rebuilt on top of it. Re-validated all NINE other subsections that share
this same gutter logic (2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.15, 2.16,
2.17) for regressions — all clean, including every previously-confirmed
anomaly case.

EVIDENCE gathered fresh across all 36 with the fixed logic: exactly 10
fields, zero count variance (better than most subsections built so far).
Four fields are genuinely compound (two related values share one numbered
row: Hours of service/MET Office outside hours; Office responsible for TAF
preparation/Period of validity; Type of landing forecast (or "Trend
forecast" — a real terminology variant, same position)/Interval of
issuance; Flight documentation/Language(s) used) — kept as one combined
text value each, since the source doesn't reliably separate them further
and forcing a split risks re-introducing exactly the kind of misattribution
this project exists to prevent.
"""
from extractor_base import SubsectionExtractor, ExtractResult, ValidationIssue

FIELD_ORDER = [
    "associated_met_office",
    "hours_of_service",          # compound: hours + outside-hours coverage
    "taf_preparation",           # compound: office + period of validity
    "landing_forecast",          # compound: forecast type + interval
    "briefing_consultation",
    "flight_documentation",      # compound: documentation + language(s)
    "charts_and_info",
    "supplementary_equipment",
    "ats_units",
    "additional_information",
]


class AD211Extractor(SubsectionExtractor):
    subsection = "2.11"
    kind = "tabular"

    def extract(self, icao: str, segments: list) -> ExtractResult:
        all_words = self.combine_segment_words(segments)
        pairs = self.parse_key_value_rows(all_words)
        warnings = []

        values = {}
        for i, field_name in enumerate(FIELD_ORDER):
            raw = pairs[i][1] if i < len(pairs) else None
            values[field_name] = self.clean_text(raw) if raw else raw
        if len(pairs) != 10:
            warnings.append(f"expected 10 fields, found {len(pairs)}")

        record = {"icao": icao, **values}
        embed_text = f"{icao} MET: {values['associated_met_office']}"

        return ExtractResult(
            icao=icao, subsection=self.subsection, kind=self.kind,
            records=[record], text="", embed_text=embed_text, warnings=warnings,
        )

    def validate(self, result: ExtractResult) -> list:
        issues = []
        if not result.records:
            issues.append(ValidationIssue("error", "records", "no record produced"))
            return issues
        # No field here is safety-critical numeric data — this is office
        # hours, contact/briefing info, forecast availability. Legitimately
        # blank in the source at times (confirmed directly: DNMK's "Office
        # responsible for TAF preparation" field is genuinely empty on the
        # page, not a parsing gap). Only total extraction failure blocks.
        return issues
