"""
ad28_extractor.py — AD 2.8 APRONS, TAXIWAYS AND CHECK LOCATIONS DATA.

Eighth Layer 2 subsection extractor. Tabular kind, one record per aerodrome.
Positional (not canonical-label) extraction — field COUNT is consistently 6
across all 36 aerodromes, even though the apron/taxiway LABEL TEXT varies a
lot (many real phrasings: "Apron designation, surface and strength" vs
"Apron surface and strength" vs several more, similarly for taxiway).

FLAGGED, NOT DEEPLY PARSED — a genuine multi-facility-per-field pattern:
DNCA (Calabar) lists TWO separate aprons (Terminal Apron, NAF Apron), each
with its own surface/strength/dimensions, under ONE numbered field —
distinguished only by inline sub-labels within the free text, not by
separate rows. Confirmed by direct inspection: DNCA's apron field value
contains "...PCN 70 (LCG II-III)...PCN 20 (LCG V) NAF Apron...". This is the
same class of risk as the AD 2.12/AD 2.19 misattribution incidents this
project has already fought — a downstream synthesis over this flattened text
could attach the wrong strength figure to the wrong apron. Given it's
confirmed on only 1 of 36 aerodromes and building a general sub-parser for
an unbounded, per-aerodrome-varying set of facility names (Terminal/NAF/
General Aviation/etc.) is a much larger undertaking, the pragmatic choice
here is detection + a loud warning (cheap, reliable: the label repeating
"surface"/"strength" more than once), not full structured decomposition.
Downstream consumers of this field should treat a flagged record's raw text
as needing human/synthesis-side care, not a clean single-apron answer.

ALTIMETER CHECKPOINT (field 3) has a location+elevation shape when populated
("Location: X Elevation: Ym") but is mostly "No defined location"/empty
across the 36 — not parsed further, kept as text (same non-over-engineering
call made for other sparse fields elsewhere in this project).
"""
from extractor_base import SubsectionExtractor, ExtractResult, ValidationIssue

FIELD_ORDER = ["apron_info", "taxiway_info", "altimeter_checkpoint",
               "vor_checkpoints", "ins_checkpoints", "remarks"]


def _looks_multi_facility(label: str) -> bool:
    l = label.lower()
    return l.count("surface") > 1 or l.count("strength") > 1


class AD28Extractor(SubsectionExtractor):
    subsection = "2.8"
    kind = "tabular"

    def extract(self, icao: str, segments: list) -> ExtractResult:
        all_words = self.combine_segment_words(segments)
        pairs = self.parse_key_value_rows(all_words)
        warnings = []

        values = {}
        raw_labels = {}
        for i, field_name in enumerate(FIELD_ORDER):
            label = self.clean_text(pairs[i][0]) if i < len(pairs) else None
            raw = pairs[i][1] if i < len(pairs) else None
            values[field_name] = self.clean_text(raw) if raw else raw
            raw_labels[field_name] = label
        if len(pairs) != 6:
            warnings.append(f"expected 6 fields, found {len(pairs)}")

        multi_facility = False
        for fname in ("apron_info", "taxiway_info"):
            if raw_labels[fname] and _looks_multi_facility(raw_labels[fname]):
                multi_facility = True
                warnings.append(
                    f"{fname} label suggests MULTIPLE facilities merged into one field "
                    f"(label: {raw_labels[fname]!r}) — do not trust a single strength/"
                    f"dimension figure pulled from this text without checking which "
                    f"facility it belongs to")

        record = {"icao": icao, **values, "multi_facility_flag": multi_facility}
        embed_text = f"{icao} AD 2.8 aprons/taxiways" + (" [MULTI-FACILITY]" if multi_facility else "")

        return ExtractResult(
            icao=icao, subsection=self.subsection, kind=self.kind,
            records=[record], text="", embed_text=embed_text, warnings=warnings,
        )

    def validate(self, result: ExtractResult) -> list:
        issues = []
        if not result.records:
            issues.append(ValidationIssue("error", "records", "no record produced"))
            return issues
        # No field here is safety-critical enough to block on nulls the way
        # AD 2.2 elevation or AD 2.6 RFF category are — apron/taxiway/
        # checkpoint data is descriptive and frequently legitimately absent
        # ("No defined location", empty). The multi_facility_flag is
        # surfaced as a warning already (in extract()), not an error here —
        # the data isn't wrong, it just needs care downstream.
        return issues
