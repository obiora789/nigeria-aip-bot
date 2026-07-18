"""
classify_page.py — Layer 1 of the structured-extraction architecture.

This is the SINGLE function both extraction and validation must call to decide
"what is this page?" — nothing else is allowed to decide page identity on its
own. The Abuja/AD-2.12 misrouting incident happened precisely because
extraction and validation used different notions of page identity; this module
exists so that failure mode is structurally impossible going forward.

BUILT ON aip_structure.py, NOT INDEPENDENT REGEX GUESSING. An earlier version
of this file re-derived page-to-aerodrome mapping from scratch via its own
header regexes — built from a separate survey pass than extract_charts.py's
hand-verified AERODROMES boundary table. On a real full-document test run, the
two disagreed about 48 pages (this file flagged 50 pages as "missing content"
that were, in fact, real chart plates extract_charts.py already handles
correctly). That is the SAME failure class as the AD 2.12 misattribution
incident — two pieces of code, each internally consistent, silently
disagreeing about the document's own structure. Fixed by making
aip_structure.AERODROMES (content-verified, not recomputed) the PRIMARY lookup
here; this file's own header regexes are now only a fallback for the pages
that table does not cover (front matter, AD 1.x intro, ENR, GEN).

DOCUMENT STRUCTURE (validated against Complete_AIP2026.pdf, all 1073 pages —
see aip_structure.py for the full boundary table and its provenance):
  - GEN 0.x / ENR 0.x / AD 0.x   front matter: preface, amendment records, and
    a TABLE OF CONTENTS that lists every aerodrome's every subsection as a
    dot-leader reference. This TOC uses the SAME header vocabulary as real
    content pages, so front-matter detection MUST run first, before any
    content check — checking content patterns first caused TOC pages to be
    misclassified as real aerodrome data during development.
  - AD 1.x                       general aerodrome/heliport introduction, not
    per-aerodrome data.
  - AD 2.x (36 standard aerodromes)   each aerodrome's own page range (from
    aip_structure.AERODROMES) contains THREE kinds of page, distinguished
    within the range:
      * AD_CONTENT   — real narrative/tabular admin data (AD 2.1-2.24 body
        text) — this is what segment_page() will later cut into subsections.
      * CHART_INDEX  — the AD 2.24 index page itself (chart name + reference
        number) — real text data, but not part of the 24-subsection grid.
      * CHART_PLATE  — an actual chart image page. Owned entirely by
        extract_charts.py; EXCLUDED from structured/text extraction here.
  - AD 2.x (DNXX*, AIRSTRIPS AND PRIVATE AERODROMES)   a pseudo-ICAO catch-all
    embedded PHYSICALLY inside another aerodrome's page range (confirmed
    inside DNZA's range, page 1024) but self-identifying with its OWN
    "AD 2-DNXX-N" header. Real data, wrong shape for the 36-aerodrome grid.
    Detected by self-reference, checked BEFORE trusting the enclosing
    boundary's ICAO.
  - AD 3.x (DNXX*, HELIPORTS/HELIPADS/HELIDECKS)   DNXX is REUSED for a second,
    distinct catalogue — a list of small heliports (confirmed inside DNWI's
    range, page 1068, "AD 3-DNXX-1 ... List of heliports, helipads and
    helidecks"). Same detection mechanism as the AD 2.x case, generalized to
    catch either part number.
  - AD 2.x / AD 3.x (4 heliports: DNGB, DNPS, DNSK, DNWI)   no 36-aerodrome
    admin block — a lone AD 2.x chart page/index, plus an AD 3.x procedures
    section (FATO/TLOF, RNP APCH waypoint tables — the heliport analogue of
    AD 2.22, similarly high-risk for misattribution if flattened into prose).
  - DNKK / GEN special chart sections   chart-only, no narrative admin data
    model at all — owned by extract_charts.py, excluded here.
  - ENR / GEN                    kept as text (per product decision) — not
    part of the structured-extraction manifest.
"""
import re
from collections import namedtuple

from aip_structure import (
    AERODROMES, STANDARD_36, HELIPORT_ICAO, SPECIMEN_ICAO,
    page_to_section, get_own_reference, get_own_part, is_index_page,
)

# ── fallback patterns, ONLY for pages outside every AERODROMES boundary ─────
FRONT_HDR = re.compile(r'\b(GEN|ENR|AD)\s*0\.\d{1,2}\s*-\s*\d+')
AD1_HDR   = re.compile(r'\bAD\s*1\.\d{1,2}\s*-\s*\d+')
ENR_HDR   = re.compile(r'\bENR\s*(\d)\.\d{1,2}\s*-\s*\d+')
GEN_HDR   = re.compile(r'\bGEN\s*(\d)\.\d{1,2}\s*-\s*\d+')

# Detects a real AD 2.NN subsection title within an aerodrome's own range —
# e.g. "DNAA AD 2.12 RUNWAY PHYSICAL CHARACTERISTICS". Tolerant of the same
# hyphen/space variance documented in aip_structure.py.
SUBSECTION_TITLE_RE = re.compile(
    r'(?:(DN[A-Z]{2})\s+)?AD\s+([23]\.\d{1,2})\s+([A-Z][A-Z0-9 /\-&\(\)]+)'
)
# A genuinely blank page — either the AIP's own "INTENTIONALLY BLANK" verso
# marker, or a page with no extractable text at all. Checked FIRST and applies
# regardless of what boundary the page falls in (a blank page inside DNAA's
# range is just as blank as one in the GEN front matter). Distinguishing this
# from UNKNOWN matters: ~44 of 78 originally-UNKNOWN pages in a full-document
# run turned out to be exactly this — diluting UNKNOWN so real gaps (like the
# DNXX/DNWI cases below) were harder to see against the noise.
BLANK_RE = re.compile(r'INTENTIONALLY\s+BLANK', re.IGNORECASE)

# DNXX self-identifies with its own header, physically embedded inside
# another aerodrome/heliport's boundary range — must be checked before
# trusting the enclosing range's ICAO. DNXX is reused as a generic pseudo-ICAO
# for TWO distinct small-facility catalogues: "AD 2-DNXX" (AIRSTRIPS AND
# PRIVATE AERODROMES, confirmed at page 1024, inside DNZA's range) and
# "AD 3-DNXX" (HELIPORTS, HELIPADS AND HELIDECKS, confirmed at page 1068,
# inside DNWI's range). Both are real, valuable catalogue data — matched
# together here since the enclosing-range check is identical for both.
SPECIMEN_HDR_RE = re.compile(r'\bAD\s*([23])\s*-?\s*DNXX\b', re.IGNORECASE)

PageID = namedtuple("PageID", ["page_index", "category", "part", "icao", "seq"])
# category one of:
#   BLANK | FRONT_MATTER | AD_INTRO | AD_CONTENT | CHART_INDEX | CHART_PLATE |
#   AD_SPECIMEN | AD3_HELIPORT_PROC | SPECIAL_CHART_SECTION |
#   ENR_CONTENT | GEN_CONTENT | UNKNOWN


def _text_of(words):
    """Flatten word-position tuples back to a plain string for regex checks
    that don't need positional info (is_index_page, get_own_reference, etc.)."""
    return " ".join(w[4] for w in words)


def _header_blob(words, top_band_pt=40.0):
    """For the fallback regexes only: join words in the page's top band."""
    if not words:
        return ""
    top_words = [w for w in words if w[1] <= top_band_pt]
    if not top_words:
        top_words = words[:40]
    top_words.sort(key=lambda w: (round(w[1] / 3), w[0]))
    return " ".join(w[4] for w in top_words)


def classify_page(page_index, words):
    """words: list of (x0, top, x1, bottom, text) from word-position extraction
    (matches extract_page_text_fixed._words() and the fitz get_text("words")
    format used in production). Returns a PageID.

    PRIMARY method: look up page_index against aip_structure.AERODROMES (the
    content-verified boundary table shared with extract_charts.py). Within a
    known range, classify by page TYPE (content / chart index / chart plate)
    using is_index_page() and get_own_reference()/get_own_part() — the exact
    same shared functions extract_charts.py's chart pipeline uses, so the two
    pipelines can never disagree about a page within a known boundary.

    FALLBACK: only for pages the boundary table does not cover (front matter,
    AD 1.x intro, ENR, GEN) — since aip_structure.AERODROMES intentionally
    does not include them (extract_charts.py never needed to; it remains the
    authority on aerodrome/heliport boundaries only).
    """
    text = _text_of(words)

    if not words or BLANK_RE.search(text):
        return PageID(page_index, "BLANK", None, None, None)

    m = SPECIMEN_HDR_RE.search(text)
    if m:
        part = f"AD{m.group(1)}"
        return PageID(page_index, "AD_SPECIMEN", part, "DNXX", None)

    section = page_to_section(page_index)

    if section is not None:
        icao, name, start, end = section

        if icao in STANDARD_36:
            m = SUBSECTION_TITLE_RE.search(text)
            if m and m.group(2) != "2.24" and (m.group(1) is None or m.group(1) == icao):
                return PageID(page_index, "AD_CONTENT", "AD2", icao, m.group(2))
            if is_index_page(text):
                return PageID(page_index, "CHART_INDEX", "AD2", icao, None)
            if m and (m.group(1) is None or m.group(1) == icao):
                return PageID(page_index, "AD_CONTENT", "AD2", icao, m.group(2))
            if get_own_reference(icao, text) is not None:
                return PageID(page_index, "CHART_PLATE", "AD2", icao, None)
            return PageID(page_index, "UNKNOWN", None, None, None)

        if icao in HELIPORT_ICAO:
            part = get_own_part(icao, text)
            if part == "3":
                return PageID(page_index, "AD3_HELIPORT_PROC", "AD3", icao, None)
            if part == "2":
                if is_index_page(text):
                    return PageID(page_index, "CHART_INDEX", "AD2", icao, None)
                return PageID(page_index, "CHART_PLATE", "AD2", icao, None)
            return PageID(page_index, "UNKNOWN", None, None, None)

        return PageID(page_index, "SPECIAL_CHART_SECTION", None, icao, None)

    blob = _header_blob(words)

    m = FRONT_HDR.search(blob)
    if m:
        return PageID(page_index, "FRONT_MATTER", m.group(1), None, None)

    m = AD1_HDR.search(blob)
    if m:
        return PageID(page_index, "AD_INTRO", "AD", None, None)

    m = ENR_HDR.search(blob)
    if m:
        return PageID(page_index, "ENR_CONTENT", "ENR", None, m.group(1))

    m = GEN_HDR.search(blob)
    if m:
        return PageID(page_index, "GEN_CONTENT", "GEN", None, m.group(1))

    return PageID(page_index, "UNKNOWN", None, None, None)
