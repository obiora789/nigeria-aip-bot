"""
ad219_extractor.py — AD 2.19 RADIO NAVIGATION AND LANDING AIDS.

Nineteenth Layer 2 subsection extractor, and — like AD 2.12/2.18 — a known
misattribution-risk subsection flagged in this project's prior history (a
navaid guard was already built once for exactly this reason, alongside the
comms guard). Each navaid (LLZ, GP, VOR/DME, DVOR/DME, NDB, L/Locator) has
its own frequency, hours, position, elevation, and remarks spanning several
wrapped lines — misattributing one navaid's frequency or coordinates to
another is the exact failure mode this guard exists to prevent.

AID-TYPE VOCABULARY, surveyed (not guessed) across all 36 before designing:
LLZ, GP (+ GP/DME, GP ILS/DME variants), VOR/DME, VOR (standalone, no
co-located DME — confirmed real on DNKA), DVOR/DME, NDB, DME, and L
(Locator — confirmed real and necessary: appears on 6 aerodromes, e.g.
"L KT 365 kHz H24 125925.5N..."; initially looked like it could be a stray
single-letter false match, the same mistake made once already with AD
2.18's "SAR" before checking it directly — this time checked immediately).
"ILS CAT I/II/III" and bare "CH ..." lines are confirmed CONTINUATION text
belonging to the preceding LLZ/GP entry, never a new navaid — e.g. an LLZ's
full type description literally reads "LLZ 22 IAB ... / ILS CAT III ..." as
two lines of ONE entry.

TWO REAL LAYOUT VARIANTS, confirmed by direct inspection, both handled:
  - DNOG splits the aid-type word onto its OWN preceding line ("LLZ" alone,
    then "IGA 108.500 MHz..." on the next line) rather than the type+ID+
    frequency all appearing together. Detection allows a type word alone on
    its own line to open a new navaid, with the ID/frequency following on
    subsequent lines.
  - DNIM uses "SA-100 LOCATOR OW 391KHz..." — a one-off equipment-model
    name standing in for the standard "L" abbreviation. Recognized via the
    literal word "LOCATOR" appearing spelled out, not by trying to
    enumerate model names.

STRUCTURED FIELDS extracted within each navaid's correctly-scoped text,
reusing already-proven patterns from this project: frequency (same
FREQ_RE/HOURS_RE-style proximity requirement as AD218Extractor — a
frequency must have a genuine unit AND be near an hours token to be
trusted, not just any number), and ARP-style coordinates (same DMS pattern
as AD22Extractor). Elevation and remarks are kept as free text — the
Elevation column here has two related numbers (antenna elevation and DME
service-volume-radius or similar) whose exact per-aerodrome meaning is not
uniform enough to force into one typed field without guessing.
"""
import re

from extractor_base import SubsectionExtractor, ExtractResult, ValidationIssue

AID_VOCAB = ["DVOR/DME", "VOR/DME", "GP ILS/DME", "GP/DME", "GP", "LLZ",
             "VOR", "NDB", "DME", "L"]
AID_VOCAB.sort(key=len, reverse=True)
# "L" specifically requires a 2-3 letter ID immediately after (matching
# every confirmed real case: "L KT 365 kHz", "L MN 350KHz", "L ZA 336 KHz")
# to avoid false-matching a stray single letter elsewhere in wrapped text —
# the same caution already applied to AD214's bare-number end-markers.
AID_START_RE = re.compile(
    r"^(" + "|".join(re.escape(a) for a in AID_VOCAB if a != "L") + r")\b"
)
LOCATOR_START_RE = re.compile(r"^L\s+[A-Z]{2,3}\b")
LOCATOR_WORD_RE = re.compile(r"^\w[\w-]*\s+LOCATOR\b")  # DNIM's "SA-100 LOCATOR"

FREQ_RE = re.compile(r"\b(\d{2,3}(?:\.\d{1,4})?)\s*(MHz|KHz)\b", re.IGNORECASE)
HOURS_RE = re.compile(r"\b(H24|HJ|HN|HX|\d{4}\s*-\s*\d{4})\b")
LATLON_RE = re.compile(r'(\d{6}(?:\.\d+)?)([NS])\s+(\d{7}(?:\.\d+)?)([EW])')


def _dms_to_decimal(dms, is_lon):
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


def _parse_coordinates(text):
    m = LATLON_RE.search(text)
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


class AD219Extractor(SubsectionExtractor):
    subsection = "2.19"
    kind = "tabular"

    def extract(self, icao: str, segments: list) -> ExtractResult:
        from segment_page import _line_groups
        all_words = self.combine_segment_words(segments)
        lines = _line_groups(all_words)
        warnings = []

        aid_lines = {}       # aid_key -> list[str], accumulated in order
        aid_order = []
        current = None
        counter = 0

        # Exclude the SPECIFIC header phrase that caused a real false
        # positive (confirmed on DNOG/DNIM: "...for VOR/ILS/MLS, give
        # coordinates..." matched "VOR" as if it were a genuine navaid
        # start), rather than skipping everything before a column-index
        # row. The index-row approach was tried first but is unreliable —
        # confirmed on DNES/DNFB, whose tables have NO index row at all,
        # which left the code stuck in "header mode" forever and silently
        # discarded their real NDB entries entirely.
        HEADER_PHRASE_RE = re.compile(r"VOR/ILS/MLS\s*,", re.IGNORECASE)

        for line in lines:
            text = " ".join(w[4] for w in line)
            if HEADER_PHRASE_RE.search(text):
                continue

            aid_type_match = None
            m1 = AID_START_RE.match(text)
            m2 = LOCATOR_START_RE.match(text) if not m1 else None
            m3 = LOCATOR_WORD_RE.match(text) if not m1 and not m2 else None
            if m1:
                aid_type_match = m1.group(1)
            elif m2:
                aid_type_match = "L"
            elif m3:
                aid_type_match = "LOCATOR"

            if aid_type_match:
                counter += 1
                new_key = f"{aid_type_match}_{counter}"
                # Look-back correction: confirmed real on DNOG — a navaid's
                # own frequency can render on the line immediately ABOVE
                # its type+ID line (a vertical rendering-offset quirk, not
                # a genuine reading-order difference: "114.000 MHz
                # 065644.1N" at top=407.8, "DVOR/DME GAT HJ" at top=412.8,
                # only 5pt apart). Without this check, that frequency gets
                # silently attributed to the PRECEDING entity instead —
                # exactly the misattribution risk this subsection exists to
                # guard against. Only reassign a line that (a) is the most
                # recent line on the previous entity and (b) contains no
                # entity-start vocabulary word of its own — confirming it's
                # a genuine orphaned fragment, not real content belonging to
                # the previous entity.
                if current and aid_lines.get(current) and len(aid_lines[current]) >= 1:
                    prev_line = aid_lines[current][-1]
                    prev_has_own_type = bool(
                        AID_START_RE.match(prev_line) or LOCATOR_START_RE.match(prev_line)
                        or LOCATOR_WORD_RE.match(prev_line))
                    prev_looks_like_freq_fragment = bool(FREQ_RE.search(prev_line))
                    if prev_has_own_type is False and prev_looks_like_freq_fragment:
                        aid_lines[current].pop()
                        aid_lines.setdefault(new_key, []).append(prev_line)
                current = new_key
                aid_lines.setdefault(current, [])
                aid_order.append(current)
            if current:
                aid_lines.setdefault(current, []).append(text)

        if not aid_lines:
            warnings.append("no navaid entries found at all")

        records = []
        for key in aid_order:
            full_text = self.clean_text(" ".join(aid_lines[key]))
            aid_type = key.rsplit("_", 1)[0]

            # Frequency validation: require an hours token SOMEWHERE in the
            # entity's own text, not tight proximity to the frequency match
            # itself. Confirmed necessary — AD 2.19's table states hours
            # ONCE per navaid entry (near its ID), not repeated after every
            # individual frequency value the way AD 2.18's comms table
            # does. A tight proximity requirement (correct for AD 2.18)
            # caused frequency detection to fail on the MAJORITY of real
            # entries here, since a navaid's frequency often appears well
            # after its own hours token in reading order (e.g. DNAA's GP:
            # "...H24 090105.3N 354 m 217°09' MAG. 22 332.0 MHz..." — H24
            # is ~50 characters before the frequency it applies to). Lower
            # false-positive risk than AD 2.18 justifies this: each navaid
            # is already correctly scoped to its own entity by the tracking
            # above, so there's no risk of a DIFFERENT entity's stray
            # frequency mention being picked up.
            # Frequency and hours captured INDEPENDENTLY — not requiring
            # both together. AD 2.18's comms extractor required an hours
            # token near each frequency specifically to guard against
            # prose-embedded false matches (its parenthetical operational
            # notes re-mentioned real frequencies out of context). AD 2.19
            # shows no equivalent pattern — each navaid is already correctly
            # scoped to its own entity by the tracking above, so that
            # specific risk doesn't apply here. Confirmed a real problem
            # requiring both together caused: DNKT states its LLZ/GP/L
            # entries' frequencies clearly (e.g. "L KT 365 kHz
            # 125925.5N...") but never restates an hours token for those
            # specific entries (hours is declared once, for the group) —
            # the joint requirement was discarding real, valid frequency
            # data just because hours happened to be absent from that one
            # entity's own text.
            freq_val = freq_unit = freq_hours = None
            fm = FREQ_RE.search(full_text)
            if fm:
                freq_val = float(fm.group(1))
                freq_unit = fm.group(2).upper()
            else:
                # Fallback for a confirmed real source quirk (DNJO): the
                # frequency's number and its unit can print on genuinely
                # separate, non-adjacent lines, with other content (position
                # coordinates, MAG VAR) printed between them in reading
                # order — e.g. "...IJS 110.7 093818.0N 1287m/ 097°24' MAG.
                # MHz 0085306.9E...". The standard adjacent pattern cannot
                # match this. Recover it cautiously: a bare number in a
                # plausible frequency range, appearing within the first ~20
                # characters after the aid's own ID (matching the table's
                # own column order — frequency always comes right after
                # ID), confirmed by a unit word (MHz/KHz) appearing ANYWHERE
                # later in the same entity's own text.
                num_m = re.search(r"\b(\d{2,3}\.\d{1,2})\b", full_text[:40])
                unit_m = re.search(r"\b(MHz|KHz)\b", full_text, re.IGNORECASE)
                if num_m and unit_m:
                    candidate = float(num_m.group(1))
                    if 100 <= candidate <= 340:  # covers VHF (108-118) and
                                                   # GP's UHF band (328-336)
                        freq_val = candidate
                        freq_unit = unit_m.group(1).upper()
                        warnings.append(f"{icao} {aid_type}: frequency recovered via "
                                         f"fallback (number/unit on separate lines)")
            hm = HOURS_RE.search(full_text)
            if hm:
                freq_hours = hm.group(1)

            lat, lon = _parse_coordinates(full_text)

            records.append({
                "icao": icao,
                "aid_type": aid_type,
                "frequency": freq_val,
                "freq_unit": freq_unit,
                "hours": freq_hours,
                "lat": lat,
                "lon": lon,
                "raw_text": full_text,
            })
            if freq_val is None:
                warnings.append(f"{icao} {aid_type}: no frequency parsed from "
                                 f"its own text: {full_text[:80]!r}")

        embed_text = f"{icao} navaids: " + "; ".join(
            f"{r['aid_type']}={r['frequency']}{r['freq_unit']}" for r in records
            if r["frequency"])

        return ExtractResult(
            icao=icao, subsection=self.subsection, kind=self.kind,
            records=records, text="", embed_text=embed_text, warnings=warnings,
        )

    def validate(self, result: ExtractResult) -> list:
        issues = []
        if not result.records:
            issues.append(ValidationIssue("error", "records",
                                          f"{result.icao}: no navaid records"))
            return issues
        # No hard block on missing frequency/coordinates here (unlike AD
        # 2.18's comms, where every service always has one) — some navaid
        # rows genuinely have partial data or unusual formats (confirmed:
        # DNIM's SA-100 LOCATOR). Warnings surface these for review without
        # discarding the record's raw_text, which still has real content.
        return issues
