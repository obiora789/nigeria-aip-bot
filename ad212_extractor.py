"""
ad212_extractor.py — AD 2.12 RUNWAY PHYSICAL CHARACTERISTICS.

Twelfth Layer 2 subsection extractor, and the highest-stakes one so far —
this is the exact subsection the project's original misattribution incident
happened on (the "Abuja runway" query returning RWY 04's elevation spliced
with RWY 22's slope data, both from AD 2.12's dense multi-column table).

DESIGN, informed directly by that incident:
The core lesson was never "parse every column" — it was "never let one
runway end's data merge with another's." AD 2.12 is genuinely a 3-column-
group table (designation/bearing/dimensions/strength/coordinates/elevation;
then slope/RESA/CWY/strip dimensions; then OFZ/remarks), each group printing
its OWN runway-end row markers ("04"/"22" or "RWY 04"/"RWY 22") separately.
Confirmed directly on DNAA (the original incident aerodrome): all three
groups are present, each cleanly re-stating which end ("04" vs "22") a row
belongs to.

So this extractor tracks the CURRENTLY ACTIVE runway end as it walks the
segment's lines top to bottom, resetting only when a new end-marker line is
seen — every line's text is bucketed under whichever end most recently
introduced it, regardless of which of the three column-groups it's in. This
makes cross-end merging structurally impossible: text can only ever attach
to the end whose own row most recently preceded it.

STRUCTURED FIELDS (reusing already-validated logic from earlier in this
project — the "Abuja runway" designation/dimensions parser, proven across
all 36 aerodromes including opposite-end pairing for DNKN's two physical
runways, DNMM's parallel runways, and DNCA's "GEO" token variant):
  - designation ("04/22"), length_m, width_m — from each end's own
    designation/dimensions line via HEAD_RE/DIM_RE, opposite-end paired via
    opp() into ONE record per PHYSICAL runway.
Everything else (coordinates, elevation, strength/PCN, slope, RESA, OFZ,
remarks) is preserved as free text, but SEPARATELY PER END within the
record — never combined into one string — so a reader can see "here is
04's data" and "here is 22's data" as two distinct fields, not one merged
block. This is deliberately less structured than AD 2.2's field-level
parsing, matching the AD 2.10/2.11 precedent: given this table's real
per-page column-position drift (the same issue found and NOT solved for
AD 2.10), the safe choice is correct attribution via ordering, not brittle
column-position parsing of every field.
"""
import re

from extractor_base import SubsectionExtractor, ExtractResult, ValidationIssue

HEAD_RE = re.compile(r"^(\d{1,2}[LRC]?)\s+\d{2,3}(?:\.\d+)?\s*°(?:\s*\d{1,2}\s*['\u2019])?", re.I)
RWY_HEAD_RE = re.compile(r"^RWY\s+(\d{1,2}[LRC]?)\b", re.I)
BARE_END_RE = re.compile(r"^(\d{1,2}[LRC]?)\b(?!\s*\.)")  # e.g. "04 1200m from threshold..."
DIM_RE = re.compile(r"(\d[\d ]{2,6}?\d|\d{3,4})\s*[xX]\s*(\d{2,3})\b")


def _opp(desig):
    m = re.match(r'(\d{1,2})([LRC]?)', desig, re.I)
    if not m:
        return None
    n = int(m.group(1))
    side = (m.group(2) or '').upper()
    o = ((n + 18 - 1) % 36) + 1
    flip = {'L': 'R', 'R': 'L', 'C': 'C', '': ''}
    return f"{o:02d}{flip.get(side, '')}"


def _norm_end(token):
    m = re.match(r'(\d{1,2})([LRC]?)', token, re.I)
    if not m:
        return None
    return f"{int(m.group(1)):02d}{(m.group(2) or '').upper()}"


class AD212Extractor(SubsectionExtractor):
    subsection = "2.12"
    kind = "tabular"

    def extract(self, icao: str, segments: list) -> ExtractResult:
        from segment_page import _line_groups
        all_words = self.combine_segment_words(segments)
        lines = _line_groups(all_words)
        warnings = []

        # Walk lines, bucketing text under whichever runway end most
        # recently introduced it. This is the load-bearing safety property:
        # cross-end merging is structurally impossible here.
        end_text = {}       # end -> list[str] (accumulated lines, in order)
        end_dims = {}        # end -> (length_m, width_m) from its own row
        current_end = None
        for line in lines:
            text = " ".join(w[4] for w in line)
            m1 = HEAD_RE.match(text)
            m2 = RWY_HEAD_RE.match(text)
            m3 = BARE_END_RE.match(text) if not m1 and not m2 else None
            new_end = None
            if m1:
                new_end = _norm_end(m1.group(1))
                dm = DIM_RE.search(text)
                if dm:
                    length = re.sub(r'\s+', '', dm.group(1))
                    width = dm.group(2)
                    if length.isdigit() and 400 <= int(length) <= 5000 and 15 <= int(width) <= 90:
                        end_dims[new_end] = (int(length), int(width))
            elif m2:
                new_end = _norm_end(m2.group(1))
            elif m3:
                # A bare leading number COULD be a real end-marker (group 3's
                # "04 1200m from threshold...") or could be unrelated numeric
                # text. Only trust it as a new-end marker if it matches an
                # end we've ALREADY seen from a real HEAD_RE/RWY_HEAD_RE
                # line — never invent a new end from an ambiguous bare number.
                candidate = _norm_end(m3.group(1))
                if candidate in end_text or candidate in end_dims:
                    new_end = candidate

            if new_end:
                current_end = new_end
                end_text.setdefault(current_end, [])
            if current_end:
                end_text.setdefault(current_end, []).append(text)

        if not end_dims:
            warnings.append("no runway designation/dimensions rows found at all")

        # Pair opposite ends into physical runways.
        seen = set()
        runways = []
        for end in end_dims:
            if end in seen:
                continue
            opp_end = _opp(end)
            pair_present = opp_end in end_dims
            seen.add(end)
            if pair_present:
                seen.add(opp_end)
            ends_sorted = sorted([end] + ([opp_end] if pair_present else []),
                                  key=lambda e: int(re.match(r'\d+', e).group()))
            designation = "/".join(ends_sorted)
            dims = end_dims.get(end) or (end_dims.get(opp_end) if pair_present else None)
            if pair_present and end_dims.get(end) != end_dims.get(opp_end):
                warnings.append(f"{icao} {designation}: dimensions differ between ends "
                                 f"({end}={end_dims.get(end)}, {opp_end}={end_dims.get(opp_end)}) "
                                 f"— using {end}'s value, verify against source")
            runways.append({
                "icao": icao,
                "designation": designation,
                "length_m": dims[0] if dims else None,
                "width_m": dims[1] if dims else None,
                "end_detail": {
                    e: self.clean_text(" ".join(end_text.get(e, [])))
                    for e in ends_sorted
                },
            })
            if not pair_present:
                warnings.append(f"{icao} {designation}: no reciprocal end found "
                                 f"(single-direction runway, or opposite end not detected)")

        runways.sort(key=lambda r: int(re.match(r'\d+', r["designation"]).group()))
        embed_text = f"{icao} runways: " + "; ".join(
            f"{r['designation']} {r['length_m']}x{r['width_m']}m" for r in runways
            if r["length_m"])

        return ExtractResult(
            icao=icao, subsection=self.subsection, kind=self.kind,
            records=runways, text="", embed_text=embed_text, warnings=warnings,
        )

    def validate(self, result: ExtractResult) -> list:
        issues = []
        if not result.records:
            issues.append(ValidationIssue("error", "records",
                                          f"{result.icao}: no runway records produced"))
            return issues
        for rec in result.records:
            # null-over-guess is a HARD requirement here specifically:
            # dimensions are the exact kind of number the original incident
            # was about. Never let a runway record through with a guessed or
            # missing length/width.
            if rec["length_m"] is None or rec["width_m"] is None:
                issues.append(ValidationIssue(
                    "error", "dimensions",
                    f"{result.icao} {rec['designation']}: missing length/width"))
        return issues
