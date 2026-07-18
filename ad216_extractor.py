"""
ad216_extractor.py — AD 2.16 HELICOPTER LANDING AREA.

Sixteenth Layer 2 subsection extractor. Tabular kind, but the INVERSE of
most subsections built so far: structured extraction is the exception, not
the rule.

EVIDENCE, gathered across all 36 standard aerodromes (these are fixed-wing
airports — dedicated helicopters are the separate DNGB/DNPS/DNSK/DNWI
pseudo-aerodromes under AD 3.x, not covered here): 35 of 36 have NO table
at all — just a short prose status statement ("Not designated.", "Not
Available", "NIL", or occasionally something substantive like DNFD's "One
helicopter parking stand is available on the apron"). Only DNCA has the
full 7-field ICAO-standard structured table (coordinates, TLOF/FATO
elevation, dimensions/surface/strength, bearing, declared distances,
lighting, remarks) — including a real, safety-relevant elevation figure
(61.5m), the same class of number AD 2.12 cares about.

DESIGN: try structured (positional, 7-field) extraction first via
parse_key_value_rows; if no row-anchors are found (the 35-aerodrome case),
fall back to capturing the prose status as a single record — the same
fallback pattern already proven necessary for AD 2.9 (DNSU) and AD 2.14
(7 aerodromes with no published lighting table).
"""
from extractor_base import SubsectionExtractor, ExtractResult, ValidationIssue

FIELD_ORDER = ["coordinates", "elevation_raw", "dimensions_surface_strength",
               "bearing", "declared_distances", "lighting", "remarks"]


class AD216Extractor(SubsectionExtractor):
    subsection = "2.16"
    kind = "tabular"

    def extract(self, icao: str, segments: list) -> ExtractResult:
        all_words = self.combine_segment_words(segments)
        pairs = self.parse_key_value_rows(all_words)
        warnings = []

        if not pairs:
            # No numbered rows — confirmed the common case (35 of 36
            # aerodromes): a plain prose status statement, not a table.
            full_text = self.clean_text(" ".join(w[4] for w in all_words))
            if full_text.strip():
                warnings.append("no numbered rows found — captured as status text")
                record = {"icao": icao, "status": full_text}
                for f in FIELD_ORDER:
                    record[f] = None
                return ExtractResult(
                    icao=icao, subsection=self.subsection, kind=self.kind,
                    records=[record], text="", embed_text=f"{icao}: {full_text}",
                    warnings=warnings,
                )
            warnings.append("AD 2.16 segment produced no text at all")
            return ExtractResult(
                icao=icao, subsection=self.subsection, kind=self.kind,
                records=[], text="", embed_text="", warnings=warnings,
            )

        # Structured path — confirmed necessary for DNCA, the one aerodrome
        # with a real published helicopter landing area.
        values = {}
        for i, field_name in enumerate(FIELD_ORDER):
            raw = pairs[i][1] if i < len(pairs) else None
            values[field_name] = self.clean_text(raw) if raw else raw
        if len(pairs) != 7:
            warnings.append(f"expected 7 fields, found {len(pairs)}")

        record = {"icao": icao, "status": None, **values}
        embed_text = f"{icao} helicopter landing area: {values.get('coordinates')}"

        return ExtractResult(
            icao=icao, subsection=self.subsection, kind=self.kind,
            records=[record], text="", embed_text=embed_text, warnings=warnings,
        )

    def validate(self, result: ExtractResult) -> list:
        issues = []
        if not result.records:
            issues.append(ValidationIssue("error", "records", "no record produced"))
            return issues
        # Neither path is safety-critical enough to block on individual
        # nulls: a prose "Not designated" status IS the answer for 35 of 36
        # aerodromes, and DNCA's own fields (though including a real
        # elevation) are informational for helicopter ops planning, not the
        # kind of universal flight-envelope number AD 2.2/2.12/2.13 carry.
        return issues
