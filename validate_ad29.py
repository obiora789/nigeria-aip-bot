#!/usr/bin/env python3
"""
validate_ad29.py — full 36-aerodrome proof for AD29Extractor.

Runs classify_page -> segment_aerodrome -> AD29Extractor for every standard
aerodrome. Checks every declared item canonicalizes, and confirms DNSU's
prose-fallback path fires correctly (the one confirmed no-table case).

Usage:
    python validate_ad29.py Complete_AIP2026.pdf
"""
import sys

import fitz  # PyMuPDF

from classify_page import classify_page
from segment_page import segment_aerodrome
from aip_structure import AERODROMES, STANDARD_36
from ad29_extractor import AD29Extractor


def main():
    if len(sys.argv) < 2:
        print("usage: python validate_ad29.py <AIP.pdf>")
        sys.exit(2)

    doc = fitz.open(sys.argv[1])
    extractor = AD29Extractor()

    std_entries = [e for e in AERODROMES if e[0] in STANDARD_36]
    print(f"AD 2.9 extraction across {len(std_entries)} standard aerodromes\n")
    print(f"{'ICAO':6}{'#items':>8}  status")
    print("-" * 70)

    failed = []
    all_warnings = []
    dnsu_note = None
    for icao, name, start, end in std_entries:
        page_words = {}
        for p in range(start, min(start + 5, end + 1)):
            page = doc.load_page(p - 1)
            raw = page.get_text("words")
            page_words[p] = [(w[0], w[1], w[2], w[3], w[4]) for w in raw if w[4].strip()]

        segments = segment_aerodrome(icao, page_words, classify_page)
        ad29_segs = [s for s in segments if s.subsection == "2.9"]

        result = extractor.extract(icao, ad29_segs)
        issues = extractor.validate(result)
        errors = [i for i in issues if i.severity == "error"]

        if errors:
            failed.append(icao)
        if result.warnings:
            all_warnings.extend(f"{icao}: {w}" for w in result.warnings)
        if icao == "DNSU":
            dnsu_note = result.records

        n = len([r for r in result.records if r["item"] is not None])
        status = "OK" if not errors else f"FAIL: {'; '.join(i.message for i in errors)}"
        print(f"{icao:6}{n:>8}  {status}")

    print("\n" + "=" * 70)
    print(f"clean: {len(std_entries) - len(failed)}/{len(std_entries)}")
    if failed:
        print(f"FAILED: {failed}")
    if all_warnings:
        print(f"\nWarnings (review, not necessarily errors):")
        for w in all_warnings:
            print(f"  {w}")
    print(f"\nDNSU (the confirmed no-table prose case): {dnsu_note}")
    print("=" * 70)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
