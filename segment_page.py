"""
segment_page.py — Layer 1 (second half): cut an aerodrome's pages into
per-subsection text slices.

WHY THIS IS A STATEFUL WALK, NOT A PER-PAGE FUNCTION.
The original design sketch called this "segment_page(page)". Real evidence from
the document shows that is the wrong shape. Within a single aerodrome's page
range, subsections routinely CONTINUE across page boundaries (confirmed on DNKN,
pages 737-780):
    p740  starts "DNKN AD 2.11 ..."          (header present)
    p741  starts "Slope of RWY and SWY ..."  (NO header — continues p740's AD 2.13)
    p745  starts "DNKN AD 2.22 FLIGHT PROC"  (header present)
    p746-758  NO header — all continue AD 2.22 across 13 pages
So a page with no subsection header at its top is not "unknown" — it is a
continuation of whichever subsection was still open at the end of the previous
page. Segmenting one page in isolation cannot know that. This module therefore
walks an aerodrome's pages in order, carrying the currently-open subsection
across page breaks.

RELATIONSHIP TO classify_page / aip_structure.
- classify_page decides a page's CATEGORY (AD_CONTENT vs CHART_PLATE vs ...).
- segment_page only ever processes AD_CONTENT pages; it does NOT re-derive page
  identity — it consults the same aip_structure boundary table so it can never
  disagree with the classifier about where an aerodrome's range begins/ends.
- The subsection-title regex here is the SAME one classify_page uses
  (imported), so "what counts as a subsection header" is defined in exactly one
  place.

EXCLUSIONS carried through from earlier decisions.
- AD 2.22 (flight procedures) segments are still produced here (so the walk
  stays complete and every page is accounted for), but are TAGGED excluded=True.
  Downstream ingestion drops them — same rule already in vectorise_aip_v2.py
  (the `if subsec == "2.22": continue` skip). Segmenting them but marking them
  keeps the audit honest ("we saw these pages, we chose to exclude them")
  rather than silently dropping pages.
"""
import re
from collections import namedtuple

from aip_structure import page_to_section, STANDARD_36, HELIPORT_ICAO

# Same subsection-header definition classify_page uses. A header line looks like
# "DNKN AD 2.12 RUNWAY PHYSICAL CHARACTERISTICS" — but the ICAO prefix is NOT
# always present: confirmed directly on DNAA's own first page, where a
# Part-level title ("AD 2. AERODROMES") sits immediately before the very first
# subsection header, displacing the usual "DNAA AD 2.1 ..." repetition — the
# real text reads "AD 2.1 AERODROME LOCATION INDICATOR AND NAME" with no
# "DNAA" immediately before it. The ICAO group is therefore OPTIONAL here; a
# missing prefix falls back to the aerodrome ICAO segment_aerodrome() already
# knows it's processing (see _split_page_into_subsections's known_icao param).
# This is safe specifically because segmentation only ever runs within one
# aerodrome's own confirmed AD_CONTENT range — unlike a document-wide search,
# there's no risk of picking up an unrelated aerodrome's bare "AD 2.N" mention.
SUBSECTION_HDR_RE = re.compile(
    r'(?:(DN[A-Z]{2})\s+)?AD\s+([23]\.\d{1,2})\b\s*([A-Z][A-Z0-9 /\-&\(\)]*)?'
)

# Page chrome — running headers/footers that must NEVER be treated as content
# or bleed into a field's value. A REAL bug, found on AD 2.4 (not unique to
# it — this is a Layer 1 gap that could silently corrupt the LAST field of
# ANY subsection sitting against a page boundary): confirmed directly on
# DNAA, whose real, clean "7 Remarks NIL" was immediately followed on the
# same page by the unfiltered footer "NIGERIAN AIRSPACE MANAGEMENT AGENCY
# AIRAC AMDT 03/2026" — with nothing to stop it, that text got appended
# straight into the Remarks value. And on DNBB's continuation page, the
# page's own header chrome ("AD 2-DNBB-2", date-stamp "6 DEC 18", "NIGERIA
# AIP") had no closing subsection header ahead of it to signal "this isn't
# AD 2.4 continuing" — it got glued onto whatever segment was still open.
#
# Each pattern below is SUBSTRING-matched and stripped, not a rigid
# whole-line match — required because verso/recto page mirroring can put
# MULTIPLE chrome elements on the SAME visual line (confirmed: DNBB's page 2
# header has "6 DEC 18" at x0~28 and "NIGERIA AIP" at x0~511, BOTH at the
# same top=37.4, joining into one line "6 DEC 18 NIGERIA AIP" that a
# whole-line match against either fragment alone would miss). The page-code
# pattern reuses the same spacing tolerance already proven necessary in
# aip_structure.py (AD 2-DNAA-1 / AD2-DNMM-49 / AD 2- DNAI - 11 / AD2-DNMN 15
# — four confirmed real variants); a rigid single-spacing chrome pattern
# would miss three of them.
_MONTH_RE = r'(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|0CT|NOV|DEC)'
# "0CT" (zero, not letter O) confirmed directly on DNZA's page — a font
# glyph-substitution quirk, not a typo in the source content. Enumerated
# explicitly rather than widened to a loose [A-Z0]{3} character class, to
# avoid accidentally matching unrelated three-character tokens with a zero.
_CHROME_FRAGMENT_RES = [
    re.compile(r'\bAD\s*[23]\s*-?\s*DN[A-Z]{2}\s*-?\s*\d+\b'),   # "AD 2-DNBB-2"
    re.compile(rf'\b\d{{1,2}}\s+{_MONTH_RE}\s+\d{{2}}\b'),        # "6 DEC 18" / "30 0CT 25"
    re.compile(rf'\b\d{{1,2}}\s+{_MONTH_RE}\s+\d\s+\d\b'),         # "16 APR 2 6" (2-digit year
                                                                     # ITSELF split with a space —
                                                                     # confirmed on DNMM, distinct
                                                                     # from the 4-digit AMDT-year
                                                                     # split already handled below)
    re.compile(rf'\b\d{{1,2}}\s+{_MONTH_RE}\d{{2}}\b'),            # "6 DEC18" (month+year fused,
                                                                     # day still separate)
    re.compile(rf'\b{_MONTH_RE}\d{{2}}\b'),                        # "DEC18" alone (fallback, in
                                                                     # case the day is stripped by
                                                                     # a different pattern first)
    # confirmed directly on DNBK's page — a word-extraction quirk fusing the
    # month abbreviation and 2-digit year into one token with no separating
    # space, elsewhere the day-number and month/year print as expected
    # 3-token "D MMM YY" chrome (the pattern above). Distinct from the 0CT
    # glyph-substitution case: this is a spacing artifact, not a font one.
    re.compile(r'\bNIGERIA\s+AIP\b', re.I),
    re.compile(r'\bNIGERIAN?\s+AIRSPACE\s+MANAGEMENT\s+AGENCY\b', re.I),
    re.compile(r'\b(?:AIRAC|AIP)\s+AMDT\s+\d{1,3}(?:\s*/{1,2}\s*\d{2,4}(?:\s+\d{1,2})?)?\b', re.I),
    # Two confirmed amendment-stamp conventions, not one: "AIRAC AMDT NN/NNNN"
    # (current) and the OLDER "AIP AMDT ..." convention, found directly on
    # DNFB ("AIP AMDT 01/2019") and DNFD ("AIP AMDT 016" — note: no slash-year
    # at all, just a bare 3-digit amendment number). The trailing
    # "(?:\s*/{1,2}\s*\d{2,4}(?:\s+\d{1,2})?)?" is therefore OPTIONAL, covering
    # both the slash-year form and the bare-number form in one pattern. Also
    # still absorbs DNMM's confirmed split-year case ("03/202" + " " + "6"),
    # and DNPO's confirmed doubled-slash rendering quirk ("03//2026").
]


def _is_pure_chrome_line(line_text):
    """True if EVERY token on this line is part of recognized page chrome —
    i.e. stripping all known chrome fragments leaves nothing behind. A line
    with even partial genuine content is NOT treated as chrome (safer to
    risk under-filtering than to silently eat real data)."""
    remainder = line_text
    for pat in _CHROME_FRAGMENT_RES:
        remainder = pat.sub('', remainder)
    return not remainder.strip()

# Subsections excluded from structured/text ingestion (procedures — scrambled
# interleaved minima tables, high misattribution risk; chart index handled by
# extract_charts.py). Kept in sync with vectorise_aip_v2.py's ingest skip.
EXCLUDED_SUBSECTIONS = {"2.22"}

# The header band: y-position (points from page top) below the page-number/date
# running header but where the "DNxx AD 2.NN TITLE" line sits. Verified on real
# pages: page code at y~27, date/"NIGERIA AIP" at y~36, subsection header at
# y~60. We scan a generous band to tolerate layout variance.
_HDR_BAND_TOP = 50.0
_HDR_BAND_BOTTOM = 78.0
_YTOL = 3.0

Segment = namedtuple("Segment", ["icao", "subsection", "title", "page_index",
                                  "words", "is_continuation", "excluded"])


def _line_groups(words):
    """Group word tuples (x0, top, x1, bottom, text) into visual lines by top-y."""
    lines = {}
    for w in words:
        lines.setdefault(round(w[1] / _YTOL), []).append(w)
    return [sorted(lines[k], key=lambda w: w[0]) for k in sorted(lines)]


def _header_at_top(words):
    """If this page opens with a subsection header in the header band, return
    (icao, subsection, title); else None (meaning the page continues whatever
    subsection was open on the previous page)."""
    band = [w for w in words if _HDR_BAND_TOP <= w[1] <= _HDR_BAND_BOTTOM]
    if not band:
        return None
    band.sort(key=lambda w: (round(w[1] / _YTOL), w[0]))
    text = " ".join(w[4] for w in band)
    m = SUBSECTION_HDR_RE.search(text)
    if not m:
        return None
    return (m.group(1), m.group(2), (m.group(3) or "").strip())


def _split_page_into_subsections(page_index, words, open_state, known_icao=None):
    """Split ONE AD_CONTENT page into subsection segments.

    open_state: (icao, subsection, title) currently open from a previous page,
    or None at the start of an aerodrome.
    known_icao: the aerodrome this page belongs to (from segment_aerodrome's
    caller, which already knows it from aip_structure.AERODROMES). Used as a
    fallback when a header line lacks its own ICAO prefix — confirmed this
    happens on DNAA's very first subsection header (see SUBSECTION_HDR_RE's
    docstring above).

    Returns (segments_for_this_page, new_open_state). Every word on the page is
    assigned to exactly one segment, so no content is silently dropped.
    """
    lines = _line_groups(words)
    segments = []

    # Walk the page's lines top-to-bottom. Each time a subsection header line
    # appears, close the current segment and open a new one. Content before the
    # first header on the page belongs to the open_state subsection (a
    # continuation from the previous page).
    cur_icao, cur_sub, cur_title = (open_state if open_state else (None, None, None))
    cur_words = []
    started_as_continuation = open_state is not None

    def flush(is_cont):
        if cur_sub is not None and cur_words:
            segments.append(Segment(
                icao=cur_icao, subsection=cur_sub, title=cur_title,
                page_index=page_index, words=list(cur_words),
                is_continuation=is_cont,
                excluded=(cur_sub in EXCLUDED_SUBSECTIONS),
            ))

    first_segment_on_page = True
    for line in lines:
        line_text = " ".join(w[4] for w in line)
        if _is_pure_chrome_line(line_text):
            continue  # never treat page chrome as a header OR as content
        m = SUBSECTION_HDR_RE.search(line_text)
        # Only treat as a header if it's a genuine "DNxx AD 2.NN" at a line start
        # region — SUBSECTION_HDR_RE already requires that shape.
        if m:
            # close whatever was open, then open the new subsection
            flush(is_cont=(first_segment_on_page and started_as_continuation))
            first_segment_on_page = False
            cur_icao = m.group(1) or known_icao
            cur_sub, cur_title = m.group(2), (m.group(3) or "").strip()
            cur_words = list(line)
        else:
            cur_words.extend(line)

    # flush the trailing open segment (this is what may continue to next page)
    flush(is_cont=(first_segment_on_page and started_as_continuation))

    new_open_state = (cur_icao, cur_sub, cur_title) if cur_sub is not None else None
    return segments, new_open_state


def segment_aerodrome(icao, page_words_by_index, classify_fn):
    """Walk one aerodrome's pages in order, producing per-subsection segments
    with cross-page continuation carried correctly.

    page_words_by_index: dict {page_index: words} for every page in the
        aerodrome's range (caller supplies word-position extraction).
    classify_fn: classify_page (injected so this module never re-derives page
        identity independently — single source of truth).

    Only AD_CONTENT pages are segmented. CHART_PLATE / CHART_INDEX /
    AD_SPECIMEN / etc. are skipped here (owned elsewhere). When a chart or other
    non-content page interrupts the range, any open subsection is CLOSED (a
    subsection never continues across a non-content page).
    """
    section = None
    for entry in _aerodrome_entries():
        if entry[0] == icao:
            section = entry
            break
    if section is None:
        raise ValueError(f"{icao} is not a known aerodrome in aip_structure")

    _, _, start, end = section
    all_segments = []
    open_state = None

    for p in range(start, end + 1):
        if p not in page_words_by_index:
            open_state = None  # missing page breaks any continuation
            continue
        words = page_words_by_index[p]
        pid = classify_fn(p, words)

        if pid.category != "AD_CONTENT":
            # a chart/index/specimen page interrupts — close any open subsection
            open_state = None
            continue

        segs, open_state = _split_page_into_subsections(p, words, open_state, known_icao=icao)
        all_segments.extend(segs)

    return all_segments


def _aerodrome_entries():
    from aip_structure import AERODROMES
    return AERODROMES
