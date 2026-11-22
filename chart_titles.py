"""
chart_titles.py — what kind of chart a title or AD 2.24 index name describes.

Pure (no PDF, no database), so it can be tested; extract_charts.py uses it for
both the index-name lookup and the on-page title fallback.

ORDER MATTERS: the first pattern that matches wins. Measured 2026-11-21
(diagnose_chart_titles.py on the AMDT 03/2026 PDF): 17 SID and STAR pages
were stored as 'RNAV Approach Chart', because "(RNAV|GNSS|RNP) ... RWY nn" was
tried before the SID/STAR patterns and SID/STAR names read like
"Standard Instrument Departure Chart - ICAO RNAV1 (GNSS) RWY 04". Asking for
an RNAV approach plate at Abuja, Benin, Amassoma, Kano, Kashimbila or Port
Harcourt could send a SID or STAR. SID and STAR are therefore decided FIRST,
and they keep their runway when the name gives one.
"""
import re

# (pattern, procedure_type, runway) — runway: True = group 1 of the pattern,
# "any" = the first "RWY nn" anywhere in the title, False = none.
TITLE_PATTERNS = [
    (r'STANDARD\s+(?:INSTRUMENT\s+)?DEPARTURE|\(SID\)|\bSID\b',  'SID Chart',                        "any"),
    (r'STANDARD\s+(?:INSTRUMENT\s+|TERMINAL\s+)?ARRIVAL|\(STAR\)|\bSTAR\b',
                                                                 'STAR Chart',                       "any"),
    (r'PRECISION\s+APPROACH\s+TERRAIN',                          'Precision Approach Terrain Chart', False),
    (r'ILS\s*(?:/\s*DME)?(?:\s+OR\s+LOC)?[^R]*RWY\s*(\d{2}[LRC]?)', 'ILS Approach Chart',          True),
    (r'(?:RNAV|GNSS|RNP)\s*(?:\([^)]*\))?[^R]*RWY\s*(\d{2}[LRC]?)', 'RNAV Approach Chart',         True),
    (r'VOR\s*(?:/\s*DME)?[^R]*RWY\s*(\d{2}[LRC]?)',             'VOR Approach Chart',                True),
    (r'NDB\s*(?:/\s*DME)?[^R]*RWY\s*(\d{2}[LRC]?)',             'NDB Approach Chart',                True),
    (r'INSTRUMENT\s+APPROACH[^R]*RWY\s*(\d{2}[LRC]?)',          'Instrument Approach Chart',          True),
    (r'VISUAL\s+APPROACH[^R]*RWY\s*(\d{2}[LRC]?)',              'Visual Approach Chart',              True),
    (r'OBSTACLE\s+CHART[^R]*RWY\s*(\d{2}[LRC]?)',               'Aerodrome Obstacle Chart',           True),
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

_ANY_RWY = re.compile(r'RWY\s*(\d{2}[LRC]?)')


def classify_title(text: str):
    """(procedure_type, runway) for a chart title or index name, or None."""
    upper = (text or "").upper()
    # An "Area Chart - Departure/Arrival and Transit Routes" is not a SID/STAR
    # even though it says departure/arrival; decide it before SID/STAR.
    if re.search(r'AREA\s+CHART', upper):
        for pattern, proc_type, _ in TITLE_PATTERNS:
            if proc_type.startswith('Area Chart') and re.search(pattern, upper):
                return proc_type, ''
    for pattern, proc_type, has_runway in TITLE_PATTERNS:
        m = re.search(pattern, upper)
        if not m:
            continue
        if has_runway == "any":
            r = _ANY_RWY.search(upper)
            return proc_type, (r.group(1) if r else '')
        runway = m.group(1).strip() if has_runway and m.lastindex else ''
        return proc_type, runway
    return None
