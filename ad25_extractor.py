"""
ad25_extractor.py — AD 2.5 PASSENGER FACILITIES.

Fifth Layer 2 subsection extractor. Tabular kind, one record per declared
facility category (same shape as AD23/AD24Extractor).

EVIDENCE, gathered across all 36 standard aerodromes: label text is clean and
completely consistent — exactly 7 canonical labels (Hotels, Restaurants,
Transportation, Medical facilities, Bank and Post Office, Tourist Office,
Remarks), each appearing 36/36 times. No field-count variance like AD 2.3/2.4
— every aerodrome publishes the full set.

A THIRD REAL LAYER-2 BUG FOUND AND FIXED WHILE BUILDING THIS EXTRACTOR (again
in the shared layer, not specific to AD 2.5):

Cross-page word ordering. A word's 'top' coordinate resets near zero on every
new page — it is NOT a running position across a whole subsection. Every
extractor before this one built its word list with a flat concatenation
across segments, sorted later by raw top value — which silently breaks
whenever a subsection genuinely spans multiple physical pages, because a
later page's early rows can have SMALLER top values than an earlier page's
later rows. Confirmed directly: DNMM's (Lagos — one of the busiest
aerodromes, the first content large enough to actually need two pages for
this subsection) AD 2.5 spans pages 851-852. Flat concatenation put page
852's "3 Transportation" ahead of page 851's "1 Hotels"/"2 Restaurants", and
independently caused the page-852 repeated subsection header ("DNMM AD 2.5
PASSENGER FACILITIES") to be swallowed into the "Remarks" field's value.
Fixed with a new shared helper, SubsectionExtractor.combine_segment_words()
in extractor_base.py, which offsets each page's words by a page-index-based
constant before any position-based logic runs — page order becomes the
PRIMARY sort key, matching true reading order. AD22/23/24Extractor were
retrofitted to use this helper too (previously silent-passing on all 36
aerodromes only because none of them happened to need a genuine multi-page
subsection in the sample tested — this was a latent bug, not unique to
AD 2.5).

All three fixes found while building AD 2.1-2.5 (chrome filtering, gutter/
title-line exclusion, and this cross-page ordering fix) were re-validated
against every prior subsection (36/36 each) before this extractor shipped.
"""
import re

from extractor_base import SubsectionExtractor, ExtractResult, ValidationIssue

CANONICAL_FACILITIES = [
    (re.compile(r'^Hotels?', re.I), "hotels"),
    (re.compile(r'^Restaurants?', re.I), "restaurants"),
    (re.compile(r'^Transportation', re.I), "transportation"),
    (re.compile(r'^Medical facilities', re.I), "medical_facilities"),
    (re.compile(r'^Bank and Post Office', re.I), "bank_post_office"),
    (re.compile(r'^Tourist Office', re.I), "tourist_office"),
    (re.compile(r'^Remarks?', re.I), "remarks"),
]


def _canonicalize(label: str):
    for pattern, key in CANONICAL_FACILITIES:
        if pattern.match(label):
            return key
    return None


class AD25Extractor(SubsectionExtractor):
    subsection = "2.5"
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
                warnings.append(f"unrecognized facility label (new in this AIRAC cycle?): {label!r}")
            elif key in seen_keys:
                warnings.append(f"duplicate canonical facility {key!r} on one page "
                                 f"(label {label!r}) — check for a mislabeled row")
            seen_keys.add(key)
            records.append({
                "icao": icao,
                "facility": key,
                "raw_label": label,
                "detail": value or None,
            })

        embed_text = f"{icao} passenger facilities: " + "; ".join(
            f"{r['raw_label']}={r['detail']}" for r in records)

        return ExtractResult(
            icao=icao, subsection=self.subsection, kind=self.kind,
            records=records, text="", embed_text=embed_text, warnings=warnings,
        )

    def validate(self, result: ExtractResult) -> list:
        issues = []
        if not result.records:
            issues.append(ValidationIssue("error", "records",
                                          f"{result.icao}: no AD 2.5 records produced"))
            return issues
        for rec in result.records:
            if rec["facility"] is None:
                issues.append(ValidationIssue(
                    "error", "facility",
                    f"{result.icao}: unrecognized facility label {rec['raw_label']!r}"))
        return issues
