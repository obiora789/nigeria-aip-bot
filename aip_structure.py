"""
aip_structure.py — single source of truth for AIP document structure.

Owns the content-verified page boundaries and header-parsing logic that BOTH
extract_charts.py (chart image pipeline) and classify_page.py (structured-data
extraction pipeline) depend on. Neither module should re-derive this
information independently.

WHY THIS MODULE EXISTS:
Before this refactor, extract_charts.py and classify_page.py each independently
worked out "what pages belong to which aerodrome" — one via a hand-verified
boundary table (built from direct header inspection), the other via its own
regex header matching (built from a separate survey pass). On a real test run,
the two disagreed about 48 pages: classify_page.py flagged 50 pages as
"missing content" that were, in fact, real chart plates extract_charts.py
already handles correctly.

This is the SAME failure class as the AD 2.12 misattribution incident that
motivated this whole project: two pieces of code, each internally consistent,
silently disagreeing about the document's own structure, because they were
built from separate passes at separate times. A shared module — used by both —
closes that gap permanently. When the next AIRAC cycle reflows page numbers,
there is exactly ONE place to re-verify, and every consumer inherits the fix.

MAINTENANCE: AERODROMES boundaries are scoped to Complete_AIP2026.pdf (AIRAC
AMDT 03/2026). Re-verify against the new document each AIRAC cycle by
confirming the "AD 2-{ICAO}-1" / "AD 3-{ICAO}-1" header on each start page —
never recompute arithmetically. End page = next entry's verified start - 1.
"""
import re

# ── content-verified aerodrome/heliport/special-section page boundaries ─────
# Moved verbatim from extract_charts.py (do not re-derive independently).
# Every value confirmed by direct regex match against the "AD 2-{ICAO}-1" /
# "AD 3-{ICAO}-1" header on that exact page — never calculated.
AERODROMES = [
    # (icao, name, true_start, true_end)
    ("DNAA", "Abuja/Nnamdi Azikiwe",          334, 368),
    ("DNAI", "Uyo/Victor Attah",               369, 384),
    ("DNAK", "Akure",                          385, 397),
    ("DNAN", "Umueri/Chinua Achebe",           398, 415),
    ("DNAS", "Asaba",                          416, 433),
    ("DNBB", "Bebi Airstrip",                  434, 442),
    ("DNBC", "Bauchi/Tafawa Balewa",           443, 460),
    ("DNBE", "Benin",                          461, 484),
    ("DNBK", "Birnin Kebbi/Sir Ahmadu Bello",  485, 502),
    ("DNBY", "Amassoma/Bayelsa",               503, 520),
    ("DNCA", "Calabar/Margaret Ekpo",          521, 542),
    ("DNDS", "Dutse",                          543, 560),
    ("DNEN", "Enugu/Akanu Ibiam",              561, 581),
    ("DNES", "Escravos",                       582, 594),
    ("DNET", "Ado Ekiti",                      595, 611),
    ("DNFB", "Bonny/Finima Airstrip",          612, 624),
    ("DNFD", "Forcados Terminal",              625, 637),
    ("DNGO", "Gombe",                          638, 656),
    ("DNIB", "Ibadan",                         657, 669),
    ("DNIL", "Ilorin",                         670, 685),
    ("DNIM", "Owerri/Sam Mbakwe",              686, 702),
    ("DNJO", "Jos/Yakubu Gowon",               703, 716),
    ("DNKA", "Kaduna/New Kaduna",              717, 736),
    ("DNKN", "Kano/Mallam Aminu Kano",         737, 780),
    ("DNKS", "Kashimbila",                     781, 797),
    ("DNKT", "Katsina/Umaru Musa Yaradua",     798, 815),
    ("DNMA", "Maiduguri",                      816, 835),
    ("DNMK", "Makurdi",                        836, 850),
    ("DNMM", "Lagos/Murtala Muhammed",         851, 893),
    ("DNMN", "Minna",                          894, 911),
    ("DNOG", "Ogun/Gateway",                   912, 929),
    ("DNPO", "Port Harcourt/Obafemi Awolowo",  930, 959),
    ("DNSO", "Sokoto/Saddiq Abubakar III",     960, 979),
    ("DNSU", "Osubi",                          980, 994),
    ("DNYO", "Yola",                           995, 1010),
    ("DNZA", "Zaria",                         1011, 1025),
    # Heliports (AD 3 section)
    ("DNGB", "Gbaran Ubie Heliport",          1026, 1036),
    ("DNPS", "Port Harcourt Shell Industrial Area Heliport", 1037, 1046),
    ("DNSK", "Soku Heliport",                 1047, 1057),
    ("DNWI", "Warri Industrial Area Heliport",1058, 1067),
    # NOTE: DNWI's end was originally set to 1073 (the document's last page),
    # the ONLY entry in this table whose end could NOT be verified by "next
    # entry's real start - 1" (there is no next entry after the last heliport).
    # Confirmed by direct inspection: DNWI's own "AD 3-DNWI-N" header runs
    # through page 1067. Pages 1068-1073 are NOT DNWI content — p1068 is a
    # second DNXX pseudo-entry (a heliport catalogue, "AD 3-DNXX-1... List of
    # heliports, helipads and helidecks" — the AD-3 counterpart to the AD-2
    # DNXX airstrips catalogue at page 1024), and 1070-1073 are the document's
    # own back-matter (a national aerodrome index graphic, a parts-overview
    # graphic, and FIR/airspace boundary map pages) — not aerodrome/heliport
    # content at all. Every OTHER entry in this table is safely bounded by the
    # next entry's independently-verified start, so this was an isolated gap
    # specific to being the final row, not a symptom of a wider problem
    # (spot-checked: DNKN/780 -> DNKS/781 transition is clean).
    # Already independently verified, unaffected by this bug
    ("DNKK", "Kano FIR Nigeria Airspace",      291,  300),
    ("GEN",  "Nigeria Search and Rescue",      114,  114),
    # National/en-route charts, unified into this table (not a separate list)
    # after confirming they use the SAME safe mechanism as DNKK/GEN above:
    # detect_from_title's full-text-fallback path, justified because these
    # pages are pure chart content with no narrative prose to spuriously
    # match. Found via a full-document vector-draw density scan of every
    # non-aerodrome page (genuine charts: tens of thousands of drawn
    # line/curve objects; densest non-chart prose page had ~100 — an
    # unambiguous separation). Title-match verified directly for each:
    # "AERODROME INDEX" (p329, p1070) and "EN-ROUTE CHART"/"ENR 6" (p1072,
    # p1073) both matched cleanly via TITLE_PATTERNS. NG-INDEX appears twice
    # because its two occurrences are non-contiguous pages (329 and 1070) —
    # page_to_section() only needs disjoint ranges, not unique icao strings.
    # Re-verify each AIRAC cycle: re-run the density scan; a new chart
    # elsewhere in ENR/GEN would surface as a >500-draw non-aerodrome page.
    ("NG-INDEX", "National Aerodrome Index (AD 1.3)",   329,  329),
    ("NG-INDEX", "National Aerodrome Index (interactive)", 1070, 1070),
    ("NG-ENR6",  "En-Route Chart (ENR 6)",              1072, 1073),
]

HELIPORT_ICAO = {"DNGB", "DNPS", "DNSK", "DNWI"}
# no AD 2.1-2.24 admin block, no aerodrome index — all rely on
# detect_from_title's title-fallback path, not index-reference lookup
_SPECIAL_ICAO = {"DNKK", "GEN", "NG-INDEX", "NG-ENR6"}
STANDARD_36 = {
    icao for icao, *_ in AERODROMES
    if icao not in HELIPORT_ICAO and icao not in _SPECIAL_ICAO
}

# DNXX (airstrips/private-aerodromes catch-all) is NOT its own section in this
# table — it's a pseudo-ICAO page embedded inside another aerodrome's own page
# range (confirmed at page 1024, inside DNZA's 1011-1025 range). It must be
# detected by content, not boundary lookup.
SPECIMEN_ICAO = {"DNXX"}


def page_to_section(page_num):
    """Look up which AERODROMES entry (if any) contains this page.
    Returns (icao, name, start, end) or None if the page falls outside every
    known boundary (front matter, AD 1.x intro, ENR, GEN content)."""
    for entry in AERODROMES:
        icao, name, start, end = entry
        if start <= page_num <= end:
            return entry
    return None


def get_own_reference(icao, text):
    """Read this page's own self-printed "AD 2-{icao}-NN" / "AD 3-{icao}-NN"
    reference NUMBER, returning int or None.

    Moved VERBATIM (signature and behavior unchanged) from extract_charts.py,
    which depends on this exact int|None contract for its index_map lookups
    (`own_ref in index_map`, keyed by int). Do not change this return type —
    see get_own_part() below for the separate part-number ("2"/"3") need.

    The source AIP uses at least FOUR different separator formats for this
    reference across different pages: "AD 2-DNGO-13" (tight), "AD 2 - DNET -
    15" (spaced), "AD-DNES-11" (part-type digit omitted), and "AD2-DNMN 15"
    (no dash before the page number). Both dashes are optional — separation
    relies on \\s* alone, with the ICAO code itself as the strong anchor
    preventing false matches.

    Capped to 1-2 digits: FIXED (round 3) after confirming DNIL page 684 prints
    "AD2-DNIL-17" immediately followed by "119.6" (a tower frequency) with zero
    separating whitespace, so an uncapped (\\d+) captured "17119" instead of
    "17". Every reference number observed across all 40 aerodromes tops out in
    the 30s-50s, never three digits, so this cap is safe.
    """
    section = '\u2013'
    m = re.search(
        rf'AD\s*[23]?\s*[-{section}]?\s*{icao}\s*[-{section}]?\s*(\d{{1,2}})',
        text
    )
    return int(m.group(1)) if m else None


def get_own_part(icao, text):
    """Read this page's own self-printed part number ("2" or "3" in
    "AD 2-{icao}-NN" / "AD 3-{icao}-NN"), returning "2", "3", or None if the
    part digit itself was dropped (the "AD-DNES-11" degenerate case).

    NEW function, used only by classify_page.py — extract_charts.py does not
    need this distinction (it scans each aerodrome/heliport's full verified
    range regardless of part, and AERODROMES boundaries already separate
    standard aerodromes from heliports). classify_page.py needs it because,
    within a single heliport's page range, SOME pages are AD 2.x (a chart)
    and others are AD 3.x (procedures) — different structured-extraction
    treatment for each."""
    section = '\u2013'
    m = re.search(rf'AD\s*([23])\s*[-{section}]?\s*{icao}', text)
    return m.group(1) if m else None


def is_index_page(text):
    """True for the AD 2.24/3.23 chart INDEX page itself (a text listing of
    chart names + reference numbers) — never a chart image page. Moved
    verbatim from extract_charts.py. The index page contains the word "CHART"
    multiple times in its own heading/rows and would otherwise be
    misclassified by the same title-matching logic used for real chart pages;
    this check must run before any chart-plate classification attempt."""
    return bool(re.search(
        r'CHARTS?\s+RELATED\s+TO\s+AN?\s+(?:AERODROME|HELIPORT)',
        text.upper()
    ))
