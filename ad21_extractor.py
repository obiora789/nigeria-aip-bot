"""
ad21_extractor.py — AD 2.1 AERODROME LOCATION INDICATOR AND NAME.

The first Layer 2 subsection extractor (AD 2.1 -> 2.24, strict order).
Tabular kind: one record per aerodrome — {icao, city, aerodrome_name}.

FORMAT, established from real content across all 36 standard aerodromes:
    "DNKN AD 2.1 AERODROME LOCATION INDICATOR AND NAME
     DNKN - KANO/Mallam Aminu Kano"
i.e. after the subsection header: "{ICAO} - {CITY}/{AERODROME NAME}".

TWO REAL DATA ANOMALIES FOUND ACROSS THE 36, both handled WITHOUT needing to
"clean" the header text — the parser searches for the distinctive
"{ICAO} - CITY/Name" data pattern directly, wherever it falls, rather than
trying to strip a variable-length prefix:
  - DNBE: a stray digit is glued into the header text itself
    ("AD 2.1 4AERODROME LOCATION..."). Irrelevant to this parser — the data
    pattern search starts from "DNBE -", never touching the header.
  - DNIM: this page prints "DNIM AD 2.1" TWICE — once as a bare, data-less
    line, once as the real header + data. segment_page's continuation-join
    concatenates both into one string; searching for the ICAO-anchored data
    pattern finds the real occurrence regardless of the duplicate.
Both required fixing a genuine segmenter gap first (DNAA's very first
subsection header lacks its own ICAO prefix, unlike every other aerodrome —
see segment_page.py's SUBSECTION_HDR_RE docstring) before this extractor could
even see DNAA's segment at all.

Validated: all 36 standard aerodromes parse cleanly (0 failures) — see
validate_ad21.py.
"""
import re

from extractor_base import SubsectionExtractor, ExtractResult, ValidationIssue

# {ICAO} - {CITY}/{NAME}, anchored on the ICAO itself so header artifacts
# (stray digits, duplicated boilerplate) never enter the match.
_DATA_RE_TEMPLATE = r'{icao}\s*-\s*([A-Z][A-Z\s]*?)\s*/\s*(.+)$'


class AD21Extractor(SubsectionExtractor):
    subsection = "2.1"
    kind = "tabular"

    def extract(self, icao: str, segments: list) -> ExtractResult:
        text = self.segment_text(segments)
        warnings = []
        city = None
        name = None

        m = re.search(_DATA_RE_TEMPLATE.format(icao=re.escape(icao)), text)
        if m:
            city = m.group(1).strip() or None
            name = m.group(2).strip() or None
        else:
            warnings.append(f"could not find '{icao} - CITY/NAME' pattern in: {text[:120]!r}")

        record = {"icao": icao, "city": city, "aerodrome_name": name}
        embed_text = f"{icao} — {city}/{name}" if city and name else text

        return ExtractResult(
            icao=icao,
            subsection=self.subsection,
            kind=self.kind,
            records=[record],
            text="",
            embed_text=embed_text,
            warnings=warnings,
        )

    def validate(self, result: ExtractResult) -> list:
        issues = []
        if not result.records:
            issues.append(ValidationIssue("error", "records", "no record produced"))
            return issues
        rec = result.records[0]
        # null-over-guess is enforced at extract() — validate() only decides
        # severity. Both city and name are load-bearing identity fields for an
        # aerodrome record; neither may be null.
        if not rec.get("city"):
            issues.append(ValidationIssue("error", "city", f"{result.icao}: city is null"))
        if not rec.get("aerodrome_name"):
            issues.append(ValidationIssue("error", "aerodrome_name",
                                          f"{result.icao}: aerodrome_name is null"))
        if rec.get("icao") != result.icao:
            issues.append(ValidationIssue("error", "icao",
                                          f"record icao {rec.get('icao')!r} != {result.icao!r}"))
        return issues
