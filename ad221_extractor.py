"""
ad221_extractor.py — AD 2.21 PROCEDURES FOR NOISE ABATEMENT.

Twenty-first Layer 2 subsection extractor. Tabular kind, one record per
aerodrome, single status field — no numbered structure at all.

EVIDENCE, gathered across all 36 standard aerodromes: uniformly simple
prose, no table structure whatsoever. Overwhelmingly "Not Available"/"Not
AVBL"/"Not applicable"/"Not Designated" variants (Nigeria has no strict
noise-abatement regime at most of these aerodromes), one substantive entry
(DNFB: "To be developed."), and one genuinely empty subsection (DNFD — the
section title appears with no text after it at all, matching the same
confirmed-genuine-empty pattern already found elsewhere in this project,
e.g. AD 2.13's DNBB, AD 2.20's DNFB).

No numeric or structured sub-fields exist here to extract — matches the
same non-over-engineering decision already made for AD 2.7's similarly
uniform, mostly-NIL content.
"""
from extractor_base import SubsectionExtractor, ExtractResult, ValidationIssue


class AD221Extractor(SubsectionExtractor):
    subsection = "2.21"
    kind = "tabular"

    def extract(self, icao: str, segments: list) -> ExtractResult:
        all_words = self.combine_segment_words(segments)
        full_text = self.clean_text(" ".join(w[4] for w in all_words))
        warnings = []

        # Strip the redundant "ICAO AD 2.21 NOISE ABATEMENT PROCEDURES"
        # header prefix, consistently present at the start of every
        # aerodrome's segment, leaving just the genuine status text.
        prefix = f"{icao} AD 2.21 NOISE ABATEMENT PROCEDURES"
        status = full_text[len(prefix):].strip() if full_text.startswith(prefix) else full_text.strip()

        if not status:
            warnings.append(f"{icao}: AD 2.21 has no status text at all "
                             f"(confirmed genuine on some aerodromes, e.g. DNFD)")

        record = {"icao": icao, "status": status or None}
        embed_text = f"{icao} noise abatement: {status}" if status else f"{icao} AD 2.21"

        return ExtractResult(
            icao=icao, subsection=self.subsection, kind=self.kind,
            records=[record], text="", embed_text=embed_text, warnings=warnings,
        )

    def validate(self, result: ExtractResult) -> list:
        issues = []
        if not result.records:
            issues.append(ValidationIssue("error", "records", "no record produced"))
            return issues
        # A None status is a confirmed genuine gap (DNFD), not an error —
        # this subsection is legitimately empty on some aerodromes.
        return issues
