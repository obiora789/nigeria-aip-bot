"""
ad220_extractor.py — AD 2.20 LOCAL AERODROME REGULATIONS.

Twentieth Layer 2 subsection extractor. Tabular kind, one record per
declared item — canonical-label matching (like AD 2.3/2.4/2.9), because
item count and exact numbering genuinely vary across aerodromes (confirmed:
36 aerodromes show field counts ranging from 7 to 10, and position 2.20.10
specifically holds two DIFFERENT concepts across the dataset — "Fuel
spillage" on some aerodromes, "Movement on APN and taxiing strips" on
others — so position number alone cannot reliably identify a field; label
text must).

A REAL, GENUINE TWO-COLUMN PAGE LAYOUT — confirmed by direct word position
inspection, not assumed: items 2.20.1-2.20.5 print in a LEFT column
(x0≈70.8), items 2.20.6-2.20.10 print in a RIGHT column (x0≈328.8), side by
side on the same visual rows. A naive line-based flattening (walking the
page top-to-bottom without column awareness) merges labels and values
across BOTH columns onto the same accumulated text — confirmed directly:
an early attempt produced "Airport regulations 2.20.6 Taxiing -
limitations" as ONE label instead of two separate ones. Fixed by bucketing
every word into LEFT or RIGHT column first (via a page-width gutter
threshold), then walking each column's OWN lines independently to detect
"2.20.N LABEL" markers and accumulate each item's own value — the same
"never let column boundaries cross" principle already used for AD 2.10's
column-aware rebuild.
"""
import re

from extractor_base import SubsectionExtractor, ExtractResult, ValidationIssue

ITEM_START_RE = re.compile(r'^2\.20\.(\d+)\s+(.*)')
# Gutter is computed dynamically per aerodrome, not hardcoded — confirmed
# necessary: a fixed x0=250 threshold cut into the LEFT column's own
# wrapped text. DNAA's item 2.20.3 label wraps "(General" / "aviation)"
# across two lines, and "aviation)" itself renders at x0=260.4 — past a
# naive 250 threshold, causing it to be misbucketed into the right column
# entirely. The true gap sits between the left column's widest extent
# (~260-280) and the right column's start (~328.8); computing the actual
# widest gap (the same technique already proven in parse_key_value_rows)
# finds this reliably rather than guessing a single global threshold.

CANONICAL_ITEMS = [
    (re.compile(r'^Airport regulations', re.I), "airport_regulations"),
    (re.compile(r'^Taxiing to and from stands', re.I), "taxiing_to_from_stands"),
    (re.compile(r'^Parking area for small aircraft', re.I), "parking_small_aircraft"),
    (re.compile(r'^Parking area for helicopters', re.I), "parking_helicopters"),
    (re.compile(r'^Apron.*winter conditions', re.I), "apron_winter_conditions"),
    (re.compile(r'^Taxiing.*limitations', re.I), "taxiing_limitations"),
    (re.compile(r'^School and training flights', re.I), "training_flights_rwy_use"),
    (re.compile(r'^Helicopter traffic', re.I), "helicopter_traffic_limitation"),
    (re.compile(r'^Removal of disabled aircraft', re.I), "removal_disabled_aircraft"),
    (re.compile(r'^Fuel [Ss]pillage', re.I), "fuel_spillage"),
    (re.compile(r'^Movement on APN', re.I), "apron_taxiing_strip_movement"),
]


def _canonicalize(label):
    for pattern, key in CANONICAL_ITEMS:
        if pattern.match(label):
            return key
    return None


class AD220Extractor(SubsectionExtractor):
    subsection = "2.20"
    kind = "tabular"

    def extract(self, icao: str, segments: list) -> ExtractResult:
        from segment_page import _line_groups
        all_words = self.combine_segment_words(segments)
        warnings = []

        if not all_words:
            warnings.append("no words found in AD 2.20 segment")
            return ExtractResult(
                icao=icao, subsection=self.subsection, kind=self.kind,
                records=[], text="", embed_text="", warnings=warnings,
            )

        # Dynamic gutter: widest gap in the middle band of the page width,
        # the same technique already proven in parse_key_value_rows —
        # confirmed necessary here too (see module docstring: a fixed
        # threshold cut into the left column's own wrapped text).
        page_width = 595.2
        lo, hi = 0.30 * page_width, 0.70 * page_width
        xs = sorted((w[0], w[2]) for w in all_words)
        best_gap, gutter = 0.0, page_width / 2.0
        right_edge = None
        for x0, x1 in xs:
            if right_edge is not None and lo < (x0 + right_edge) / 2 < hi:
                gap = x0 - right_edge
                if gap > best_gap:
                    best_gap, gutter = gap, (x0 + right_edge) / 2
            right_edge = max(right_edge, x1) if right_edge is not None else x1

        left_words = [w for w in all_words if w[0] < gutter]
        right_words = [w for w in all_words if w[0] >= gutter]

        def walk_column(words):
            lines = _line_groups(words)
            items = []  # list of (number, label, [value_lines])
            current = None
            is_first_value_line = False
            for line in lines:
                text = " ".join(w[4] for w in line)
                m = ITEM_START_RE.match(text)
                if m:
                    current = {"number": m.group(1), "label": m.group(2).strip(),
                               "value_lines": []}
                    items.append(current)
                    is_first_value_line = True
                elif current is not None:
                    # Wrapped label continuation: confirmed real (DNAA's
                    # "School and training flights...use" wraps "of RWYs"
                    # onto its own next line, and "(General" wraps
                    # "aviation)"). Checking canonicalization status doesn't
                    # work here — these labels already canonicalize
                    # successfully via prefix match on their FIRST line,
                    # before the continuation is even seen. A more reliable
                    # signal: only the very first candidate "value" line
                    # after an item starts is checked, and treated as a
                    # label continuation if short AND lowercase-starting —
                    # every genuine value in this dataset starts uppercase
                    # ("Available.", "NIL", "Not AVBL", "None, except..."),
                    # so a lowercase start is a reliable tell of a wrapped
                    # label fragment instead.
                    if (is_first_value_line and len(text.split()) <= 3
                            and text[:1].islower()):
                        current["label"] = (current["label"] + " " + text).strip()
                    else:
                        current["value_lines"].append(text)
                    is_first_value_line = False
            return items

        left_items = walk_column(left_words)
        right_items = walk_column(right_words)
        all_items = left_items + right_items

        if not all_items:
            warnings.append("no numbered 2.20.N items found in either column")

        records = []
        seen_keys = set()
        for item in all_items:
            label = self.clean_text(item["label"])
            value = self.clean_text(" ".join(item["value_lines"])) or None
            key = _canonicalize(label)
            if key is None:
                warnings.append(f"unrecognized item label (new in this AIRAC cycle?): "
                                 f"2.20.{item['number']} {label!r}")
            elif key in seen_keys:
                warnings.append(f"duplicate canonical item {key!r} "
                                 f"(2.20.{item['number']} {label!r}) — check for a "
                                 f"mislabeled or duplicated row")
            seen_keys.add(key)
            records.append({
                "icao": icao, "item": key, "item_number": item["number"],
                "raw_label": label, "detail": value,
            })

        embed_text = f"{icao} AD 2.20 local regulations: " + "; ".join(
            f"{r['raw_label']}={r['detail']}" for r in records)

        return ExtractResult(
            icao=icao, subsection=self.subsection, kind=self.kind,
            records=records, text="", embed_text=embed_text, warnings=warnings,
        )

    def validate(self, result: ExtractResult) -> list:
        issues = []
        if not result.records:
            issues.append(ValidationIssue("error", "records",
                                          f"{result.icao}: no AD 2.20 records produced"))
            return issues
        for rec in result.records:
            if rec["item"] is None:
                issues.append(ValidationIssue(
                    "error", "item",
                    f"{result.icao}: unrecognized item label {rec['raw_label']!r}"))
        return issues
