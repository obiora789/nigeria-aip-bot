"""
ad222_extractor.py — AD 2.22 FLIGHT PROCEDURES.  FULL-CAPTURE design.

AD 2.22 is captured in FULL — every procedure, every approach/takeoff minimum,
every PBN waypoint and coding-table cell — with NOTHING excluded. The earlier
reference-pointer version deliberately withheld the dense tables; this version
does not. The safety requirement ("Vannie must never state a wrong operational
value") is met NOT by omission but by POSITION-FAITHFUL capture: each table is
reconstructed with its true row/column layout preserved, so every OCA/H stays
under its own column and beside its own runway and category. Capturing a value
in its correct 2-D position is the opposite of misattributing it.

WHY THIS EXTRACTOR TAKES PAGES, NOT SEGMENTS.
The 2.22 PBN coding-table pages (2.22.7.x) do not classify as AD_CONTENT — their
headers are the bare "2.22.7 ..." form with a page-reference code and no
"DNxx AD 2.NN" prefix, so classify_page routes them to CHART_PLATE and
segment_page skips them. They are, however, ordinary TEXT tables (confirmed:
hundreds of text words, near-zero vector objects — NOT raster charts), fully
loadable. So this extractor is driven by the RAW PAGE WORDS across the whole
2.22 page span (from the AD 2.22 span survey), not by the excluded segments.
segment_page.py / vectorise_aip_v2.py are untouched; ingestion drives this
extractor with span pages the same way the validator does.

HOW CAPTURE STAYS FAITHFUL (evidence-driven, confirmed on DNKS/DNAA/DNKN):
  1. BAND-SLICE per page — keep only the 2.22 region: from the "AD 2.22 FLIGHT
     PROCEDURES" header (on the start page) down to the "AD 2.23 ADDITIONAL" or
     "CHARTS RELATED TO" heading (on the end page); whole page on the middle
     pages. This excludes the neighbouring 2.21/2.23 content and the charts
     index without dropping any 2.22 content.
  2. COLUMN-SPLIT only on a CLEAN central corridor — if a vertical whitespace
     band in x∈[256,314] is crossed by ≤1 word, the page is the 2-column
     procedure layout (left prose, right minima); render the left region then
     the right region so prose and the minima grid never interleave. The
     ≤1-word rule is deliberately strict: a full-width PBN coding table (whose
     every data row spans the page) has NO such corridor, so it is NEVER split
     — splitting it would cut its rows and THAT would misattribute. Cleanly
     bimodal in practice: clean 2-column pages expose a corridor; table pages
     do not.
  3. LAYOUT-PRESERVING render — within each region, every word is placed at a
     character column derived from its x-coordinate, so the source's columns
     and blank cells are preserved (a blank OCA/H cell stays blank rather than
     letting the next column shift into it — the AD 2.13 lesson). This keeps
     "RWY 13 → OCA/H 2130(1480) → CAT A → VIS 1500" aligned exactly as printed.

Source letter-spacing artifacts ("inENR", "approach/landin g") are preserved
verbatim — re-gluing is a speculative transform, and this is a faithful capture.

VALIDATION asserts NO WORD LOSS: every non-chrome word in each page's 2.22 band
appears in the rendered text (multiset containment). That is the concrete proof
that nothing is excluded.
"""
import re

from extractor_base import SubsectionExtractor, ExtractResult, ValidationIssue
from segment_page import _line_groups, _is_pure_chrome_line

CHAR_W = 3.1
LINE_TOL = 3.0

_H22_RE = re.compile(r'AD\s+2\.22\s+FLIGHT', re.IGNORECASE)
_H23_RE = re.compile(r'AD\s+2\.23\s+ADDITIONAL', re.IGNORECASE)
_HC_RE = re.compile(r'CHARTS?\s+RELATED\s+TO\s+AN', re.IGNORECASE)
_HNUM_RE = re.compile(r'^2\.22(?:\.\d+){1,4}$')


def _line_top(line):
    return min(w[1] for w in line)


def band_2_22(words):
    """Keep only this page's AD 2.22 content: from the AD 2.22 header (if the
    page has it) down to the AD 2.23 header / CHARTS heading (if the page has
    one), minus pure page chrome. Middle pages (no header at all) keep
    everything; the whole page is 2.22 continuation."""
    lines = _line_groups(words)
    lo, hi = -1e9, 1e9
    for ln in lines:
        t = " ".join(w[4] for w in ln)
        if _H22_RE.search(t):
            lo = max(lo, _line_top(ln))
        if _H23_RE.search(t) or _HC_RE.search(t):
            hi = min(hi, _line_top(ln))
    kept = []
    for ln in lines:
        t = " ".join(w[4] for w in ln)
        if _is_pure_chrome_line(t):
            continue
        if lo - 2 <= _line_top(ln) < hi - 2:
            kept.extend(ln)
    return kept


def _detect_gutter(words):
    """Return a central gutter x if a clean vertical corridor (crossed by ≤1
    word) exists in x∈[256,314] with words on both sides; else None. Strict on
    purpose: full-width coding tables must never be split."""
    if not words:
        return None
    for g in range(256, 316, 2):
        cross = sum(1 for w in words if w[0] < g < w[2])
        if cross <= 1 and any(w[2] <= g for w in words) and any(w[0] >= g for w in words):
            return float(g)
    return None


def _render_region(words):
    """Layout-preserving render of one region: cluster into visual lines, place
    each word at a character column from its x so columns and blank cells are
    preserved."""
    if not words:
        return ""
    x0min = min(w[0] for w in words)
    lines = {}
    for w in words:
        lines.setdefault(round(w[1] / LINE_TOL), []).append(w)
    out = []
    for k in sorted(lines):
        ln = sorted(lines[k], key=lambda w: w[0])
        buf = ""
        for w in ln:
            col = int(round((w[0] - x0min) / CHAR_W))
            if not buf:
                buf = " " * col + w[4]
            elif col <= len(buf):
                buf += " " + w[4]           # keep >=1 space: never merge words
            else:
                buf += " " * (col - len(buf)) + w[4]
        if buf.strip():
            out.append(buf.rstrip())
    return "\n".join(out)


def _render_page(words):
    """Render a page's 2.22 band, splitting into left/right regions only when a
    clean central corridor exists (2-column procedure layout)."""
    g = _detect_gutter(words)
    if g is not None:
        left = [w for w in words if (w[0] + w[2]) / 2 < g]
        right = [w for w in words if (w[0] + w[2]) / 2 >= g]
        parts = [p for p in (_render_region(left), _render_region(right)) if p]
        return "\n".join(parts)
    return _render_region(words)


def _norm_tokens(text):
    """Whitespace-split tokens for the word-loss check (order-independent)."""
    return [t for t in re.split(r'\s+', text) if t]


def _headings(words):
    seen, order = {}, []
    for w in sorted(words, key=lambda w: (w[1], w[0])):
        if _HNUM_RE.match(w[4]) and w[0] < 0.5 * 595.2:
            if w[4] not in seen:
                seen[w[4]] = True
                order.append(w[4])
    return sorted(set(order), key=lambda n: tuple(int(p) for p in n.split(".")))


class AD222Extractor(SubsectionExtractor):
    subsection = "2.22"
    kind = "text"

    def __init__(self, page_span=None):
        # page_span: {icao: (start_page, end_page)} for the 2.22 span (from the
        # survey). Required — this extractor reads pages, not segments.
        self.page_span = page_span or {}

    def extract(self, icao: str, page_words_by_index: dict) -> ExtractResult:
        """page_words_by_index: {page_index: word-tuples} for EVERY page in the
        aerodrome's 2.22 span (AD_CONTENT and the CHART_PLATE PBN pages alike).
        """
        warnings = []
        span = self.page_span.get(icao)
        pages = sorted(page_words_by_index)
        if span:
            pages = [p for p in pages if span[0] <= p <= span[1]]

        rendered_pages = []
        all_band_words = []
        heading_nums = []
        for p in pages:
            band = band_2_22(page_words_by_index[p])
            if not band:
                continue
            all_band_words.extend(band)
            heading_nums.extend(_headings(band))
            rendered_pages.append(_render_page(band))

        if not all_band_words:
            warnings.append(f"{icao}: no AD 2.22 content found in span {span}")
            return ExtractResult(
                icao=icao, subsection=self.subsection, kind=self.kind,
                records=[], text="", embed_text="", warnings=warnings,
            )

        span_txt = f"AIP pages {span[0]}\u2013{span[1]}" if span else "the source AIP"
        header = f"AD 2.22 FLIGHT PROCEDURES \u2014 {icao} (source: {span_txt})"
        # Pages joined continuously (no inline page markers) so a procedure body
        # that spans a page break stays intact for the downstream sectioniser
        # (procedures.py) — an inline marker was being turned into a bullet and
        # leaking into the Holding section. Page traceability stays in `header`.
        text = header + "\n\n" + "\n".join(rendered_pages)

        heads = sorted(set(heading_nums), key=lambda n: tuple(int(x) for x in n.split(".")))
        has_pbn = any(h.startswith("2.22.7") for h in heads)
        embed_text = (
            f"{icao} AD 2.22 flight procedures instrument approach holding "
            f"letdown missed approach circling approach minima takeoff minima "
            f"OCA/H OCH visibility RVR "
            + ("PBN RNAV RNP GNSS LNAV VNAV waypoint coordinates coding SID STAR "
               if has_pbn else "")
            + " ".join(heads)
        )

        return ExtractResult(
            icao=icao, subsection=self.subsection, kind=self.kind,
            records=[], text=text, embed_text=embed_text, warnings=warnings,
        )

    def validate(self, result: ExtractResult, page_words_by_index=None) -> list:
        """Assert non-empty AND (if the source pages are supplied) NO WORD LOSS:
        every non-chrome word in each page's 2.22 band must appear in the
        rendered text — the concrete proof that nothing was excluded."""
        issues = []
        if not result.text.strip():
            issues.append(ValidationIssue(
                "error", "text", f"{result.icao}: AD 2.22 produced no text"))
            return issues

        if page_words_by_index is not None:
            span = self.page_span.get(result.icao)
            from collections import Counter
            src = Counter()
            for p in sorted(page_words_by_index):
                if span and not (span[0] <= p <= span[1]):
                    continue
                for w in band_2_22(page_words_by_index[p]):
                    src[w[4]] += 1
            out = Counter(_norm_tokens(result.text))
            missing = []
            for tok, n in src.items():
                if out[tok] < n:
                    missing.append((tok, n, out[tok]))
            if missing:
                issues.append(ValidationIssue(
                    "error", "text",
                    f"{result.icao}: {len(missing)} source tokens lost in render, "
                    f"e.g. {missing[:6]}"))
        return issues
