#!/usr/bin/env python3
"""
validate_ad23.py — full 36-aerodrome proof for AD23Extractor.

Runs classify_page -> segment_aerodrome -> AD23Extractor for every standard
aerodrome. Checks: every declared service label canonicalizes to one of the
12 known categories (a real, verified-complete set — see ad23_extractor.py's
docstring), and reports per-aerodrome service counts so genuine content
differences (DNKA's real 10-of-12 table) are visible, not hidden.

Usage:
    python validate_ad23.py Complete_AIP2026.pdf
"""
import sys

import fitz  # PyMuPDF

from classify_page import classify_page
from segment_page import segment_aerodrome
from aip_structure import AERODROMES, STANDARD_36
from ad23_extractor import AD23Extractor


def main():
    if len(sys.argv) < 2:
        print("usage: python validate_ad23.py <AIP.pdf>")
        sys.exit(2)

    doc = fitz.open(sys.argv[1])
    extractor = AD23Extractor()

    std_entries = [e for e in AERODROMES if e[0] in STANDARD_36]
    print(f"AD 2.3 extraction across {len(std_entries)} standard aerodromes\n")
    print(f"{'ICAO':6}{'#services':>10}  status")
    print("-" * 70)

    failed = []
    all_warnings = []
    for icao, name, start, end in std_entries:
        page_words = {}
        for p in range(start, min(start + 3, end + 1)):
            page = doc.load_page(p - 1)
            raw = page.get_text("words")
            page_words[p] = [(w[0], w[1], w[2], w[3], w[4]) for w in raw if w[4].strip()]

        segments = segment_aerodrome(icao, page_words, classify_page)
        ad23_segs = [s for s in segments if s.subsection == "2.3"]

        result = extractor.extract(icao, ad23_segs)
        issues = extractor.validate(result)
        errors = [i for i in issues if i.severity == "error"]

        if errors:
            failed.append(icao)
        if result.warnings:
            all_warnings.extend(f"{icao}: {w}" for w in result.warnings)

        status = "OK" if not errors else f"FAIL: {'; '.join(i.message for i in errors)}"
        n_services = len([r for r in result.records if r["service"] is not None])
        flag = "" if n_services == 12 else f"  ({n_services} of 12 — check if genuine)"
        print(f"{icao:6}{n_services:>10}  {status}{flag}")

    print("\n" + "=" * 70)
    print(f"clean: {len(std_entries) - len(failed)}/{len(std_entries)}")
    if failed:
        print(f"FAILED: {failed}")
    if all_warnings:
        print(f"\nWarnings (review, not necessarily errors):")
        for w in all_warnings:
            print(f"  {w}")
    print("=" * 70)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
