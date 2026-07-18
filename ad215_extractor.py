"""
ad215_extractor.py — AD 2.15 OTHER LIGHTING, SECONDARY POWER SUPPLY.

Fifteenth Layer 2 subsection extractor. Tabular kind, one record per
aerodrome, POSITIONAL (fixed 5-field structure) — like AD 2.6/2.7, unlike
the canonical-label-matching subsections (2.3/2.4/2.5/2.9) or the
text-preserving ones (2.10/2.11).

EVIDENCE, gathered across a sample before designing: 5 fields, stable
position and count, consistent across every aerodrome checked (ABN/IBN
beacon, LDI+Anemometer, TWY edge/centreline lighting, secondary power
supply, remarks).

FIELD 2 IS GENUINELY COMPOUND (LDI location/LGT + Anemometer location/LGT
share one numbered row) — checked whether this causes the same
unpredictable value-bleeding AD 2.11's compound fields did, and it does
NOT: tested against 3 aerodromes, the value consistently and predictably
captures both facilities' combined description (e.g. DNAS: "WDI's 3 Nr 2
Nr" — wind direction indicators + anemometer count together), not a
scrambled partial split. Kept as one combined free-text field rather than
force-splitting LDI-specific from Anemometer-specific content, since that
split isn't reliably separable from the source format itself — the same
non-over-parsing decision made for AD 2.2's non-critical paired fields.

No field here is safety-critical numeric data — all five are
descriptive/categorical (Available/NIL/free text), so none get deep
parsing beyond capturing the text cleanly.
"""
from extractor_base import SubsectionExtractor, ExtractResult, ValidationIssue

FIELD_ORDER = ["abn_ibn", "ldi_anemometer", "twy_lighting",
               "secondary_power", "remarks"]


class AD215Extractor(SubsectionExtractor):
    subsection = "2.15"
    kind = "tabular"

    def extract(self, icao: str, segments: list) -> ExtractResult:
        all_words = self.combine_segment_words(segments)
        pairs = self.parse_key_value_rows(all_words)
        warnings = []

        values = {}
        for i, field_name in enumerate(FIELD_ORDER):
            raw = pairs[i][1] if i < len(pairs) else None
            values[field_name] = self.clean_text(raw) if raw else raw
        if len(pairs) != 5:
            warnings.append(f"expected 5 fields, found {len(pairs)}")

        record = {"icao": icao, **values}
        embed_text = f"{icao} AD 2.15: ABN/IBN={values['abn_ibn']}"

        return ExtractResult(
            icao=icao, subsection=self.subsection, kind=self.kind,
            records=[record], text="", embed_text=embed_text, warnings=warnings,
        )

    def validate(self, result: ExtractResult) -> list:
        issues = []
        if not result.records:
            issues.append(ValidationIssue("error", "records", "no record produced"))
            return issues
        # No field here rises to the level of AD 2.2/2.6/2.12/2.13's
        # safety-critical numerics — descriptive lighting/power-supply
        # status is informational, and legitimately blank in the source at
        # times (confirmed: DNAS's secondary power field is empty, DNAN's
        # remarks field is empty). Only total extraction failure blocks.
        return issues
