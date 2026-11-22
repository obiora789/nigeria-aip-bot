#!/usr/bin/env python3
"""
diagnose_chart_index.py — every AD 2.24 chart-index entry, old label vs new.

READ-ONLY and free: reads the AIP PDF (PDF_PATH in .env), uploads nothing,
writes nothing, calls no model. Run it from the repo folder AFTER unzipping
the fix (it needs the new chart_titles.py and the old rules are inlined
below for comparison).

Stored chart labels come from these index names (extract_charts.py
normalize_checklist_entry). The old rule order filed
"Standard Instrument Departure Chart - ICAO RNAV1 (GNSS) RWY 04" as an RNAV
approach; this prints, for every index entry, what the OLD rules and the NEW
chart_titles.py rules make of it, and marks every entry that changes.

    python diagnose_chart_index.py > chart_index.txt
"""
import re

import extract_charts as ec          # opens the PDF; process() is NOT run
from aip_structure import AERODROMES
from chart_titles import classify_title

# The rule order before 2026-11-21, kept here only for this comparison.
_OLD = [
    (r'PRECISION\s+APPROACH\s+TERRAIN', 'Precision Approach Terrain Chart', False),
    (r'ILS\s*(?:/\s*DME)?(?:\s+OR\s+LOC)?[^R]*RWY\s*(\d{2}[LRC]?)', 'ILS Approach Chart', True),
    (r'(?:RNAV|GNSS|RNP)\s*(?:\([^)]*\))?[^R]*RWY\s*(\d{2}[LRC]?)', 'RNAV Approach Chart', True),
    (r'VOR\s*(?:/\s*DME)?[^R]*RWY\s*(\d{2}[LRC]?)', 'VOR Approach Chart', True),
    (r'NDB\s*(?:/\s*DME)?[^R]*RWY\s*(\d{2}[LRC]?)', 'NDB Approach Chart', True),
    (r'INSTRUMENT\s+APPROACH[^R]*RWY\s*(\d{2}[LRC]?)', 'Instrument Approach Chart', True),
    (r'VISUAL\s+APPROACH[^R]*RWY\s*(\d{2}[LRC]?)', 'Visual Approach Chart', True),
    (r'OBSTACLE\s+CHART[^R]*RWY\s*(\d{2}[LRC]?)', 'Aerodrome Obstacle Chart', True),
    (r'STANDARD\s+(?:INSTRUMENT\s+)?DEPARTURE|SID\b.*?ICAO', 'SID Chart', False),
    (r'STANDARD\s+(?:TERMINAL\s+)?ARRIVAL|STAR\b.*?ICAO', 'STAR Chart', False),
    (r'AREA\s+CHART.*?ARRIVAL\s+AND\s+TRANSIT', 'Area Chart - Arrival and Transit Routes', False),
    (r'AREA\s+CHART.*?DEPARTURE\s+AND\s+TRANSIT', 'Area Chart - Departure and Transit Routes', False),
    (r'AREA\s+CHART', 'Area Chart', False),
    (r'GROUND\s+MOVEMENT\s+CHART', 'Ground Movement Chart', False),
    (r'(?:AIRCRAFT\s+)?PARKING\s*(?:/\s*DOCKING)?\s*CHART', 'Parking / Docking Chart', False),
    (r'AERODROME\s+CHART|\bADC\b', 'Aerodrome Chart', False),
    (r'HELIPORT\s+CHART', 'Heliport Chart', False),
    (r'HELICOPTER\s+TRAFFIC\s+CHART', 'Helicopter Traffic Chart', False),
    (r'EN.?ROUTE\s+CHART|ENR\s+6', 'En-Route Chart', False),
    (r'SEARCH\s+AND\s+RESCUE', 'Search and Rescue Units Chart', False),
    (r'AERODROME\s+INDEX', 'Aerodrome Index Chart', False),
]


def old(name):
    u = name.upper()
    for p, kind, rw in _OLD:
        m = re.search(p, u)
        if m:
            return kind, (m.group(1).strip() if rw and m.lastindex else '')
    return name.strip()[:40], ''


def main():
    changed = 0
    for icao, _name, start, end in AERODROMES:
        if icao in ("DNKK", "GEN", "NG-INDEX", "NG-ENR6"):
            continue
        index_map = ec.get_index_map(icao, start, end) or {}
        for ref in sorted(index_map):
            nm = index_map[ref]
            o = old(nm)
            n = classify_title(nm) or (nm.strip()[:40], '')
            mark = "  <-- CHANGES" if o != n else ""
            changed += bool(mark)
            print(f"{icao} AD-{ref:<3} old={o[0]:<26} {o[1]:<4} new={n[0]:<26} {n[1]:<4} "
                  f"| {' '.join(nm.split())[:90]}{mark}")
    print(f"\n{changed} index entr(ies) get a different label with the new rules.")


if __name__ == "__main__":
    main()
