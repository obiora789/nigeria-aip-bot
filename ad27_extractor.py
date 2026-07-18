"""
ad27_extractor.py — AD 2.7 SEASONAL AVAILABILITY - CLEARING.

Seventh Layer 2 subsection extractor. Tabular kind, one record per aerodrome
(fixed 3-field structure, like AD 2.6).

EVIDENCE, gathered across all 36 standard aerodromes: exactly 3 fields (Types
of clearing equipment, Clearance priorities, Remarks), 36/36, zero chrome
contamination, zero field-count variance.

NO NUMERIC PARSING — deliberately, unlike AD 2.6. This subsection is about
snow/ice clearing, which is not applicable to Nigeria's tropical climate:
values are overwhelmingly "NIL"/"Not applicable" with heavy real spelling
variance across aerodromes (confirmed: "Not applicable", "Not Applicable",
"Not AVBL", "Not application", "NIL", "Nil", plus a few genuinely populated
entries like "2 Tractors" or specific runway names for the handful of
aerodromes with real clearing equipment/priorities). Normalizing these
variants into a canonical "not applicable" flag would be manufacturing
structure the source doesn't actually have — kept as clean free text per
field instead, same decision already made for AD 2.2's non-critical fields
and AD 2.6's equipment/remarks fields.
"""
from extractor_base import SubsectionExtractor, ExtractResult, ValidationIssue

FIELD_ORDER = ["clearing_equipment", "clearance_priorities", "remarks"]


class AD27Extractor(SubsectionExtractor):
    subsection = "2.7"
    kind = "tabular"

    def extract(self, icao: str, segments: list) -> ExtractResult:
        all_words = self.combine_segment_words(segments)
        pairs = self.parse_key_value_rows(all_words)
        warnings = []

        values = {}
        for i, field_name in enumerate(FIELD_ORDER):
            raw = pairs[i][1] if i < len(pairs) else None
            values[field_name] = self.clean_text(raw) if raw else raw
        if len(pairs) != 3:
            warnings.append(f"expected 3 fields, found {len(pairs)}")

        record = {"icao": icao, **values}
        embed_text = f"{icao} seasonal clearing: {values['clearing_equipment']}"

        return ExtractResult(
            icao=icao, subsection=self.subsection, kind=self.kind,
            records=[record], text="", embed_text=embed_text, warnings=warnings,
        )

    def validate(self, result: ExtractResult) -> list:
        issues = []
        if not result.records:
            issues.append(ValidationIssue("error", "records", "no record produced"))
            return issues
        # No field here is safety-critical in the way AD 2.2's elevation or
        # AD 2.6's RFF category are — this subsection is "not applicable" for
        # the overwhelming majority of aerodromes by the nature of the
        # climate, so an empty/None field is expected, common, and not an
        # error. Only a total extraction failure (no record at all,
        # already checked above) blocks.
        return issues
