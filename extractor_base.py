"""
extractor_base.py — Layer 2 contract.

Every AD 2.x subsection extractor is a plugin implementing ONE interface, so the
ingestion pipeline treats them uniformly and each can be built and validated in
isolation (strict AD 2.1 -> 2.24 order).

DESIGN DECISIONS LOCKED EARLIER IN THIS PROJECT:
  - Hybrid storage: a "tabular" extractor emits typed records (rows for a typed
    Supabase table); a "text" extractor emits cleaned prose. BOTH always emit
    embed_text so semantic search covers everything (embeddings live only in
    aip_knowledge_base; typed tables reference back by icao+subsection).
  - null-over-guess is a HARD rule: when the source does not cleanly provide a
    field, the record carries None for it. An extractor NEVER fabricates a value
    to fill a column. validate() decides, per field, whether None is acceptable
    (e.g. runway dimensions: never null; surface: null allowed).
  - validate() runs AT INGEST and a FAILURE BLOCKS that record from being
    stored. Breakage is caught before it reaches the database, not discovered in
    production (the entire reason this rebuild exists).

INPUT: an extractor receives the list of Segment objects (from segment_page.py)
belonging to its subsection for ONE aerodrome — already cut, already
continuation-joined, already tagged excluded/not. It does not re-read pages or
re-derive page identity.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ExtractResult:
    icao: str
    subsection: str                 # e.g. "2.12"
    kind: str                       # "tabular" | "text"
    records: list = field(default_factory=list)   # list[dict] for tabular; [] for text
    text: str = ""                  # cleaned prose for text kind; "" for tabular
    embed_text: str = ""            # what gets embedded — ALWAYS present
    warnings: list = field(default_factory=list)   # non-fatal notes for the audit
    excluded: bool = False          # True for subsections excluded by decision (2.22)


@dataclass
class ValidationIssue:
    severity: str                   # "error" (blocks storage) | "warning" (surfaced only)
    field: str
    message: str


class SubsectionExtractor:
    """Base class. A concrete extractor sets `subsection` and `kind`, and
    implements extract() and validate()."""

    subsection: str = ""            # "2.1"
    kind: str = "text"              # "tabular" | "text"

    def extract(self, icao: str, segments: list) -> ExtractResult:
        """segments: list[Segment] for THIS subsection, THIS aerodrome (already
        cut and continuation-joined by segment_page). Returns an ExtractResult.
        Must never raise on ordinary malformed input — capture problems as
        warnings or as None fields, and let validate() decide severity."""
        raise NotImplementedError

    def validate(self, result: ExtractResult) -> list:
        """Return a list[ValidationIssue]. Any issue with severity 'error'
        blocks the whole result from being stored at ingest. Default: no issues
        (a text extractor with non-empty text is valid)."""
        issues = []
        if result.kind == "text" and not result.excluded and not result.text.strip():
            issues.append(ValidationIssue("error", "text",
                                          "text-kind subsection produced empty text"))
        return issues

    # ── shared helpers available to all extractors ────────────────────────────

    @staticmethod
    def segment_text(segments) -> str:
        """Flatten a subsection's segments into one whitespace-normalised
        string, in page order, dropping any excluded segments."""
        parts = []
        for s in sorted(segments, key=lambda s: s.page_index):
            if getattr(s, "excluded", False):
                continue
            parts.append(" ".join(w[4] for w in s.words))
        return " ".join(" ".join(parts).split())

    @staticmethod
    def combine_segment_words(segments):
        """Combine words from possibly-multiple Segment objects (one subsection
        can span several physical pages) into ONE correctly-ordered word list,
        excluding any segment tagged excluded.

        REAL BUG, found building AD 2.5: a word's 'top' coordinate resets near
        zero on every new page — it is NOT a running position across a whole
        subsection. A naive flat concatenation of segments' words, sorted (as
        parse_key_value_rows does) by raw top value, will interleave pages
        incorrectly whenever a later page's early rows have SMALLER top values
        than an earlier page's later rows. Confirmed directly: DNMM's AD 2.5
        spans pages 851 (rows 1-2, "Hotels"/"Restaurants", ending near the
        bottom of that page) and 852 (row 3 "Transportation" onward, starting
        near the TOP of that page) — flat concatenation put page 852's "3
        Transportation" ahead of page 851's "1 Hotels"/"2 Restaurants" in the
        final field order, and caused a page-852 continuation of the SAME
        subsection's own repeated header to be swallowed into a field's value.

        Fix: offset every word's top coordinate by a large, page-index-based
        constant BEFORE any position-based logic runs, so page order is always
        the PRIMARY sort key and within-page position is secondary — exactly
        matching true reading order. The offset (2000pt) safely exceeds any
        real page height seen in this document (the tallest confirmed page,
        a chart/map page, is 1191pt)."""
        PAGE_OFFSET = 2000.0
        out = []
        for s in segments:
            if getattr(s, "excluded", False):
                continue
            shift = s.page_index * PAGE_OFFSET
            for w in s.words:
                out.append((w[0], w[1] + shift, w[2], w[3] + shift, w[4]))
        return out

    @staticmethod
    def clean_text(s: str) -> str:
        """Strip Unicode Private-Use-Area characters (U+E000-U+F8FF) and other
        non-printable glyphs that some AIP-authoring fonts embed as invisible
        spacing/icon placeholders. Confirmed directly: DNBC's own longitude
        word extracts as '0094438E\\uf020' — a PUA character glued onto the
        real data with no separating space. Applied before any regex matching
        so such characters can never silently affect word geometry or pattern
        matching downstream."""
        import re
        return re.sub(r'[\ue000-\uf8ff]', '', s)

    @staticmethod
    def parse_key_value_rows(words, page_width=595.2):
        """Split ONE subsection's words into [(label, value), ...] pairs, for
        AD 2.2/2.3/2.4-style numbered key-value pages (each row: a bare
        left-margin integer, a label, a value in the right column).

        Ported from the validated logic in extract_page_text_fixed.py
        (_detect_gutter / _emit_region), which was proven across all 36
        aerodromes including the format anomalies that logic exists to handle
        (e.g. DNET's superscript-reordering fix). segment_page.py already
        isolates one subsection's own words per Segment — unlike the original
        page-level version, this never needs to find where AD 2.2 starts/ends
        on the page, only to pair labels with values within an already-cut
        segment.

        page_width: the AIP's admin/text pages are consistently 595.2pt wide
        (A4) — confirmed directly and repeatedly across this project (chart
        pages use a different, larger size, but AD 2.2/2.3/2.4 never do, since
        classify_page routes chart-shaped pages to CHART_PLATE/CHART_INDEX,
        never AD_CONTENT). Gutter detection is relative to this width.
        """
        import re
        INT_RE = re.compile(r'\d{1,2}')
        ROWNUM_MAX_XFRAC = 0.16
        LINE_TOL = 4.0

        def detect_gutter(cell, width):
            lo, hi = 0.30 * width, 0.66 * width
            xs = sorted((w[0], w[2]) for w in cell)
            best_gap, gutter = 0.0, width / 2.0
            right_edge = None
            for x0, x1 in xs:
                if right_edge is not None and lo < (x0 + right_edge) / 2 < hi:
                    gap = x0 - right_edge
                    if gap > best_gap:
                        best_gap, gutter = gap, (x0 + right_edge) / 2
                right_edge = max(right_edge, x1) if right_edge is not None else x1
            return gutter

        def line_order(ws):
            """Reading order within a cell: cluster by RELATIVE proximity in
            top-position, not by rounding to an absolute grid. Rounding
            (round(top / BAND)) always has a hard boundary somewhere no
            matter how wide BAND is — a word a fraction of a point past that
            boundary sorts into the wrong band entirely. Confirmed as a real,
            reproduced bug: DNBC's longitude word extracts with top=115.3
            while every other word on the SAME visual row sits at top=116.1
            (a font-metric quirk from an embedded Private-Use-Area glyph —
            the same underlying class of issue as DNET's superscript-°C
            shift found earlier in this project). The old round(top/8.0) key
            put 115.3 and 116.1 in different bands (the boundary falls at
            exactly top=116.0), sorting the longitude BEFORE the latitude —
            "0094438E 102858N" instead of "102858N 0094438E" — which broke
            the coordinate regex entirely, since it requires lat-then-lon
            order. Clustering by "is this word within LINE_TOL of the
            current line's reference top" has no such fixed boundary: a
            0.8pt gap is absorbed regardless of where it falls.
            """
            ws = sorted(ws, key=lambda w: w[1])
            lines, cur, ref = [], [], None
            for w in ws:
                if ref is None or abs(w[1] - ref) <= LINE_TOL:
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

        anchors = sorted(
            (w for w in words if INT_RE.fullmatch(w[4]) and w[2] < ROWNUM_MAX_XFRAC * page_width),
            key=lambda w: w[1],
        )
        if not anchors:
            return []

        # Gutter detection must only see the actual TABLE ROWS, never the
        # subsection title line above them. Confirmed as a real bug: DNFB's
        # (and others') "DNFB AD 2.4 HANDLING SERVICES AND FACILITIES" header
        # spans continuously from x0=218 to x1=419.7 — directly through the
        # 0.30-0.66 width band detect_gutter scans for the widest gap. With
        # the header word set included, that continuous span filled in what
        # should have been the natural label/value gap, shifting the computed
        # gutter far enough that every value on the page (short strings like
        # "Not available", "NIL") landed on the wrong side. Restricting the
        # gutter scan to words at or below the first row anchor's top
        # excludes the header entirely — it's always above row 1.
        first_row_top = anchors[0][1]
        table_words = [w for w in words if w[1] >= first_row_top - 2.0]

        # Gutter must be computed PER PAGE, not once globally — confirmed a
        # real, user-caught bug: DNMK's AD 2.11 spans two pages with
        # genuinely different column layouts (page 1's values start at
        # x0=266, page 2's at x0=309). A single global gutter, computed
        # across both pages' combined words, came out at x0=297.6 — high
        # enough that EVERY value on page 1 (all sitting below 297.6) was
        # misclassified as label text, producing exactly the "short value
        # bled into the label" symptom this project has chased before as if
        # it were a font/whitespace quirk. It's a real gutter-computation
        # bug. combine_segment_words already offsets each page's words by a
        # PAGE_OFFSET-sized block for ordering purposes — that same offset
        # is reused here to identify which page-group a word belongs to,
        # so each page's own rows are split against its own gutter, not a
        # blended one.
        PAGE_OFFSET = 2000.0
        pages_present = sorted({int(w[1] // PAGE_OFFSET) for w in table_words})
        gutter_by_page = {
            p: detect_gutter([w for w in table_words if int(w[1] // PAGE_OFFSET) == p],
                              page_width)
            for p in pages_present
        }

        tops = [a[1] for a in anchors] + [float("inf")]
        out = []
        for i, a in enumerate(anchors):
            lo, hi = a[1] - 2.0, tops[i + 1] - 2.0
            body = [w for w in words if lo <= w[1] < hi and w is not a]
            row_page = int(a[1] // PAGE_OFFSET)
            gutter = gutter_by_page.get(row_page, page_width / 2.0)
            label = " ".join(w[4] for w in line_order(
                [w for w in body if (w[0] + w[2]) / 2 < gutter]))
            value = " ".join(w[4] for w in line_order(
                [w for w in body if (w[0] + w[2]) / 2 >= gutter]))
            if (label + value).strip():
                out.append((label.strip(), value.strip()))
        return out

    @staticmethod
    def parse_full_rows(words, page_width=595.2):
        """Like parse_key_value_rows, but returns each numbered row's COMPLETE
        text — label side and value side combined, in correct reading order —
        rather than splitting on the gutter. For fields that are semantically
        one cohesive multi-line block, not a genuine label:value pair.

        Confirmed necessary directly on AD 2.2's field 6 (operator info,
        address/telephone/telefax/AFS/AFTN/SITA block): its own sub-label
        "AFS" genuinely prints at the SAME far-left x-position as the row's
        own field label ("6 AD Administration...", both at x0=103.1 on
        DNBC), while other sub-labels in the SAME field ("TEL:", "AFTN:")
        print at the normal value-column position. Gutter-based splitting
        can only make ONE binary left/right decision per word position — it
        has no way to know "AFS" is semantically value content just because
        OTHER sub-labels in the same block happen to sit further right.
        Checked empirically: this cost real data on 13 of the first 18
        aerodromes checked (the word "AFS" silently dropped from
        operator_info every time). The fix isn't a better gutter — it's not
        trying to split this field at all. The caller is expected to strip
        the field's own known label prefix (which IS consistent) from the
        front of each returned row's text."""
        import re
        INT_RE = re.compile(r'\d{1,2}')
        ROWNUM_MAX_XFRAC = 0.16
        LINE_TOL = 4.0

        def line_order(ws):
            ws = sorted(ws, key=lambda w: w[1])
            lines, cur, ref = [], [], None
            for w in ws:
                if ref is None or abs(w[1] - ref) <= LINE_TOL:
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

        anchors = sorted(
            (w for w in words if INT_RE.fullmatch(w[4]) and w[2] < ROWNUM_MAX_XFRAC * page_width),
            key=lambda w: w[1],
        )
        if not anchors:
            return []

        tops = [a[1] for a in anchors] + [float("inf")]
        out = []
        for i, a in enumerate(anchors):
            lo, hi = a[1] - 2.0, tops[i + 1] - 2.0
            body = [w for w in words if lo <= w[1] < hi and w is not a]
            text = " ".join(w[4] for w in line_order(body))
            if text.strip():
                out.append(text.strip())
        return out
