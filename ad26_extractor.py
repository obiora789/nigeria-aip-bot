"""
ad26_extractor.py — AD 2.6 RESCUE AND FIRE FIGHTING.

Sixth Layer 2 subsection extractor. Tabular kind, one record per aerodrome
(fixed 4-field structure, like AD 2.1/2.2 — not the variable-count pattern of
AD 2.3/2.4/2.5).

EVIDENCE, gathered across all 36 standard aerodromes: the cleanest subsection
found so far — exactly 4 fields (AD category for fire fighting, Rescue
equipment, Capability for removal of disabled aircraft, Remarks), all 36/36,
zero chrome contamination, zero field-count variance, zero label variants.

FIELD 1 (RFF category) is the only one parsed beyond raw text — it directly
determines what aircraft types the aerodrome's fire service can safely
support, so it's worth a clean integer rather than leaving it as "CAT 9"
text. Format is consistent ("CAT N") with one confirmed case variant (DNKS:
"Cat 6" vs everyone else's "CAT N") — parsed case-insensitively.

Fields 2-4 (rescue equipment, removal capability, remarks) are genuinely
free text with wide real variance (equipment lists, contact details,
capability notes) — kept as-is, matching the same non-over-parsing decision
made for AD 2.2's non-critical fields.
"""
import re

from extractor_base import SubsectionExtractor, ExtractResult, ValidationIssue

CAT_RE = re.compile(r'CAT\s*(\d+)', re.IGNORECASE)

FIELD_ORDER = ["rff_category_raw", "rescue_equipment", "removal_capability", "remarks"]


class AD26Extractor(SubsectionExtractor):
    subsection = "2.6"
    kind = "tabular"

    def extract(self, icao: str, segments: list) -> ExtractResult:
        all_words = self.combine_segment_words(segments)
        pairs = self.parse_key_value_rows(all_words)
        warnings = []

        values = {}
        for i, field_name in enumerate(FIELD_ORDER):
            raw = pairs[i][1] if i < len(pairs) else None
            values[field_name] = self.clean_text(raw) if raw else raw
        if len(pairs) != 4:
            warnings.append(f"expected 4 fields, found {len(pairs)}")

        rff_category = None
        if values["rff_category_raw"]:
            m = CAT_RE.search(values["rff_category_raw"])
            if m:
                rff_category = int(m.group(1))
            else:
                warnings.append(f"could not parse RFF category from: "
                                 f"{values['rff_category_raw']!r}")

        record = {
            "icao": icao,
            "rff_category": rff_category,
            "rff_category_raw": values["rff_category_raw"],
            "rescue_equipment": values["rescue_equipment"],
            "removal_capability": values["removal_capability"],
            "remarks": values["remarks"],
        }

        embed_text = f"{icao} RFF category {rff_category}" if rff_category else f"{icao} AD 2.6"

        return ExtractResult(
            icao=icao, subsection=self.subsection, kind=self.kind,
            records=[record], text="", embed_text=embed_text, warnings=warnings,
        )

    def validate(self, result: ExtractResult) -> list:
        issues = []
        if not result.records:
            issues.append(ValidationIssue("error", "records", "no record produced"))
            return issues
        rec = result.records[0]
        # null-over-guess: rescue_equipment/removal_capability/remarks may
        # legitimately be blank in the source (confirmed: DNBB's Remarks is
        # an empty string) — not blocking. The RFF category IS blocking: it's
        # present in 100% of the 36 real aerodromes tested, and a null here
        # would mean this extractor failed to read a field every real page
        # has, not a genuine gap in the source.
        if rec["rff_category"] is None:
            issues.append(ValidationIssue("error", "rff_category",
                                          f"{result.icao}: RFF category missing or unparseable "
                                          f"(raw: {rec['rff_category_raw']!r})"))
        return issues
