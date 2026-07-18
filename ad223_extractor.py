"""
ad223_extractor.py — AD 2.23 ADDITIONAL INFORMATION.

Twenty-third Layer 2 subsection extractor. TEXT kind — one prose block per
aerodrome. In this AIP the content is uniformly a bird/animal-hazard advisory,
a single "2.23.1 <template label> <remark>" item (a handful of aerodromes add
2.23.1.1 / 2.23.1.2 sub-remarks; DNPO has two). Two real, evidence-driven
problems this extractor solves — neither reasoned from structure, both
confirmed by direct word-position inspection of the source PDF:

(1) MULTI-COLUMN READING SCRAMBLE (confirmed on DNKN, DNMA, DNEN, DNYO, DNJO,
    DNPO — i.e. the MAJORITY of the substantive entries, not an outlier). The
    item's value prose is laid out in a right-hand column whose LAST wrapped
    line frequently shares a top-y with the LABEL's first line, so a naive
    (top, x0) flatten interleaves the value's tail into the middle of the
    label. Confirmed exactly on DNKN: "during taxiing, landing and take-off."
    renders at top=210 x0=286 — level with, and to the right of, the label's
    own first line ("2.23.1 Bird concentrations in the vicinity of the",
    ending x1=266) — while the value's MAIN text sits lower at top=236. Sorting
    by (top, x0) produces "...vicinity of the during taxiing, landing and
    take-off. aerodrome Concentration of birds...". Fixed by REGION-AWARE
    reading: within each numbered item, read every word left of the item's own
    detected gutter in reading order, THEN every word right of it —
    structurally the same "never let column boundaries cross" principle used in
    AD 2.10's per-column rebuild and AD 2.20's two-column walk. Degenerates
    safely to plain top-to-bottom on single-column aerodromes (DNAA, DNMM),
    which have no right column and no in-band gutter at all.

(2) CHARTS-INDEX ORPHAN (confirmed on DNMA, page 827). A few aerodromes print
    the whole AD 2.23 block on the SAME physical page as the AD 2.24 charts
    index. classify_page (correctly, and by design) routes any page bearing the
    "CHARTS RELATED TO AN AERODROME" heading to CHART_INDEX, and segment_page
    skips CHART_INDEX pages entirely — so that aerodrome yields NO 2.23 segment
    and its additional-information text is silently lost (DNMA's real
    bird/animal advisory was dropped completely before this extractor existed).
    salvage_from_chart_index() recovers it: on a charts-index page the AD 2.23
    block, when present, sits in the band BETWEEN its own "AD 2.23 ADDITIONAL
    INFORMATION" header and the "CHARTS RELATED TO..." heading. The driver
    (validator / ingestion) calls this only when normal segmentation produced
    no 2.23 segment, and feeds the recovered words back through the same
    extract() path as a synthetic segment.

    This is deliberately NOT a segment_page change: orphaned-onto-a-chart-index
    content is unique to 2.23 (the last text subsection before the charts), so
    pushing it into shared Layer 1 would force a full 20-extractor
    re-regression for a case none of them touch. A documented, localized
    recovery — the same philosophy as AD 2.10's marker-less fallback.

    DNYO is deliberately recovered as NOTHING here: its page-1004 "AD 2.23" is a
    MIS-LABELLED charts index ("DNYO AD 2.23 CHARTS RELATED TO AN AERODROME" —
    the source printed 2.23 where 2.24 belongs), carrying no additional-info
    content, and its real 2.23 is wholly on page 1003 (segments normally). The
    salvage band (2.23-header -> charts-heading) is empty there, so salvage
    correctly returns nothing rather than swallowing the charts listing.

NULL-OVER-GUESS: letter-split source artifacts ("th e" for "the", "cautio n"
for "caution", "exercis e") are preserved verbatim rather than re-glued —
gluing trailing single letters is a speculative transform that could corrupt
genuine content, and this is advisory prose for semantic search / display, not
an operational value. Faithful-to-source beats tidy-but-invented.
"""
import re

from extractor_base import SubsectionExtractor, ExtractResult, ValidationIssue

PAGE_WIDTH = 595.2
ROWNUM_MAX_XFRAC = 0.16
LINE_TOL = 4.0
GUTTER_MIN_GAP = 14.0  # a real column split needs at least this wide a gap in
                        # the mid-band AND words on both sides; below it, treat
                        # the item as single-column (plain reading order).
                        # Data-driven, measured across all 36: single-column
                        # 2.23 blocks top out at an 8.2pt inter-word gap (DNKS's
                        # "the"->"vicinity"), while every genuine two-column /
                        # value-in-right-column layout has a >=19.8pt gutter —
                        # a clean bimodal separation with nothing in between, so
                        # 14pt discriminates with wide margin either side. Set
                        # too low (8pt), DNKS's ordinary word spacing was
                        # mistaken for a column and its value scrambled.

# A top-level numbered item marker: "2.23.1", "2.23", or the split "2.23."
# (confirmed on DNAS, where the number renders as two words "2.23." + "1").
# NOT "2.23.1.1" — those two-extra-dot sub-remarks stay inline as content
# within their parent item (confirmed desired on DNEN/DNPO).
_ITEM_ANCHOR_RE = re.compile(r'^2\.23\.?\d?$')
_CHARTS_HEADING_RE = re.compile(r'CHARTS?\s+RELATED\s+TO\s+AN?\s+(?:AERODROME|HELIPORT)',
                                re.IGNORECASE)
_H223_RE = re.compile(r'AD\s+2\.23\b', re.IGNORECASE)


def _reading_order(words):
    """Reading order within one region: cluster into visual lines by relative
    top-proximity (LINE_TOL), then sort each line by x0. Same clustering as
    extractor_base.line_order — no fixed top-band boundary, so a sub-pixel
    font shift can't reorder a line (the DNBC/DNET class of bug)."""
    ws = sorted(words, key=lambda w: w[1])
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
    return " ".join(w[4] for w in out)


def _region_read(words, page_width=PAGE_WIDTH):
    """Read left-of-gutter region fully (reading order), then right-of-gutter
    region — the fix for the value-tail-interleaves-the-label scramble.

    The gutter is the LEFTMOST clean vertical corridor (a mid-band whitespace
    band that no word crosses), NOT the widest single gap. This distinction is
    load-bearing: on DNGO the widest mid-band gap (27.4pt) sits between the
    right column's "2.23.2" label and its own text "Stray", while the TRUE
    column boundary is a narrower corridor (19.9pt) further left, between the
    left column's "...th e" and the right column's "2.23.2". Picking the widest
    gap split "2.23.2" away from "Stray animals..."; picking the leftmost clean
    corridor keeps each column whole. Built by merging every word's x-interval
    and taking the first inter-range gap that is wide enough and mid-band —
    which structurally guarantees words on both sides. Degenerates to plain
    top-to-bottom on single-column aerodromes (no qualifying corridor)."""
    if not words:
        return ""
    lo, hi = 0.30 * page_width, 0.66 * page_width

    merged = []
    for x0, x1 in sorted((w[0], w[2]) for w in words):
        if merged and x0 <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], x1)
        else:
            merged.append([x0, x1])

    gutter = None
    for i in range(len(merged) - 1):
        gap_lo, gap_hi = merged[i][1], merged[i + 1][0]
        mid = (gap_lo + gap_hi) / 2
        if gap_hi - gap_lo >= GUTTER_MIN_GAP and lo < mid < hi:
            gutter = mid           # leftmost qualifying corridor
            break

    if gutter is not None:
        left = [w for w in words if (w[0] + w[2]) / 2 < gutter]
        right = [w for w in words if (w[0] + w[2]) / 2 >= gutter]
        if left and right:
            return (_reading_order(left) + " " + _reading_order(right)).strip()
    return _reading_order(words)


def _reconstruct(words):
    """Reconstruct AD 2.23 prose from its words.

    Drops the "ICAO AD 2.23 ADDITIONAL INFORMATION" title line(s), then
    region-reads the WHOLE remaining block (left column in reading order, then
    right column). Region-reading the whole block — rather than pre-splitting
    on "2.23.N" anchors — is deliberate and evidence-driven: DNGO prints TWO
    top-level items in TWO columns on the SAME visual rows (2.23.1 + 2.23.1.1
    left, 2.23.2 right), so anchors 2.23.1 and 2.23.2 share a top and an
    anchor-band split collapses (the 2.23.1 band becomes zero-height). A single
    region-read reproduces DNGO's two columns correctly AND still fixes the
    single-item value-tail scramble (DNKN/DNMA) and reads single-column
    aerodromes (DNAA) straight down — one mechanism, every layout.
    """
    from segment_page import _line_groups
    lines = _line_groups(words)
    kept = []
    for line in lines:
        text = " ".join(w[4] for w in line)
        # The subsection TITLE line is the only one carrying "AD 2.23" (the
        # numbered items are "2.23.N" with no preceding "AD"). Drop it — including
        # the "Nil"-only case (DNET) that has no numbered item to anchor on.
        if _H223_RE.search(text):
            continue
        kept.extend(line)
    if not kept:
        return ""
    return SubsectionExtractor.clean_text(_region_read(kept))


def salvage_from_page(icao, page_words):
    """Recover an AD 2.23 block that segment_page skipped because its physical
    page did not classify as AD_CONTENT. Two confirmed orphan classes:

      * CHART_INDEX orphan (DNMA p827): the whole 2.23 block prints above the
        AD 2.24 charts index on one page; the "CHARTS RELATED TO..." heading
        routes the page to CHART_INDEX.
      * CHART_PLATE orphan (DNKS p790): the 2.23 block prints at the bottom of a
        page otherwise full of AD 2.22 PBN procedure tables, and its own header
        is the bare "AD 2.23 ADDITIONAL INFORMATION" with NO "DNKS" prefix — so
        SUBSECTION_TITLE_RE (which requires the ICAO prefix) misses it and the
        page's self-reference code lands it in CHART_PLATE.

    Returns the 2.23 block's words (5-tuples) or [] if the page carries no
    additional-information header (the DNYO mis-labelled-index case, where
    page-1004's "AD 2.23" is actually the charts index — no ADDITIONAL block
    above the charts heading, so nothing is recovered). Band = from the 2.23
    header top down to the charts heading (if present and below it) else the
    page end, minus pure page chrome. The caller wraps the result in a synthetic
    Segment and runs the normal extract() path.
    """
    from segment_page import _line_groups, _is_pure_chrome_line

    lines = _line_groups(page_words)
    hdr_top = None
    charts_top = None
    for line in lines:
        text = " ".join(w[4] for w in line)
        if hdr_top is None and _H223_RE.search(text) and "ADDITIONAL" in text.upper():
            hdr_top = min(w[1] for w in line)
        if charts_top is None and _CHARTS_HEADING_RE.search(text):
            charts_top = min(w[1] for w in line)
    if hdr_top is None:
        return []
    lower = charts_top if (charts_top is not None and charts_top > hdr_top) else float("inf")

    band = []
    for line in lines:
        top = min(w[1] for w in line)
        if not (hdr_top - 2.0 <= top < lower - 2.0):
            continue
        if _is_pure_chrome_line(" ".join(w[4] for w in line)):
            continue
        band.extend(line)
    return band


class AD223Extractor(SubsectionExtractor):
    subsection = "2.23"
    kind = "text"

    def extract(self, icao: str, segments: list) -> ExtractResult:
        all_words = self.combine_segment_words(segments)
        warnings = []

        if not all_words:
            warnings.append(f"{icao}: no AD 2.23 words (segment absent — driver "
                             f"should attempt charts-index salvage)")
            return ExtractResult(
                icao=icao, subsection=self.subsection, kind=self.kind,
                records=[], text="", embed_text="", warnings=warnings,
            )

        text = _reconstruct(all_words)
        if not text.strip():
            warnings.append(f"{icao}: AD 2.23 segment produced no text")

        embed_text = (f"{icao} AD 2.23 additional information "
                      f"(bird/animal hazards): {text}") if text else f"{icao} AD 2.23"

        return ExtractResult(
            icao=icao, subsection=self.subsection, kind=self.kind,
            records=[], text=text, embed_text=embed_text, warnings=warnings,
        )

    def validate(self, result: ExtractResult) -> list:
        issues = []
        if not result.text.strip():
            issues.append(ValidationIssue(
                "error", "text", f"{result.icao}: AD 2.23 produced no text"))
        return issues
