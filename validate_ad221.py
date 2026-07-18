#!/usr/bin/env python3
"""
validate_ad221.py — full 36-aerodrome proof for AD221Extractor.

Runs classify_page -> segment_aerodrome -> AD221Extractor for every standard
aerodrome. Checks every aerodrome produces exactly one record, with status
text present except for the one confirmed genuine gap (DNFD).

Usage:
    python validate_ad221.py Complete_AIP2026.pdf
"""
import sys

import fitz  # PyMuPDF

from classify_page import classify_page
from segment_page import segment_aerodrome
from aip_structure import AERODROMES, STANDARD_36
from ad221_extractor import AD221Extractor


def main():
    if len(sys.argv) < 2:
        print("usage: python validate_ad221.py <AIP.pdf>")
        sys.exit(2)

    doc = fitz.open(sys.argv[1])
    extractor = AD221Extractor()

    std_entries = [e for e in AERODROMES if e[0] in STANDARD_36]
    print(f"AD 2.21 extraction across {len(std_entries)} standard aerodromes\n")
    print(f"{'ICAO':6}{'status':>30}  status")
    print("-" * 90)

    failed = []
    all_warnings = []
    for icao, name, start, end in std_entries:
        page_words = {}
        for p in range(start, min(start + 12, end + 1)):
            page = doc.load_page(p - 1)
            raw = page.get_text("words")
            page_words[p] = [(w[0], w[1], w[2], w[3], w[4]) for w in raw if w[4].strip()]

        segments = segment_aerodrome(icao, page_words, classify_page)
        ad221_segs = [s for s in segments if s.subsection == "2.21"]

        result = extractor.extract(icao, ad221_segs)
        issues = extractor.validate(result)
        errors = [i for i in issues if i.severity == "error"]
        rec = result.records[0] if result.records else {}

        if errors:
            failed.append(icao)
        if result.warnings:
            all_warnings.extend(f"{icao}: {w}" for w in result.warnings)

        status_val = str(rec.get("status"))[:28]
        result_str = "OK" if not errors else f"FAIL: {'; '.join(i.message for i in errors)}"
        print(f"{icao:6}{status_val:>30}  {result_str}")

    print("\n" + "=" * 90)
    print(f"clean: {len(std_entries) - len(failed)}/{len(std_entries)}")
    if failed:
        print(f"FAILED: {failed}")
    if all_warnings:
        print(f"\nWarnings (genuine gaps, not necessarily errors):")
        for w in all_warnings:
            print(f"  {w}")
    print("=" * 90)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
