"""
ad29_extractor.py — AD 2.9 SURFACE MOVEMENT GUIDANCE AND CONTROL SYSTEM AND
MARKINGS.

Ninth Layer 2 subsection extractor. Tabular kind, one record per declared
item (same canonical-label pattern as AD 2.3/2.4/2.5) — WITH a fallback for
the one confirmed aerodrome (DNSU) whose page has no numbered rows at all.

EVIDENCE, gathered across all 36 standard aerodromes:
  - 35 of 36 use the standard numbered-row format. Label text varies (real
    phrasing differences, not parsing artifacts) — "RWY and TWY markings and
    LGT" / "RWY markings and LGT" / "RWY and Taxi-link markings and LGT" are
    the same field; "Use of aircraft stand ID signs, TWY guide lines..." /
    "...Taxi-link guide lines..." are the same field. "Other runway
    protection measures" is genuinely OPTIONAL — present on 19 of 35,
    explaining the observed 4-vs-5 field-count split (not a parsing bug).
  - DNSU is the one real exception: its AD 2.9 page is plain prose with NO
    numbered rows at all — "Airline marshallers and yellow markings
    available." — confirmed by direct page inspection. parse_key_value_rows
    correctly returns empty for a page with no row anchors; without a
    fallback, this aerodrome's real content would be silently dropped
    instead of captured. Falls back to storing the segment's whole text as
    a single "general_notes" record when no anchors are found but the
    segment has real content.
"""
import re

from extractor_base import SubsectionExtractor, ExtractResult, ValidationIssue

CANONICAL_ITEMS = [
    (re.compile(r'^RWY (?:and (?:TWY|Taxi-link) )?markings and LGT', re.I), "rwy_twy_markings_lgt"),
    (re.compile(r'^Stop bars', re.I), "stop_bars"),
    (re.compile(r'^Use of aircraft stand ID signs', re.I), "stand_id_guidance"),
    (re.compile(r'^Other runway protection measures', re.I), "other_protection_measures"),
    (re.compile(r'^Remarks?', re.I), "remarks"),
]


def _canonicalize(label: str):
    for pattern, key in CANONICAL_ITEMS:
        if pattern.match(label):
            return key
    return None


class AD29Extractor(SubsectionExtractor):
    subsection = "2.9"
    kind = "tabular"

    def extract(self, icao: str, segments: list) -> ExtractResult:
        all_words = self.combine_segment_words(segments)
        pairs = self.parse_key_value_rows(all_words)
        warnings = []

        if not pairs:
            # No numbered rows found — confirmed real for DNSU (plain prose,
            # not a table). Fall back to capturing the segment's whole text
            # rather than silently producing nothing.
            full_text = self.clean_text(" ".join(w[4] for w in all_words))
            if full_text.strip():
                warnings.append("no numbered rows found — page is plain prose, "
                                 "not the standard table; captured as general_notes")
                record = {"icao": icao, "item": "general_notes",
                          "raw_label": None, "detail": full_text}
                return ExtractResult(
                    icao=icao, subsection=self.subsection, kind=self.kind,
                    records=[record], text="", embed_text=f"{icao}: {full_text}",
                    warnings=warnings,
                )
            warnings.append("no content found at all for AD 2.9")
            return ExtractResult(
                icao=icao, subsection=self.subsection, kind=self.kind,
                records=[], text="", embed_text="", warnings=warnings,
            )

        records = []
        seen_keys = set()
        for raw_label, raw_value in pairs:
            label = self.clean_text(raw_label)
            value = self.clean_text(raw_value)
            key = _canonicalize(label)
            if key is None:
                warnings.append(f"unrecognized item label (new in this AIRAC cycle?): {label!r}")
            elif key in seen_keys:
                warnings.append(f"duplicate canonical item {key!r} on one page "
                                 f"(label {label!r}) — check for a mislabeled row")
            seen_keys.add(key)
            records.append({
                "icao": icao, "item": key, "raw_label": label, "detail": value or None,
            })

        embed_text = f"{icao} surface movement guidance: " + "; ".join(
            f"{r['raw_label']}={r['detail']}" for r in records)

        return ExtractResult(
            icao=icao, subsection=self.subsection, kind=self.kind,
            records=records, text="", embed_text=embed_text, warnings=warnings,
        )

    def validate(self, result: ExtractResult) -> list:
        issues = []
        if not result.records:
            issues.append(ValidationIssue("error", "records",
                                          f"{result.icao}: no AD 2.9 records produced"))
            return issues
        for rec in result.records:
            if rec["item"] is None:
                issues.append(ValidationIssue(
                    "error", "item",
                    f"{result.icao}: unrecognized item label {rec['raw_label']!r}"))
        return issues
