"""
ad23_extractor.py — AD 2.3 OPERATIONAL HOURS.

Third Layer 2 subsection extractor. Tabular kind, but UNLIKE AD 2.1/2.2:
produces MULTIPLE records per aerodrome — one per declared service — not a
single combined record. Field COUNT and LABEL TEXT genuinely vary by
aerodrome; this cannot be extracted by fixed position/column the way AD
2.1/2.2 were.

EVIDENCE, gathered across all 36 standard aerodromes before designing:
35 of 36 aerodromes print the standard 12-row template. DNKA genuinely prints
only 10 rows, consecutively numbered 1-10 (confirmed by direct page
inspection) — "ATS Reporting Office" and "De-icing" are simply absent from
its table, not mis-parsed. This is a real content difference in the source,
and null-over-guess means DNKA's record set for those two services must be
absent, not fabricated as H24/NIL.

LABEL NORMALIZATION — required because the same 12 conceptual services are
spelled differently across aerodromes, confirmed by exact counts summing to
36 in each group (proving they're spelling variants, not different fields):
  "AD Administration" (16) + "Aerodrome Operator" (14) + "AD Operator" (6) = 36
  "ATS Reporting Office" (22) + "...  (ARO)" (13) + "Air Traffic Services (ARO)" (1) = 36
  "Remarks" (34) + "Remark" (2) = 36
The one-off "Air Traffic Services (ARO)" (DNJO) is NOT the ARO field — direct
inspection confirmed DNJO ALSO has a separate, clean "ATS Reporting Office"
entry; the "(ARO)" suffix is a stray label quirk on the Air Traffic Services
row specifically for this one aerodrome. Matching by which phrase the label
STARTS WITH (not by "contains (ARO)") resolves this correctly — the two
labels have distinct prefixes even though both carry the same suffix.

Every one of the 12 canonical service categories below was derived FROM the
real label text across all 36 aerodromes, not assumed in advance.
"""
import re

from extractor_base import SubsectionExtractor, ExtractResult, ValidationIssue

# Ordered (pattern, canonical_key) — checked in order, first match wins.
# Order matters specifically for the DNJO "Air Traffic Services (ARO)" case:
# it must match the air_traffic_services pattern (checked by prefix), not be
# confused with ats_reporting_office.
CANONICAL_SERVICES = [
    (re.compile(r'^(AD Administration|Aerodrome Operator|AD Operator)\b', re.I), "aerodrome_operator"),
    (re.compile(r'^Customs', re.I), "customs_immigration"),
    (re.compile(r'^Health', re.I), "health_sanitation"),
    (re.compile(r'^AIS Briefing', re.I), "ais_briefing_office"),
    (re.compile(r'^ATS Reporting Office', re.I), "ats_reporting_office"),
    (re.compile(r'^MET Briefing', re.I), "met_briefing_office"),
    (re.compile(r'^Air Traffic Services', re.I), "air_traffic_services"),
    (re.compile(r'^Fuelling', re.I), "fuelling"),
    (re.compile(r'^Handling', re.I), "handling"),
    (re.compile(r'^Security', re.I), "security"),
    (re.compile(r'^De.?icing', re.I), "de_icing"),
    (re.compile(r'^Remarks?', re.I), "remarks"),
]


def _canonicalize(label: str):
    for pattern, key in CANONICAL_SERVICES:
        if pattern.match(label):
            return key
    return None


class AD23Extractor(SubsectionExtractor):
    subsection = "2.3"
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
                "hours": value or None,
            })

        embed_text = f"{icao} operational hours: " + "; ".join(
            f"{r['raw_label']}={r['hours']}" for r in records)

        return ExtractResult(
            icao=icao, subsection=self.subsection, kind=self.kind,
            records=records, text="", embed_text=embed_text, warnings=warnings,
        )

    def validate(self, result: ExtractResult) -> list:
        issues = []
        if not result.records:
            issues.append(ValidationIssue("error", "records",
                                          f"{result.icao}: no AD 2.3 records produced"))
            return issues
        for rec in result.records:
            # null-over-guess: hours may legitimately be absent in rare cases
            # (not observed across the 36, but not assumed impossible) — that
            # is a warning-level concern, not a blocking error. What DOES
            # block: a row whose label couldn't be canonicalized at all, since
            # that means this extractor doesn't understand what it just read.
            if rec["service"] is None:
                issues.append(ValidationIssue(
                    "error", "service",
                    f"{result.icao}: unrecognized service label {rec['raw_label']!r}"))
        return issues
