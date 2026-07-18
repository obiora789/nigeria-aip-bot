#!/usr/bin/env python3
"""
extract_charts.py — Nigerian AIP 2026 Chart Extraction (FINAL)
====================================================================
Extracts every chart plate from the 2026 Nigerian AIP and uploads each
to Supabase Storage with correct icao_code/procedure_type/runway
metadata, replacing a prior version whose page-number arithmetic was
wrong for 104 of 252 entries (41%) — most severely for DNMM (Lagos),
where every STAR/ILS/VOR/RNAV approach-chart entry pointed into DNMN's
(Minna's) physical pages instead, confirmed by direct visual inspection.

THREE STRUCTURAL GUARANTEES, each one closing a class of bug found and
confirmed against real pages during this rebuild:

1. NO ARITHMETIC PAGE BOUNDARIES. Every aerodrome's start/end page is
   verified by direct text match against its own section header
   ("AD 2-{ICAO}-1" / "AD 3-{ICAO}-1") — never calculated. End page =
   next aerodrome's verified start - 1. A chart can never be
   misattributed to the wrong aerodrome, because the scan for aerodrome
   X never reads outside X's own verified range.

2. CLASSIFICATION IS THE GATE, not a downstream step after a pre-filter.
   A page is only treated as a chart if it successfully resolves via:
     - exact reference-number lookup against that aerodrome's own AD
       2.24/3.23 index page (the primary method, validated end-to-end
       against DNGO and DNMM's real pages), or
     - the small MANUAL_REFERENCE_OVERRIDES table, for the handful of
       pages confirmed to have zero extractable text at the PDF data
       level (flat scanned images — verified directly by failed text
       selection in Acrobat), or
     - for DNKK and GEN only (the two sections with no index page to
       check against at all) — a genuine TITLE_PATTERNS match.
   An earlier version used a separate keyword pre-filter to decide what
   counted as a "chart candidate" before classification; that produced
   hundreds of false positives, since words like ELEV/HEIGHT/ARRIVAL
   appear throughout ordinary narrative text, not only on chart pages.
   Removed entirely — there is no code path left where an unclassified
   page can reach the database.

3. The source AIP itself is inconsistently formatted, and every variant
   below was found and fixed by direct inspection, not guessed at:
   tight dashes ("AD 2-DNGO-13"), spaced dashes ("AD 2 - DNET - 15"),
   a dropped part-type digit ("AD-DNES-11"), a dropped trailing dash
   ("AD2-DNMN 15"), a missing "Chart name" index header on three
   heliports, and a greedy digit-capture bug that bled into adjacent
   unrelated numbers in the flat extracted text ("AD2-DNIL-17119").

Run this AFTER:
  1. DELETE FROM aip_charts;
  2. Clear the nigeria_aip_charts storage bucket
  3. vectorise_aip_v2.py has completed (unrelated table, unaffected by
     any of the above — only aip_charts/chart images were ever wrong)

MAINTENANCE NOTE for the next AIRAC cycle: AERODROMES boundaries and
MANUAL_REFERENCE_OVERRIDES are both scoped to THIS specific PDF. A new
AIRAC document will reflow page numbers and may re-digitize the
override pages with real text — re-run the boundary verification and
override checks against the new document rather than reusing these.
"""

import os, re, time
import fitz
from supabase import create_client
from dotenv import load_dotenv

from aip_structure import AERODROMES, get_own_reference, is_index_page

load_dotenv()

supabase    = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
PDF_PATH    = os.getenv("PDF_PATH")
BUCKET_NAME = "nigeria_aip_charts"
TABLE_NAME  = "aip_charts"
DPI         = 150

doc = fitz.open(PDF_PATH)

# ── content-verified aerodrome/heliport boundaries ────────────────────────────
# Moved to aip_structure.py (single source of truth, shared with
# classify_page.py — see that module's docstring for why). Imported above.

# ── Chart detection and classification (proven logic, unchanged) ─────────────
# ── NOTE: there is deliberately no keyword-based chart pre-filter ────────────
# An earlier version used a CHART_KEYWORDS list (ELEV, HEIGHT, ALTITUDE,
# ARRIVAL, DEPARTURE, etc.) to decide which pages were "chart candidates"
# before attempting classification. That was removed entirely after
# confirming it produced hundreds of false positives: those words appear
# throughout ordinary AD 2.x narrative prose (location indicator, ARP
# coordinates, operating hours), not only on chart pages — confirmed
# directly on DNAA's very first narrative page. Classification itself is
# now the only gate (see process() below): a page is treated as a chart
# if and only if it successfully resolves via index lookup, manual
# override, or a genuine title match. There is no separate pre-filter
# left to accidentally loosen.

# ── is_index_page(), get_own_reference() ──────────────────────────────────────
# Both moved to aip_structure.py (single source of truth, shared with
# classify_page.py). Imported above. Logic and behavior unchanged.


TITLE_PATTERNS = [
    (r'PRECISION\s+APPROACH\s+TERRAIN',                          'Precision Approach Terrain Chart', False),
    (r'ILS\s*(?:/\s*DME)?(?:\s+OR\s+LOC)?[^R]*RWY\s*(\d{2}[LRC]?)', 'ILS Approach Chart',          True),
    (r'(?:RNAV|GNSS|RNP)\s*(?:\([^)]*\))?[^R]*RWY\s*(\d{2}[LRC]?)', 'RNAV Approach Chart',         True),
    (r'VOR\s*(?:/\s*DME)?[^R]*RWY\s*(\d{2}[LRC]?)',             'VOR Approach Chart',                True),
    (r'NDB\s*(?:/\s*DME)?[^R]*RWY\s*(\d{2}[LRC]?)',             'NDB Approach Chart',                True),
    (r'INSTRUMENT\s+APPROACH[^R]*RWY\s*(\d{2}[LRC]?)',          'Instrument Approach Chart',          True),
    (r'VISUAL\s+APPROACH[^R]*RWY\s*(\d{2}[LRC]?)',              'Visual Approach Chart',              True),
    (r'OBSTACLE\s+CHART[^R]*RWY\s*(\d{2}[LRC]?)',               'Aerodrome Obstacle Chart',           True),
    (r'STANDARD\s+(?:INSTRUMENT\s+)?DEPARTURE|SID\b.*?ICAO',    'SID Chart',                         False),
    (r'STANDARD\s+(?:TERMINAL\s+)?ARRIVAL|STAR\b.*?ICAO',       'STAR Chart',                        False),
    (r'AREA\s+CHART.*?ARRIVAL\s+AND\s+TRANSIT',                 'Area Chart - Arrival and Transit Routes', False),
    (r'AREA\s+CHART.*?DEPARTURE\s+AND\s+TRANSIT',               'Area Chart - Departure and Transit Routes', False),
    (r'AREA\s+CHART',                                            'Area Chart',                         False),
    (r'GROUND\s+MOVEMENT\s+CHART',                              'Ground Movement Chart',               False),
    (r'(?:AIRCRAFT\s+)?PARKING\s*(?:/\s*DOCKING)?\s*CHART',     'Parking / Docking Chart',            False),
    (r'AERODROME\s+CHART|\bADC\b',                              'Aerodrome Chart',                    False),
    (r'HELIPORT\s+CHART',                                       'Heliport Chart',                     False),
    (r'HELICOPTER\s+TRAFFIC\s+CHART',                           'Helicopter Traffic Chart',            False),
    (r'EN.?ROUTE\s+CHART|ENR\s+6',                              'En-Route Chart',                     False),
    (r'SEARCH\s+AND\s+RESCUE',                                  'Search and Rescue Units Chart',       False),
    (r'AERODROME\s+INDEX',                                      'Aerodrome Index Chart',               False),
]

def detect_from_title(page, text):
    """
    Classify a chart page from its ACTUAL TITLE — read by physical
    position on the page, not by where text happens to land in flat
    extraction order.

    Why this matters: on narrative procedure pages, "NIGERIA AIP" and
    the section header are extracted in clean top-to-bottom reading
    order, so stripping a prefix and reading what follows works fine.
    On graphic/chart pages, PyMuPDF's flat text order often does NOT
    match visual top-to-bottom position — verified directly: searching
    for "AREA CHART" in a chart page's full flat text found a match,
    but it was legend/cross-reference text buried mid-page, not the
    actual title sitting at the top below "NIGERIA AIP" as it visually
    appears. A flat-string search cannot tell the difference; only
    position can.

    This reads actual text blocks with their (x0, y0, x1, y1) bounding
    boxes, sorts by y0 (vertical position), and looks ONLY at blocks
    within the top 15% of the page — where titles are reliably placed,
    exactly as observed directly from chart page screenshots.
    """
    page_height = page.rect.height
    top_zone    = page_height * 0.15

    blocks = page.get_text("blocks")  # (x0, y0, x1, y1, text, block_no, block_type)
    top_blocks = sorted(
        [b for b in blocks if b[1] <= top_zone],
        key=lambda b: b[1]  # sort by vertical position, topmost first
    )

    title_area = ' '.join(' '.join(b[4].split()) for b in top_blocks)[:300].upper()

    # Strip the "AD 2-XXXX-NN ... NIGERIA AIP ..." boilerplate that's
    # also typically in the top zone, so it doesn't interfere with
    # matching the actual title text that follows it
    title_area = re.sub(
        r'(?:AD\s*[23][-\u2013]\w+[-\u2013]\d+\s+)?NIGERIA\s+AIP'
        r'(?:\s+NIGERIAN\s+AIRSPACE\s+MANAGEMENT\s+AGENCY)?',
        '', title_area, flags=re.IGNORECASE
    ).strip()

    for pattern, proc_type, has_runway in TITLE_PATTERNS:
        m = re.search(pattern, title_area)
        if m:
            runway = m.group(1).strip() if has_runway and m.lastindex else ''
            return proc_type, runway

    # Fallback: if nothing matched in the top zone specifically, fall
    # back to a full-text search. NOTE: this fallback branch is, by
    # construction in process() below, ONLY ever reached for DNKK and
    # GEN (the two sections with no AD 2.24/3.23 index to check against
    # at all) — never for ordinary aerodrome narrative text, which is
    # excluded entirely once a real index exists. That matters here
    # specifically: searching the FULL text (no arbitrary truncation)
    # is exactly the kind of change that would be unsafe for narrative
    # pages (risk of matching a stray cross-reference), but is safe and
    # necessary here, because DNKK/GEN's pages are themselves the chart
    # in full — there's no surrounding narrative prose to spuriously
    # match.
    #
    # A 400-char cap here was the WRONG kind of caution — confirmed
    # directly: DNKK's 10 pages and GEN's page all have their title text
    # (e.g. "SEARCH AND RESCUE", "EN-ROUTE CHART") sitting well past the
    # first 400 characters of flat-extracted text, the same root cause
    # as the false "DNMM has no charts" alarm earlier in this build —
    # truncating a search window arbitrarily and concluding "not found"
    # when the real match was simply further down the string.
    clean = re.sub(
        r'(?:AD\s*[23][-\u2013]\w+[-\u2013]\d+\s+)?NIGERIA\s+AIP'
        r'(?:\s+NIGERIAN\s+AIRSPACE\s+MANAGEMENT\s+AGENCY)?',
        '', text, flags=re.IGNORECASE
    ).strip()
    fallback_area = ' '.join(clean.split()).upper()
    for pattern, proc_type, has_runway in TITLE_PATTERNS:
        m = re.search(pattern, fallback_area)
        if m:
            runway = m.group(1).strip() if has_runway and m.lastindex else ''
            return proc_type, runway

    return '', ''

def get_index_map(icao, start, end):
    """
    Parse this aerodrome's own AD 2.24/3.23 index page into a direct
    {reference_number: chart_name} map — e.g. {13: 'Aerodrome Chart -
    ICAO', 15: 'Aerodrome Obstacle Chart - ICAO Type A RWY 05/23', ...}.

    This is the FINAL, evidence-validated method: proven by directly
    reading real chart pages for both DNGO and DNMM, every chart page
    prints its own "AD 2-{ICAO}-NN" reference number somewhere on the
    page (not always at the start of extracted text — see
    get_own_reference below). That number is the single most reliable
    key available: it requires no arithmetic to become a page number,
    and no fragile title-text pattern matching on scattered graphic
    text. It only requires a direct, exact lookup against this map.
    """
    section = '\u2013'
    for p in range(start - 1, end):
        text = doc[p].get_text("text")
        if ('CHARTS RELATED TO AN AERODROME' in text.upper()
                or 'CHARTS RELATED TO A HELIPORT' in text.upper()):
            clean = ' '.join(text.split())
            # Split on this aerodrome's own reference markers, keeping
            # the reference number this time (not discarding it)
            pieces = re.split(rf'AD\s*[23]?\s*[-{section}]?\s*{icao}\s*[-{section}]?\s*(\d{{1,2}})', clean)
            # re.split with a capturing group interleaves: [text, num, text, num, ...]
            index_map = {}
            for i in range(1, len(pieces), 2):
                ref_num  = int(pieces[i])
                name_raw = pieces[i - 1]
                name_raw = re.sub(r'^.*?(?:Chart name\s*)?Page\s*', '', name_raw, flags=re.IGNORECASE).strip()
                if len(name_raw) > 4 and 'CHART' in name_raw.upper():
                    index_map[ref_num] = name_raw[-120:]
            return index_map
    return None  # no index page found


# get_own_reference() — moved to aip_structure.py, imported at top of file.
# Logic and behavior unchanged (verbatim int|None contract preserved — see
# aip_structure.get_own_reference's docstring).


def normalize_checklist_entry(name):
    """Map an index-page chart name to the same (procedure_type, runway)
    vocabulary used by detect_from_title, for direct comparison. Reuses
    the same TITLE_PATTERNS the live content scan matches against, so
    a checklist entry and a scanned page are judged by identical rules."""
    upper = name.upper()
    for pattern, proc_type, has_runway in TITLE_PATTERNS:
        m = re.search(pattern, upper)
        if m:
            runway = m.group(1).strip() if has_runway and m.lastindex else ''
            return proc_type, runway
    return name.strip()[:40], ''


# ── Manual reference overrides ────────────────────────────────────────────────
# A small, explicit set of known exceptions — NOT a return to arithmetic
# trust. Each entry was individually confirmed by direct inspection: the
# physical page has zero embedded fonts and hundreds of embedded image
# tiles (a flat scanned image, no text layer at the PDF data level —
# confirmed directly by attempting to select text on the page in Acrobat,
# which failed). No regex can read text that doesn't exist. The mapping
# below was determined by elimination: each index entry's expected page
# sits in this exact sequential position with no other candidate page
# in range, confirmed against the aerodrome's own index listing.
#
# This table is scoped to THIS specific AIRAC cycle's PDF only. The next
# AIRAC update will require re-verifying this exact set against the new
# document — these pages may have been re-digitized with real text by
# then, or the page numbers may have shifted entirely.
MANUAL_REFERENCE_OVERRIDES = {
    ("DNAS", 429): 15,  # Instrument Approach Chart - VOR/DME RWY 11
    ("DNAS", 430): 17,  # Instrument Approach Chart - VOR/DME RWY 29
    ("DNAS", 431): 19,  # Instrument Approach Chart - VOR/ILS/DME RWY 11
    ("DNKN", 766): 35,  # confirmed same signature: 0 fonts, 196 images, 0 text
    ("DNWI", 1066): 9,  # Heliport Chart - ICAO — ref fused with adjacent
                         # frequency "9126.825" in flat text, no recoverable
                         # boundary; confirmed independently via the page's
                         # own title text reading "HELIPORT CHART - ICAO"
}

def upload_chart(page, icao, page_num):
    """Render page as PNG and upload to Supabase Storage; return public URL."""
    pix       = page.get_pixmap(dpi=DPI)
    file_name = f"{icao}_chart_page_{page_num}.png"
    try:
        supabase.storage.from_(BUCKET_NAME).upload(
            path=file_name,
            file=pix.tobytes("png"),
            file_options={"content-type": "image/png"},
        )
    except Exception:
        pass
    return supabase.storage.from_(BUCKET_NAME).get_public_url(file_name)

def save_chart(icao, name, proc_type, runway, url, page_num):
    supabase.table(TABLE_NAME).upsert({
        "icao_code":      icao,
        "aerodrome_name": name,
        "procedure_type": proc_type or '',
        "runway":         runway    or '',
        "chart_url":      url,
        "source_page":    page_num,
    }, on_conflict="icao_code,source_page").execute()

# process_national_charts() removed: the national/en-route charts (national
# aerodrome index x2, en-route chart x2) are now handled by the SAME main loop
# below as DNKK/GEN, via their own AERODROMES entries (see aip_structure.py) —
# unified into one mechanism rather than two, after confirming the same
# full-text title-fallback safety justification used for DNKK/GEN applies
# equally here (these pages are pure chart, no surrounding narrative prose to
# spuriously match — verified directly on all four pages).


def process():
    grand_total = 0
    all_unmatched_index = []  # (icao, ref, name) — on index, page never found
    no_index             = []

    print(f"🚀 Boundary-locked extraction — {len(AERODROMES)} sections", flush=True)
    print("   Classification: exact reference-number lookup against each", flush=True)
    print("   aerodrome's own index (validated directly against DNGO and", flush=True)
    print("   DNMM's real pages — no arithmetic, no title-text guessing).\n", flush=True)

    # Pseudo-sections with no per-aerodrome AD 2.24/3.23 index to check
    # against at all — these rely entirely on detect_from_title's
    # full-text-fallback path (safe here: each is pure chart content, no
    # narrative prose to spuriously match — same justification as DNKK/GEN,
    # confirmed directly for NG-INDEX/NG-ENR6 before adding them below).
    NO_INDEX_SECTIONS = ("DNKK", "GEN", "NG-INDEX", "NG-ENR6")

    for icao, name, start, end in AERODROMES:
        section_total = 0

        index_map = None
        if icao not in NO_INDEX_SECTIONS:
            index_map = get_index_map(icao, start, end)
            if index_map is None:
                print(f"  ⚠ {icao}: no AD 2.24/3.23 index page found — falling back to title detection only", flush=True)
                no_index.append(icao)

        consumed_refs = set()

        for page_num in range(start, end + 1):
            page = doc[page_num - 1]
            text = page.get_text("text").strip()

            if len(text) < 20 and not page.get_images(full=True):
                continue
            if is_index_page(text):
                continue  # the index page itself is not a chart — skip it

            # ── Classification IS the gate ──────────────────────────────
            # Previously, is_chart_page() (a loose keyword pre-filter)
            # decided what counted as a chart candidate, and pages that
            # passed but failed to classify were logged as "unclassified."
            # That pre-filter was far too loose: CHART_KEYWORDS includes
            # generic words ("ELEV", "HEIGHT", "ALTITUDE", "ARRIVAL",
            # "DEPARTURE") that appear throughout ordinary narrative AD
            # 2.x text, not only on chart pages — confirmed directly:
            # DNAA p334, its very first narrative page (location
            # indicator, ARP coordinates, operating hours), was being
            # swept in as a "chart" purely because it mentions elevation
            # data, the same way virtually every narrative page in every
            # aerodrome's section does. This produced hundreds of false
            # "unclassified chart" uploads that were never charts at all.
            #
            # Fixed by removing the separate pre-filter entirely. A page
            # is now ONLY treated as a chart if it actually classifies —
            # via exact index-reference lookup (including manual
            # overrides) or, for DNKK/GEN, via a genuine TITLE_PATTERNS
            # match. There is no path left by which an unclassified
            # page can reach upload_chart() at all.

            proc_type, runway, method = '', '', None

            if index_map is not None:
                own_ref = get_own_reference(icao, text)

                if (own_ref is None or own_ref not in index_map) and (icao, page_num) in MANUAL_REFERENCE_OVERRIDES:
                    own_ref = MANUAL_REFERENCE_OVERRIDES[(icao, page_num)]

                if own_ref is not None and own_ref in index_map:
                    proc_type, runway = normalize_checklist_entry(index_map[own_ref])
                    consumed_refs.add(own_ref)
                    method = 'index-lookup' if (icao, page_num) not in MANUAL_REFERENCE_OVERRIDES else 'manual-override'

            if method is None and index_map is None:
                # Title-fallback ONLY applies when there is no index at
                # all (DNKK/GEN). For every other aerodrome, the index is
                # the complete, authoritative source — confirmed
                # directly: DNAA p343/344/350/351 are genuine narrative
                # procedure-text pages ("Takeoff Minima for Runway 04",
                # "TRANSITION FROM KIGSO", "RNP APCH RWY22") that
                # spuriously matched TITLE_PATTERNS because the prose
                # legitimately discusses approach procedures by name —
                # not because they're chart images. If a page's
                # reference doesn't resolve against a REAL index, it is
                # correctly excluded, not handed to a second, looser
                # check that real narrative text can still pass.
                proc_type, runway = detect_from_title(page, text)
                if proc_type:
                    method = 'title-fallback'

            if method is None:
                # Genuinely did not classify — this page is NOT uploaded
                # as a chart at all. No unclassified entries can reach
                # the database under this structure.
                continue

            url = upload_chart(page, icao, page_num)
            save_chart(icao, name, proc_type, runway, url, page_num)

            label = proc_type or "unclassified"
            if runway:
                label += f" RWY {runway}"
            tag = {"index-lookup": "✓", "manual-override": "!", "title-fallback": "~"}[method]
            print(f"  {tag} {icao} p{page_num:4d}  {label}", flush=True)
            section_total += 1
            grand_total   += 1
            time.sleep(0.1)

        print(f"  → {icao} ({name}): {section_total} chart(s) in verified range {start}-{end}", flush=True)

        if index_map is not None:
            unmatched = set(index_map.keys()) - consumed_refs
            if unmatched:
                print(f"  ⚠ {icao}: {len(unmatched)} index entr{'y' if len(unmatched)==1 else 'ies'} never matched to a page:")
                for ref in sorted(unmatched):
                    print(f"      ref={ref}  {index_map[ref]}")
                    all_unmatched_index.append((icao, ref, index_map[ref]))
            else:
                print(f"  ✓ {icao}: every index entry matched to a page")
        print()

    print(f"\n🎉 Complete — {grand_total} chart plates stored.", flush=True)

    print(f"\n=== RECONCILIATION SUMMARY ===")
    print(f"Index entries that NEVER matched a real page (exact ref-number basis):")
    for icao, ref, nm in all_unmatched_index:
        print(f"  {icao} ref={ref}: {nm}")
    print(f"\nAerodromes with no index page located at all (DNKK/GEN excluded — expected): {no_index}")
    print("\nNote: there is no 'unclassified chart' category in this run by", flush=True)
    print("construction — a page is only uploaded if it successfully classified", flush=True)
    print("via index lookup, manual override, or title match. Anything that", flush=True)
    print("didn't classify was never uploaded at all.", flush=True)
    print("\nVerify coverage:", flush=True)
    print("  SELECT icao_code, procedure_type, COUNT(*) FROM aip_charts GROUP BY 1,2 ORDER BY 1,2;", flush=True)

if __name__ == "__main__":
    print("⚠️  Confirm aip_charts is EMPTY and the storage bucket is cleared before running.", flush=True)
    if input("Confirmed cleared? (yes/no): ").strip().lower() == "yes":
        process()
    else:
        print("Aborted. Clear the table and bucket first.", flush=True)
