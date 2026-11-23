"""
extract_page_text_fixed.py — drop-in replacement for extract_page_text() in
vectorise_aip_v2.py.

WHY THIS VERSION EXISTS
-----------------------
The earlier "table vs prose" discriminator (_TABLE_MAX_CELL_H) could not fix the
AD 2.2/2.3/2.4 administrative pages, for two reasons proven on Kano p.737:

  1. fitz groups each key-value ROW into ONE block ("3\nElevation...\n476 m..."),
     so the value never becomes a separate right-column cell. The left/right
     bucketing then dropped every row into `left`, `right` stayed empty, the
     tabular test short-circuited, and the block's internal newlines were dumped
     verbatim — label and value on separate lines. Tuning cell height never
     reaches that path.

  2. Worse: the section headers (DNKN AD 2.2/2.3/2.4) sit RIGHT of centre, so the
     old "all left, then all right" order emitted every field row FIRST and every
     header LAST. ad_subsection_chunks() splits on those headers, so with the
     headers relocated to the end the field values (elevation, ARP, hours) landed
     BEFORE the first header and were dropped from every subsection chunk.

THE FIX
-------
Administrative key-value pages are detected (a far-left column of bare row
integers) and read with a dedicated pass that:
  • keeps each "DNKN AD 2.N" header immediately ABOVE its own field rows, and
  • pairs each field's label (left of the column gutter) with its value (right of
    it) on a single "label: value" line, across multi-line rows.

Everything else (GEN/ENR prose, two-column procedure text, wide tables) falls
through to the previous column reader UNCHANGED, so nothing else regresses.

NOTE ON AD 2.22: two-column procedure pages carry embedded minima tables that
this reader (and the old one) still interleave. Do NOT rely on text extraction
for AD 2.22 — exclude it at ingestion until a per-approach extractor exists.

Validated on Complete_AIP2026.pdf page 737 (DNKN AD 2.1–2.4): every field pairs
with its value on one line, and ad_subsection_chunks() places "476 m" in [2.2].
Re-validate with verify_extract.py on a few more aerodromes before re-ingesting.
"""
import sys as _sys, pathlib as _pl  # repo layout: bot at the top, scripts in folders
_R = next(d for d in _pl.Path(__file__).resolve().parents if (d / "main.py").exists())
_sys.path[1:1] = [str(p) for p in (_R, _R / "checks", _R / "ingestion", _R / "ingestion" / "validate")
                  if str(p) not in _sys.path]
import re

_HDR_RE = re.compile(r'\bAD\s+[23]\.\d{1,2}\b')
_INT_RE = re.compile(r'\d{1,2}')
_CHROME_RE = re.compile(
    r'^(NIGERIA AIP|.*AIRSPACE MANAGEMENT.*|AD 2-DN[A-Z]{2}-\d+|'
    r'\d{1,2} [A-Z]{3} \d{2}|AIRAC AMDT \d\d/\d{4})$'
)

_ROWNUM_MAX_XFRAC = 0.16   # bare row integers live in the far-left 16% of the page
_MIN_ROWNUMS      = 4      # a key-value page has at least this many far-left ints
_YTOL             = 3.0    # words within this vertical gap are the same visual line
_READ_BAND        = 8.0    # reading-order band (~half a line): keeps off-baseline
                           # superscripts/subscripts (e.g. a raised °C) on their own
                           # line so they order by x instead of floating to the front


def _words(page):
    """fitz words -> (x0, top, x1, bottom, text), reading-safe order."""
    ws = page.get_text("words")   # (x0, y0, x1, y1, "word", block, line, wordno)
    return [(w[0], w[1], w[2], w[3], w[4]) for w in ws if w[4].strip()]


def _is_chrome(text):
    return bool(_CHROME_RE.match(text.strip()))


def _rownum_count(words, width):
    lim = _ROWNUM_MAX_XFRAC * width
    return sum(1 for x0, _, x1, _, t in words
               if _INT_RE.fullmatch(t) and x1 < lim)


def looks_key_value(words, width):
    """True for AD 2.2/2.3/2.4-style admin pages: a far-left column of bare row
    integers. Procedure pages (2.22) use dotted numbering (2.22.3.1.1) inline, not
    a far-left single-int column, so they score 0 here and keep the prose path."""
    return _rownum_count(words, width) >= _MIN_ROWNUMS


def _detect_gutter(cell_words, width):
    """Column gutter = centre of the widest horizontal gap between word x-intervals
    within the central band. Adapts per aerodrome; falls back to page mid."""
    lo, hi = 0.30 * width, 0.66 * width
    xs = sorted((w[0], w[2]) for w in cell_words)
    best_gap, gutter = 0.0, width / 2.0
    right_edge = None
    for x0, x1 in xs:
        if right_edge is not None and lo < (x0 + right_edge) / 2 < hi:
            gap = x0 - right_edge
            if gap > best_gap:
                best_gap, gutter = gap, (x0 + right_edge) / 2
        right_edge = max(right_edge, x1) if right_edge is not None else x1
    return gutter


def _emit_region(cell, width):
    """cell: list of (x0, top, x1, bottom, text) belonging to one AD 2.N region
    (its rows, header already emitted). Returns 'label: value' lines."""
    anchors = sorted(
        (w for w in cell if _INT_RE.fullmatch(w[4]) and w[2] < _ROWNUM_MAX_XFRAC * width),
        key=lambda w: w[1],
    )
    if not anchors:                       # e.g. AD 2.1: just a name, no numbered rows
        seq = sorted(cell, key=lambda w: (round(w[1] / _READ_BAND), w[0]))
        line = " ".join(w[4] for w in seq).strip()
        return [line] if line else []

    gutter = _detect_gutter(cell, width)
    tops = [a[1] for a in anchors] + [float("inf")]
    out = []
    for i, a in enumerate(anchors):
        lo, hi = a[1] - 2.0, tops[i + 1] - 2.0
        body = [w for w in cell if lo <= w[1] < hi and w is not a]
        label = " ".join(w[4] for w in sorted(
            (w for w in body if (w[0] + w[2]) / 2 < gutter),
            key=lambda w: (round(w[1] / _READ_BAND), w[0])))
        value = " ".join(w[4] for w in sorted(
            (w for w in body if (w[0] + w[2]) / 2 >= gutter),
            key=lambda w: (round(w[1] / _READ_BAND), w[0])))
        row = f"{label}: {value}".strip(": ").strip()
        if row:
            out.append(f"{label}: {value}".strip())
    return out


def _extract_key_value(words, width):
    """Admin key-value page: headers kept above their own fields; each field on
    one 'label: value' line."""
    # group into visual lines by top-y
    lines = {}
    for w in words:
        if w[1] < 48 or w[1] > 790:       # strip top/bottom page chrome band
            continue
        lines.setdefault(round(w[1] / _YTOL), []).append(w)

    ordered = sorted(lines.items())
    out, buf = [], []

    def flush():
        if buf:
            out.extend(_emit_region(buf, width))
            buf.clear()

    for _, lw in ordered:
        text = " ".join(t[4] for t in sorted(lw, key=lambda w: w[0]))
        if _is_chrome(text):
            continue
        if _HDR_RE.search(text):          # section header -> boundary
            flush()
            out.append(text.strip())
        else:
            buf.extend(lw)
    flush()
    return "\n".join(out)


def _extract_columns(page):
    """Previous behaviour, unchanged: full-width blocks in place; narrow blocks
    bucketed left/right and emitted left-column-then-right. Used for everything
    that is NOT an admin key-value page (GEN/ENR prose, procedure text, etc.)."""
    try:
        blocks = [b for b in page.get_text("blocks")
                  if len(b) >= 7 and b[6] == 0 and b[4].strip()]
    except Exception:  # noqa: BLE001
        return page.get_text("text")
    if not blocks:
        return page.get_text("text")

    width = page.rect.width
    mid = width / 2.0
    blocks.sort(key=lambda b: (round(b[1], 1), b[0]))
    out, left, right = [], [], []

    def flush():
        for bb in sorted(left, key=lambda b: (b[1], b[0])):
            out.append(bb[4].strip())
        for bb in sorted(right, key=lambda b: (b[1], b[0])):
            out.append(bb[4].strip())
        left.clear()
        right.clear()

    for b in blocks:
        if (b[2] - b[0]) > 0.60 * width:
            flush()
            out.append(b[4].strip())
        elif (b[0] + b[2]) / 2.0 < mid:
            left.append(b)
        else:
            right.append(b)
    flush()
    return "\n".join(out)


def extract_page_text(page) -> str:
    """Router: admin key-value pages (AD 2.2/2.3/2.4-style) use the key-value pass
    so each field's header+value stay together and each subsection chunk keeps its
    values; every other page uses the previous column reader."""
    try:
        words = _words(page)
        width = page.rect.width
    except Exception:  # noqa: BLE001
        return _extract_columns(page)

    if words and looks_key_value(words, width):
        return _extract_key_value(words, width)
    return _extract_columns(page)
