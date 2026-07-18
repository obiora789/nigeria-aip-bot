#!/usr/bin/env python3
"""
validate_ad212.py — full 36-aerodrome proof for AD212Extractor.

Runs classify_page -> segment_aerodrome -> AD212Extractor for every standard
aerodrome. Checks every runway has both length and width (null-over-guess is
a hard requirement here — see ad212_extractor.py's docstring), and reports
any dimension mismatches between paired ends for manual review.

Usage:
    python validate_ad212.py Complete_AIP2026.pdf
"""
import sys

import fitz  # PyMuPDF

from classify_page import classify_page
from segment_page import segment_aerodrome
from aip_structure import AERODROMES, STANDARD_36
from ad212_extractor import AD212Extractor


def main():
    if len(sys.argv) < 2:
        print("usage: python validate_ad212.py <AIP.pdf>")
        sys.exit(2)

    doc = fitz.open(sys.argv[1])
    extractor = AD212Extractor()

    std_entries = [e for e in AERODROMES if e[0] in STANDARD_36]
    print(f"AD 2.12 extraction across {len(std_entries)} standard aerodromes\n")
    print(f"{'ICAO':6}{'runways'}")
    print("-" * 80)

    failed = []
    all_warnings = []
    for icao, name, start, end in std_entries:
        page_words = {}
        for p in range(start, min(start + 5, end + 1)):
            page = doc.load_page(p - 1)
            raw = page.get_text("words")
            page_words[p] = [(w[0], w[1], w[2], w[3], w[4]) for w in raw if w[4].strip()]

        segments = segment_aerodrome(icao, page_words, classify_page)
        ad212_segs = [s for s in segments if s.subsection == "2.12"]

        result = extractor.extract(icao, ad212_segs)
        issues = extractor.validate(result)
        errors = [i for i in issues if i.severity == "error"]

        if errors:
            failed.append(icao)
        if result.warnings:
            all_warnings.extend(f"{icao}: {w}" for w in result.warnings)

        rwy_str = "; ".join(f"{r['designation']} ({r['length_m']}x{r['width_m']}m)"
                             for r in result.records)
        status = "" if not errors else f"  FAIL: {'; '.join(i.message for i in errors)}"
        print(f"{icao:6}{rwy_str}{status}")

    print("\n" + "=" * 80)
    print(f"clean: {len(std_entries) - len(failed)}/{len(std_entries)}")
    if failed:
        print(f"FAILED: {failed}")
    if all_warnings:
        print(f"\nWarnings (review each — some may be genuine single-direction runways):")
        for w in all_warnings:
            print(f"  {w}")
    print("=" * 80)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
