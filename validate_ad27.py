#!/usr/bin/env python3
"""
validate_ad27.py — full 36-aerodrome proof for AD27Extractor.

Runs classify_page -> segment_aerodrome -> AD27Extractor for every standard
aerodrome. Checks every aerodrome produces exactly one record with all 3
fields present as text (possibly "NIL"/"Not applicable" — that's expected,
not a failure, for this subsection).

Usage:
    python validate_ad27.py Complete_AIP2026.pdf
"""
import sys

import fitz  # PyMuPDF

from classify_page import classify_page
from segment_page import segment_aerodrome
from aip_structure import AERODROMES, STANDARD_36
from ad27_extractor import AD27Extractor


def main():
    if len(sys.argv) < 2:
        print("usage: python validate_ad27.py <AIP.pdf>")
        sys.exit(2)

    doc = fitz.open(sys.argv[1])
    extractor = AD27Extractor()

    std_entries = [e for e in AERODROMES if e[0] in STANDARD_36]
    print(f"AD 2.7 extraction across {len(std_entries)} standard aerodromes\n")
    print(f"{'ICAO':6}{'equipment':>22}  status")
    print("-" * 70)

    failed = []
    all_warnings = []
    for icao, name, start, end in std_entries:
        page_words = {}
        for p in range(start, min(start + 4, end + 1)):
            page = doc.load_page(p - 1)
            raw = page.get_text("words")
            page_words[p] = [(w[0], w[1], w[2], w[3], w[4]) for w in raw if w[4].strip()]

        segments = segment_aerodrome(icao, page_words, classify_page)
        ad27_segs = [s for s in segments if s.subsection == "2.7"]

        result = extractor.extract(icao, ad27_segs)
        issues = extractor.validate(result)
        errors = [i for i in issues if i.severity == "error"]
        rec = result.records[0] if result.records else {}

        if errors:
            failed.append(icao)
        if result.warnings:
            all_warnings.extend(f"{icao}: {w}" for w in result.warnings)

        status = "OK" if not errors else f"FAIL: {'; '.join(i.message for i in errors)}"
        eq = str(rec.get("clearing_equipment"))[:20]
        print(f"{icao:6}{eq:>22}  {status}")

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
