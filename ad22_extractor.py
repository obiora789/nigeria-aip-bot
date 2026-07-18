"""
ad22_extractor.py — AD 2.2 AERODROME GEOGRAPHICAL AND ADMINISTRATIVE DATA.

Second Layer 2 subsection extractor. Tabular kind: one record per aerodrome
with 8 numbered fields. Two of them (elevation, ARP coordinates) are
safety-critical numeric data; the rest are free text, kept as-is.

FIELD-PAIRING: uses SubsectionExtractor.parse_key_value_rows (ported from the
validated extract_page_text_fixed.py logic) to split the AD 2.2 segment into
[(label, value), ...] pairs. Proven across all 36 standard aerodromes:
every one produces exactly 8 fields.

NUMERIC FIELDS — extracted independently, not with one combined pattern.
A single "metres (feet) / temp" regex was tried and rejected: real values
include no-parens format ("8.32 m/ 27.3 ft", DNSU), a stray internal space in
the unit itself ("410 f t", DNOG), uppercase units ("918.819FT", DNBB/DNBK),
an extra "AMSL" token sitting mid-expression (DNAN/DNGO/DNMK), and internal
spaces inside big numbers as a thousands-marker ("1 289.023", DNJO). Anchoring
each unit (m / ft / °C) independently and ignoring everything else survives
all of these; validated to parse cleanly on all 36 real elevation strings.

NULL-OVER-GUESS, confirmed necessary, not hypothetical: DNAS and DNBY publish
no metres elevation at all (feet-only aerodromes); DNBC and DNSU's source
text has NO reference temperature printed at all (confirmed by direct page
inspection for DNSU — "8.32 m/ 27.3 ft" with nothing after "ft"). These are
real gaps in the source AIP, not extraction failures, and must surface as
None, never a fabricated figure.

COORDINATES: ARP field format is consistent across all 36 —
"DDMMSSN DDDMMSSE" (fixed-width degrees-minutes-seconds + hemisphere), with
one variant (DNOG) using decimal seconds. Converted to decimal degrees.
"""
import re

from extractor_base import SubsectionExtractor, ExtractResult, ValidationIssue

M_RE      = re.compile(r'([\d][\d\s]*\.?\d*)\s*m\b', re.IGNORECASE)
FT_RE     = re.compile(r'([\d][\d\s]*\.?\d*)\s*f\s*t\b', re.IGNORECASE)
C_RE      = re.compile(r'([\d][\d\s]*\.?\d*)\s*°?\s*o?C\b', re.IGNORECASE)
LATLON_RE = re.compile(r'(\d{6}(?:\.\d+)?)([NS])\s+(\d{7}(?:\.\d+)?)([EW])')

FIELD_ORDER = [
    "arp_raw", "direction_distance_from_city", "elevation_raw",
    "geoid_undulation_raw", "magnetic_variation", "operator_info",
    "traffic_type", "remarks",
]


def _to_float(s):
    s = re.sub(r'\s+', '', s)
    try:
        return float(s)
    except ValueError:
        return None


def _dms_to_decimal(dms, is_lon):
    """Convert 'DDMMSS[.frac]' or 'DDDMMSS[.frac]' to decimal degrees."""
    intpart, _, frac = dms.partition('.')
    deg_len = 3 if is_lon else 2
    if len(intpart) < deg_len + 4:
        return None
    deg = int(intpart[:deg_len])
    minute = int(intpart[deg_len:deg_len + 2])
    sec_str = intpart[deg_len + 2:] + (('.' + frac) if frac else '')
    try:
        sec = float(sec_str)
    except ValueError:
        return None
    return round(deg + minute / 60 + sec / 3600, 6)


def _parse_elevation(value):
    m_match = M_RE.search(value)
    ft_match = FT_RE.search(value)
    c_match = C_RE.search(value)
    return (
        _to_float(m_match.group(1)) if m_match else None,
        _to_float(ft_match.group(1)) if ft_match else None,
        _to_float(c_match.group(1)) if c_match else None,
    )


def _parse_coordinates(value):
    m = LATLON_RE.search(value)
    if not m:
        return None, None
    lat = _dms_to_decimal(m.group(1), is_lon=False)
    lon = _dms_to_decimal(m.group(3), is_lon=True)
    if lat is None or lon is None:
        return None, None
    if m.group(2) == 'S':
        lat = -lat
    if m.group(4) == 'W':
        lon = -lon
    return lat, lon


class AD22Extractor(SubsectionExtractor):
    subsection = "2.2"
    kind = "tabular"

    def extract(self, icao: str, segments: list) -> ExtractResult:
        all_words = self.combine_segment_words(segments)
        pairs = self.parse_key_value_rows(all_words)
        warnings = []

        values = {}
        for i, field_name in enumerate(FIELD_ORDER):
            raw = pairs[i][1] if i < len(pairs) else None
            values[field_name] = self.clean_text(raw) if raw else raw
        if len(pairs) != 8:
            warnings.append(f"expected 8 fields, found {len(pairs)}")

        # operator_info (field 6) is NOT a genuine label:value pair — it's
        # one cohesive multi-line block (address/telephone/telefax/AFS/AFTN/
        # SITA), and gutter-based splitting loses real data here. Confirmed
        # directly: this field's own sub-label "AFS" prints at the SAME
        # far-left x-position as the row's OWN field label on many
        # aerodromes (while OTHER sub-labels in the same block, "TEL:"/
        # "AFTN:", sit at the normal value position) — a gutter can only
        # make one binary left/right decision per word position, so it has
        # no way to know "AFS" is semantically value content. Checked
        # empirically across the first 18 aerodromes: the word "AFS" was
        # silently dropped from operator_info on 13 of them before this fix.
        # parse_full_rows returns the row's COMPLETE text (both sides of
        # the gutter combined) instead — this does carry the field's own
        # label prefix along with it (a minor cosmetic redundancy, and the
        # label phrasing itself genuinely varies between at least two
        # different templates across aerodromes, so it isn't reliably
        # stripped), but guarantees no real content is lost, which matters
        # more than a perfectly clean value for a contact-information field.
        full_rows = self.parse_full_rows(all_words)
        if len(full_rows) == len(pairs) and len(full_rows) > 5:
            values["operator_info"] = self.clean_text(full_rows[5])
        elif len(full_rows) != len(pairs):
            warnings.append(f"parse_full_rows returned {len(full_rows)} rows but "
                             f"parse_key_value_rows returned {len(pairs)} — indices "
                             f"may not align, keeping the gutter-based operator_info "
                             f"value rather than risk using the wrong row")

        # Full-segment text, cleaned — used as a fallback when the gutter-based
        # column split doesn't yield the value for a field with an
        # unmistakable, unambiguous shape (coordinates, elevation units).
        # Confirmed necessary in production (not hypothetical): a real fitz
        # run failed to find DNBC/DNBK's ARP coordinates via the gutter-based
        # 'arp_raw' value alone, even though the coordinate is genuinely
        # present on the page and this sandbox's pdfplumber-based mirror
        # parsed it correctly — a real divergence between extraction engines
        # for this specific row, not reproduced with certainty here (no fitz
        # available in this environment to confirm the exact mechanism). A
        # coordinate pattern (DDMMSSN DDDMMSSE) cannot occur elsewhere in an
        # AD 2.2 segment, so searching the whole segment carries no
        # meaningful false-positive risk — this makes coordinate extraction
        # robust regardless of what caused the gutter-based value to miss it.
        full_text = self.clean_text(" ".join(w[4] for w in all_words))

        arp_lat = arp_lon = None
        if values["arp_raw"]:
            arp_lat, arp_lon = _parse_coordinates(values["arp_raw"])
        if arp_lat is None:
            arp_lat, arp_lon = _parse_coordinates(full_text)
            if arp_lat is not None:
                warnings.append("ARP coordinates recovered via full-segment "
                                 "fallback, not the gutter-split field value")
        if arp_lat is None:
            warnings.append(f"could not parse ARP coordinates anywhere in segment "
                             f"(field value was: {values['arp_raw']!r})")

        elev_m = elev_ft = temp_c = None
        if values["elevation_raw"]:
            elev_m, elev_ft, temp_c = _parse_elevation(values["elevation_raw"])
        if elev_m is None and elev_ft is None:
            elev_m2, elev_ft2, temp_c2 = _parse_elevation(full_text)
            if elev_m2 is not None or elev_ft2 is not None:
                elev_m, elev_ft = elev_m2, elev_ft2
                temp_c = temp_c if temp_c is not None else temp_c2
                warnings.append("elevation recovered via full-segment fallback, "
                                 "not the gutter-split field value")
        if elev_m is None and elev_ft is None:
            warnings.append(f"could not parse elevation from: {values['elevation_raw']!r}")

        geoid_m = None
        if values["geoid_undulation_raw"]:
            gm = M_RE.search(values["geoid_undulation_raw"])
            geoid_m = _to_float(gm.group(1)) if gm else None

        record = {
            "icao": icao,
            "arp_lat": arp_lat,
            "arp_lon": arp_lon,
            "arp_site_description": values["arp_raw"],
            "direction_distance_from_city": values["direction_distance_from_city"],
            "elevation_m": elev_m,
            "elevation_ft": elev_ft,
            "reference_temp_c": temp_c,
            "geoid_undulation_m": geoid_m,
            "magnetic_variation": values["magnetic_variation"],
            "operator_info": values["operator_info"],
            "traffic_type": values["traffic_type"],
            "remarks": values["remarks"],
        }

        embed_parts = [f"{icao} elevation {elev_m}m/{elev_ft}ft" if (elev_m or elev_ft) else ""]
        embed_text = " ".join(p for p in embed_parts if p) or f"{icao} AD 2.2"

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

        # Coordinates: never null — every one of the 36 aerodromes has them,
        # and a missing ARP is a genuine extraction failure, not a real gap.
        if rec["arp_lat"] is None or rec["arp_lon"] is None:
            issues.append(ValidationIssue("error", "arp_lat/arp_lon",
                                          f"{result.icao}: ARP coordinates missing"))

        # Elevation: null-over-guess applies. At least ONE of metres/feet must
        # be present (every aerodrome has at least one) — but which one, and
        # whether temperature is present, both vary genuinely in the source
        # (confirmed: DNAS/DNBY feet-only; DNBC/DNSU no temperature at all).
        if rec["elevation_m"] is None and rec["elevation_ft"] is None:
            issues.append(ValidationIssue("error", "elevation",
                                          f"{result.icao}: no elevation value in either unit"))

        return issues
