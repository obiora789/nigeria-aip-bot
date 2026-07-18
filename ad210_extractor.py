"""
ad210_extractor.py — AD 2.10 AERODROME OBSTACLES.

Tenth Layer 2 subsection extractor. TEXT kind — deliberately NOT structured
per-obstacle records (see below for why), but REBUILT (round 2) after a
real, user-caught bug in the original naive-flattening version.

WHY TEXT, NOT FULLY STRUCTURED TYPED FIELDS — investigated, not a shortcut:
AD 2.10 is a genuine multi-column table (5-6 columns depending on sub-
section: a-f for 2.10.1's RWY/Area, OBST type, position, ELEV/HGT,
Markings, Remarks; a-d for 2.10.2, which drops the RWY/Area column), with
0-to-many obstacle rows each. Column x-positions DRIFT between sub-tables
on the same page (confirmed on DNAA: 2.10.1's markers at x0=[101,167,256,
350,427,516], 2.10.2's at x0=[101,171,262,363,450,528]), and some
aerodromes (DNBB, an unrefreshed "AIRAC AMDT 01/2018" page) lack the a-f
markers entirely. Full per-obstacle STRUCTURED decomposition robust to
every variant would need the same multi-round investigation the AD 2.12
runway-table fix took — not attempted here, matching the AD 2.22 precedent
(this data is also duplicated by the official Aerodrome Obstacle Chart,
the authoritative visual reference for obstacle clearance planning).

BUG FOUND AND FIXED (round 2), confirmed directly by a user cross-checking
DNES's real page against the extracted text: the original version flattened
words by raw Y-position-then-X-position across the WHOLE row width, which
works fine for single-line cells but breaks when adjacent columns wrap to
DIFFERENT numbers of lines. Confirmed exactly on DNES: one obstacle row has
its Markings/LGT cell wrap to 2 lines ("Marked and" / "lighted") while its
Remarks cell wraps to 4 lines — the SECOND line of Markings/LGT and the
SECOND line of Remarks land at the same Y-position, and naive flattening
sorted them together by X, producing "...about 1000 lighted m left of RWY
31..." — a genuinely misleading scramble, not just untidy.

FIX: bucket each row's words by nearest COLUMN (using the page's own a-f/
a-d marker x-positions, the same proximity technique used in AD 2.13's
fix), assemble each column's FULL text across all its own wrapped lines
FIRST, then join columns in order — this makes cross-column interleaving
structurally impossible, the same safety principle already used for
AD 2.12/2.14/2.18's per-entity text scoping, applied here to per-COLUMN
scoping within a row. Row boundaries are detected via column b (Obstacle
type — present in BOTH 2.10.1's 6-column and 2.10.2's 5-column layout,
unlike column a which 2.10.2 lacks): a new row starts whenever column b
gets a new word at a Y-position clearly separated from the current row's
own content.

Aerodromes lacking the a-f/a-d markers (confirmed: DNBB) fall back to the
original per-line flattening — a documented, narrower residual risk,
rather than blocking extraction entirely.
"""
import re

from extractor_base import SubsectionExtractor, ExtractResult, ValidationIssue

SUBSUBSECTION_RE = re.compile(r'^(2\.10\.\d+)\b')
COLUMN_MARKER_RE = re.compile(r'^[a-f]$')
ROW_GAP = 15.0   # a column-b word starting a new row must be at least this
                  # far below the current row's own Y-extent — smaller gaps
                  # are a wrapped continuation line, not a new obstacle.


def _reconstruct_subtable(words):
    """words: one sub-section's (2.10.1 or 2.10.2) own words, INCLUDING its
    column markers. Returns reconstructed text with rows correctly
    column-bucketed, or None if no markers were found (caller should fall
    back to naive flattening)."""
    markers = [w for w in words if COLUMN_MARKER_RE.match(w[4])]
    if not markers:
        return None
    # keep only the marker ROW itself (markers all share one Y-band) —
    # guards against a stray single-letter word elsewhere being mistaken
    # for a column marker.
    marker_top = sorted(markers, key=lambda w: w[1])[len(markers) // 2][1]
    markers = [w for w in markers if abs(w[1] - marker_top) < 3.0]
    if len(markers) < 3:
        return None
    markers = sorted(markers, key=lambda w: w[0])
    col_letters = [w[4] for w in markers]
    col_xs = [w[0] for w in markers]
    boundaries = ([-1e9] + [(col_xs[i] + col_xs[i + 1]) / 2
                             for i in range(len(col_xs) - 1)] + [1e9])

    def bucket_for(x0):
        for i in range(len(col_letters)):
            if boundaries[i] <= x0 < boundaries[i + 1]:
                return i
        return len(col_letters) - 1

    # The row-boundary anchor column is whichever one's HEADER TEXT says
    # "Obstacle" — NOT whichever is labeled 'b'. Confirmed a real, separate
    # bug from the ordering fix above: 2.10.1's layout has RWY/Area as
    # column 'a' and Obstacle type as 'b', but 2.10.2 generally DROPS the
    # RWY/Area column, so ITS Obstacle-type column is labeled 'a' instead.
    # A hardcoded "always use column b" assumption silently anchored row-
    # boundary detection on 2.10.2's Markings/LGT column instead — which is
    # mostly sparse "NIL"/"Lighted" values, not present on every row — and
    # that caused genuinely separate obstacles to merge into one row.
    # Confirmed directly on DNSU: five distinct obstacles (GP OBS, LLZ
    # MONITOR, DME, WINDSLEEVE, MET MAST), each with its own elevation and
    # coordinates, were merged into a single reconstructed row.
    header_words = [w for w in words if w[1] < marker_top - 2.0]
    obstacle_header = next((w for w in header_words if w[4].startswith('Obstacle')
                             or w[4].startswith('OBST')), None)
    if obstacle_header is not None:
        b_index = min(range(len(col_xs)), key=lambda i: abs(col_xs[i] - obstacle_header[0]))
    else:
        b_index = col_letters.index('b') if 'b' in col_letters else 0

    # Only words BELOW the marker row are real data.
    data_words = sorted((w for w in words if w[1] > marker_top + 2.0),
                         key=lambda w: (w[1], w[0]))

    # PASS 1: establish row boundaries from the Obstacle-type column ALONE.
    # Confirmed necessary — a single-pass approach (detecting row starts
    # while also bucketing every word in one sweep, sorted by Y-then-X) has
    # a real ordering bug: at a shared Y-position, column a's words sort
    # before column b's (lower x), so column a's new-row content gets
    # appended to the still-open PREVIOUS row before column b's word ever
    # triggers the new row. Confirmed directly on DNES: row 2's own "RWY
    # 13/APCH" (column a) ended up merged into row 1, leaving row 2 with
    # no designation at all. Determining row boundaries independently of
    # word-processing
    # order removes the bug entirely.
    b_words = sorted((w for w in data_words if bucket_for(w[0]) == b_index),
                      key=lambda w: w[1])
    row_boundaries = []
    prev_top = None
    for w in b_words:
        if prev_top is None or w[1] - prev_top > ROW_GAP:
            row_boundaries.append(w[1])
        prev_top = w[1]
    if not row_boundaries:
        return None

    def row_for(top):
        idx = 0
        for i, bound in enumerate(row_boundaries):
            if top >= bound - 2.0:
                idx = i
        return idx

    # PASS 2: assign every word to its row band (established above) and its
    # column bucket, independent of processing order. Store full word
    # tuples (not just text) — needed for the proximity-based re-sort below.
    rows = [{i: [] for i in range(len(col_letters))} for _ in row_boundaries]
    for w in data_words:
        r = row_for(w[1])
        c = bucket_for(w[0])
        rows[r][c].append(w)

    def _proximity_order(ws, tol=2.0):
        """Sort a single column's own words into correct reading order.
        NOT a naive sort by (top, x0) — confirmed a real bug on DNSU: two
        words ("ELEV", "17.11") render at top=640.4 while three others on
        the SAME visual line ("4", "m/56.148", "ft") render at top=640.0 —
        a 0.4pt font sub-pixel difference, the same class of rendering
        quirk found repeatedly elsewhere in this project (DNET's
        superscript shift, DNBC's PUA-glyph longitude shift). A plain
        tuple sort treats 640.0 as strictly before 640.4 and puts the
        wrong words first, producing "TREE 4 m/56.148 ft ELEV 17.11"
        instead of "TREE ELEV 17.114 m/56.148 ft". Clustering by relative
        proximity (words within `tol` of each other belong to the same
        line) before sorting by x within each cluster avoids this
        entirely — the same principle as extractor_base.py's line_order,
        applied here since this function doesn't share that helper."""
        ws = sorted(ws, key=lambda w: w[1])
        lines, cur, ref = [], [], None
        for w in ws:
            if ref is None or abs(w[1] - ref) <= tol:
                cur.append(w)
                if ref is None:
                    ref = w[1]
            else:
                lines.append(cur)
                cur, ref = [w], w[1]
        if cur:
            lines.append(cur)
        out = []
        for line in lines:
            out.extend(sorted(line, key=lambda w: w[0]))
        return out

    out_lines = []
    for row in rows:
        parts = []
        for i, letter in enumerate(col_letters):
            ordered = _proximity_order(row.get(i, []))
            text = " ".join(w[4] for w in ordered)
            if text:
                parts.append(text)
        if parts:
            out_lines.append(" | ".join(parts))
    return "\n".join(out_lines)


class AD210Extractor(SubsectionExtractor):
    subsection = "2.10"
    kind = "text"

    def extract(self, icao: str, segments: list) -> ExtractResult:
        from segment_page import _line_groups
        all_words = self.combine_segment_words(segments)
        warnings = []

        # Split into sub-sections (2.10.1, 2.10.2, ...) by locating their
        # own header lines, so each can get its OWN column-marker detection
        # — confirmed necessary, not cosmetic: column positions drift
        # between sub-tables even on the same page (see docstring).
        lines = _line_groups(all_words)
        section_starts = []
        for i, line in enumerate(lines):
            text = " ".join(w[4] for w in line)
            m = SUBSUBSECTION_RE.match(text)
            if m:
                section_starts.append((i, m.group(1)))

        sections_text = {}
        if section_starts:
            for j, (i, label) in enumerate(section_starts):
                end_i = section_starts[j + 1][0] if j + 1 < len(section_starts) else len(lines)
                section_words = [w for line in lines[i:end_i] for w in line]
                reconstructed = _reconstruct_subtable(section_words)
                if reconstructed is None:
                    warnings.append(f"{label}: no column markers found — "
                                     f"falling back to per-line text (residual "
                                     f"cross-column interleaving risk)")
                    reconstructed = self.clean_text(
                        " ".join(w[4] for w in section_words))
                sections_text[label] = self.clean_text(reconstructed)
        else:
            warnings.append("no 2.10.1/2.10.2 sub-section markers found — "
                             "storing whole segment as undivided text")

        full_text = "\n\n".join(f"{label}\n{text}" for label, text in sections_text.items())
        if not sections_text:
            full_text = self.clean_text(" ".join(w[4] for w in all_words))

        if not full_text.strip():
            warnings.append("AD 2.10 segment produced no text at all")

        return ExtractResult(
            icao=icao, subsection=self.subsection, kind=self.kind,
            records=[], text=full_text, embed_text=full_text,
            warnings=warnings,
        )

    def validate(self, result: ExtractResult) -> list:
        issues = []
        if not result.text.strip():
            issues.append(ValidationIssue("error", "text",
                                          f"{result.icao}: AD 2.10 produced no text"))
        return issues

