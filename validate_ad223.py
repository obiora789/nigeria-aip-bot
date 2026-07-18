#!/usr/bin/env python3
"""
validate_ad223.py — full 36-aerodrome proof for AD223Extractor.

Runs classify_page -> segment_aerodrome -> (charts-index salvage if needed) ->
AD223Extractor for every standard aerodrome, through the REAL pipeline on the
real PDF. Prints the reconstructed additional-information text for each
aerodrome so reading order can be eyeballed against the source, and asserts
every aerodrome yields non-empty 2.23 text (there is no genuine-empty 2.23 in
this AIP — every aerodrome carries at least the "2.23.1 Bird concentrations in
the vicinity of the aerodrome" line).

Memory-safe: uses pypdfium2 to locate each aerodrome's charts-index page once
(cached in _pages.json by _locate.py if present, else computed here), then
loads only a small pdfplumber window around it. No fitz dependency.

Usage:
    python validate_ad223.py Complete_AIP2026.pdf
"""
import sys
import json
import os
import gc
import re

import pdfplumber

from classify_page import classify_page
from segment_page import segment_aerodrome, Segment
from aip_structure import AERODROMES, STANDARD_36
from ad223_extractor import AD223Extractor, salvage_from_page


def load_words_range(pdf, start, end):
    d = {}
    for p in range(start, end + 1):
        page = pdf.pages[p - 1]
        ws = page.extract_words(use_text_flow=False, keep_blank_chars=False)
        d[p] = [(w['x0'], w['top'], w['x1'], w['bottom'], w['text'])
                for w in ws if w['text'].strip()]
        page.flush_cache()
    return d


def locate_charts_index(pdf_path):
    """Return {icao: charts_index_page}. Prefer the cached _pages.json; fall
    back to a pypdfium2 scan (fast)."""
    if os.path.exists("_pages.json"):
        loc = json.load(open("_pages.json"))
        if all(loc.get(i, {}).get("charts_index") for i in STANDARD_36):
            return {i: loc[i]["charts_index"] for i in STANDARD_36}
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf_path)
    ci = {}
    for icao, name, start, end in AERODROMES:
        if icao not in STANDARD_36:
            continue
        for p in range(start, end + 1):
            tp = doc[p - 1].get_textpage()
            txt = tp.get_text_range(); tp.close()
            if re.search(r'CHARTS?\s+RELATED\s+TO\s+AN', txt.upper()):
                ci[icao] = p
                break
    return ci


def main():
    if len(sys.argv) < 2:
        print("usage: python validate_ad223.py <AIP.pdf>")
        sys.exit(2)
    pdf_path = sys.argv[1]

    charts_index = locate_charts_index(pdf_path)
    extractor = AD223Extractor()
    std_entries = [e for e in AERODROMES if e[0] in STANDARD_36]

    print(f"AD 2.23 extraction across {len(std_entries)} standard aerodromes\n")

    failed, salvaged, all_warnings = [], [], []
    with pdfplumber.open(pdf_path) as pdf:
        for icao, name, start, end in std_entries:
            ci = charts_index.get(icao)
            if ci is None:
                failed.append(icao)
                print(f"{icao}: FAIL — could not locate charts-index page")
                continue

            w0 = max(start, ci - 4)
            page_words = load_words_range(pdf, w0, ci)

            segments = segment_aerodrome(icao, page_words, classify_page)
            ad223 = [s for s in segments if s.subsection == "2.23"]

            note = ""
            if not ad223:
                # No 2.23 segment: the block may be orphaned on a page that did
                # not classify as AD_CONTENT (CHART_INDEX on DNMA, CHART_PLATE on
                # DNKS). Scan the loaded window for a page carrying the 2.23
                # header and salvage from the first one found.
                for p in sorted(page_words):
                    band = salvage_from_page(icao, page_words[p])
                    if band:
                        ad223 = [Segment(icao=icao, subsection="2.23", title="",
                                         page_index=p, words=band,
                                         is_continuation=False, excluded=False)]
                        note = f" [SALVAGED p{p}]"
                        salvaged.append(icao)
                        break

            result = extractor.extract(icao, ad223)
            issues = extractor.validate(result)
            errors = [i for i in issues if i.severity == "error"]
            if errors:
                failed.append(icao)
            if result.warnings:
                all_warnings.extend(f"{icao}: {w}" for w in result.warnings)

            status = "OK" if not errors else \
                f"FAIL: {'; '.join(i.message for i in errors)}"
            print(f"--- {icao} ({name}){note}  [{status}]")
            print(f"    {result.text}\n")

            del page_words, segments
            gc.collect()

    print("=" * 90)
    print(f"clean: {len(std_entries) - len(set(failed))}/{len(std_entries)}")
    if salvaged:
        print(f"salvaged from charts-index page: {salvaged}")
    if failed:
        print(f"FAILED: {sorted(set(failed))}")
    if all_warnings:
        print("\nWarnings:")
        for w in all_warnings:
            print(f"  {w}")
    print("=" * 90)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
