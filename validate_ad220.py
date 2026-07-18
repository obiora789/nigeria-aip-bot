#!/usr/bin/env python3
"""
validate_ad220.py — full 36-aerodrome proof for AD220Extractor.

Runs classify_page -> segment_aerodrome -> AD220Extractor for every standard
aerodrome. Checks every declared item canonicalizes to a known category, and
confirms no cross-column contamination (the specific bug this extractor's
column-aware rebuild was built to prevent — see module docstring).

Usage:
    python validate_ad220.py Complete_AIP2026.pdf
"""
import sys

import fitz  # PyMuPDF

from classify_page import classify_page
from segment_page import segment_aerodrome
from aip_structure import AERODROMES, STANDARD_36
from ad220_extractor import AD220Extractor


def main():
    if len(sys.argv) < 2:
        print("usage: python validate_ad220.py <AIP.pdf>")
        sys.exit(2)

    doc = fitz.open(sys.argv[1])
    extractor = AD220Extractor()

    std_entries = [e for e in AERODROMES if e[0] in STANDARD_36]
    print(f"AD 2.20 extraction across {len(std_entries)} standard aerodromes\n")
    print(f"{'ICAO':6}{'#items':>8}  status")
    print("-" * 90)

    failed = []
    all_warnings = []
    for icao, name, start, end in std_entries:
        page_words = {}
        # wide lookahead — confirmed necessary: DNMM's AD 2.20 sits 8 pages
        # into its range, right at the edge of a narrower window
        for p in range(start, min(start + 12, end + 1)):
            page = doc.load_page(p - 1)
            raw = page.get_text("words")
            page_words[p] = [(w[0], w[1], w[2], w[3], w[4]) for w in raw if w[4].strip()]

        segments = segment_aerodrome(icao, page_words, classify_page)
        ad220_segs = [s for s in segments if s.subsection == "2.20"]

        result = extractor.extract(icao, ad220_segs)
        issues = extractor.validate(result)
        errors = [i for i in issues if i.severity == "error"]

        if errors:
            failed.append(icao)
        if result.warnings:
            all_warnings.extend(f"{icao}: {w}" for w in result.warnings)

        n = len([r for r in result.records if r["item"] is not None])
        status = "OK" if not errors else f"FAIL: {'; '.join(i.message for i in errors)}"
        print(f"{icao:6}{n:>8}  {status}")

    print("\n" + "=" * 90)
    print(f"clean: {len(std_entries) - len(failed)}/{len(std_entries)}")
    if failed:
        print(f"FAILED: {failed}")
    if all_warnings:
        print(f"\nWarnings (review, not necessarily errors):")
        for w in all_warnings:
            print(f"  {w}")
    print("=" * 90)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
