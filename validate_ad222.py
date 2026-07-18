#!/usr/bin/env python3
"""
validate_ad222.py — full 36-aerodrome proof for the FULL-CAPTURE AD222Extractor.

Loads every page of each aerodrome's AD 2.22 span (AD_CONTENT pages AND the
CHART_PLATE-classified PBN coding-table pages — all ordinary text tables),
renders the position-faithful reconstruction, and asserts NO WORD LOSS: every
non-chrome word in each page's 2.22 band appears in the output. That is the
concrete proof that nothing is excluded. Memory-safe (page-by-page load with
cache flush; these pages are text, not raster charts). No fitz dependency.

Usage:
    python validate_ad222.py Complete_AIP2026.pdf [ICAO ...]
      (optional ICAO list runs a subset — useful for batching within time limits)
"""
import sys
import json
import os
import gc

import pdfplumber

from aip_structure import AERODROMES, STANDARD_36
from ad222_extractor import AD222Extractor


def load_span(pdf, start, end):
    d = {}
    for p in range(start, end + 1):
        page = pdf.pages[p - 1]
        ws = page.extract_words(use_text_flow=False, keep_blank_chars=False)
        d[p] = [(w['x0'], w['top'], w['x1'], w['bottom'], w['text'])
                for w in ws if w['text'].strip()]
        page.flush_cache()
    return d


def main():
    if len(sys.argv) < 2:
        print("usage: python validate_ad222.py <AIP.pdf> [ICAO ...]")
        sys.exit(2)
    pdf_path = sys.argv[1]
    only = set(a.upper() for a in sys.argv[2:]) if len(sys.argv) > 2 else None

    span_map = {}
    raw = json.load(open("_span22.json")) if os.path.exists("_span22.json") else {}
    for icao, d in raw.items():
        if d.get("s22"):
            span_map[icao] = (d["s22"], d["end22"])

    extractor = AD222Extractor(page_span=span_map)
    std_entries = [e for e in AERODROMES if e[0] in STANDARD_36
                   and (only is None or e[0] in only)]

    print(f"AD 2.22 FULL-CAPTURE across {len(std_entries)} aerodromes\n")
    failed, all_notes = [], []
    with pdfplumber.open(pdf_path) as pdf:
        for icao, name, start, end in std_entries:
            span = span_map.get(icao)
            lo, hi = (span if span else (start, end))
            page_words = load_span(pdf, lo, hi)

            result = extractor.extract(icao, page_words)
            issues = extractor.validate(result, page_words)
            errors = [i for i in issues if i.severity == "error"]
            if errors:
                failed.append(icao)
                all_notes.extend(f"{icao}: {i.message}" for i in errors)
            for w in result.warnings:
                all_notes.append(f"{icao}: {w}")

            status = "OK" if not errors else "FAIL"
            nchars = len(result.text)
            print(f"--- {icao} ({name})  [{status}]  span {lo}-{hi}  "
                  f"{nchars} chars")
            del page_words
            gc.collect()

    print("=" * 80)
    print(f"clean (no word loss): {len(std_entries) - len(set(failed))}/{len(std_entries)}")
    if failed:
        print(f"FAILED: {sorted(set(failed))}")
    for n in all_notes:
        print(f"  {n}")
    print("=" * 80)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
