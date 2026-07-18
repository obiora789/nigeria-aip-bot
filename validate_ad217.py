#!/usr/bin/env python3
"""
validate_ad217.py — full 36-aerodrome proof for AD217Extractor.

Runs classify_page -> segment_aerodrome -> AD217Extractor for every standard
aerodrome. Checks every field canonicalizes, and — the specific bug this
extractor was built to fix — that no aerodrome's remarks field contains the
ICAO Annex 11 boilerplate note text (confirmed present as raw content on 11
of 36 aerodromes; must be filtered before it ever reaches a stored field).

Usage:
    python validate_ad217.py Complete_AIP2026.pdf
"""
import sys

import fitz  # PyMuPDF

from classify_page import classify_page
from segment_page import segment_aerodrome
from aip_structure import AERODROMES, STANDARD_36
from ad217_extractor import AD217Extractor


def main():
    if len(sys.argv) < 2:
        print("usage: python validate_ad217.py <AIP.pdf>")
        sys.exit(2)

    doc = fitz.open(sys.argv[1])
    extractor = AD217Extractor()

    std_entries = [e for e in AERODROMES if e[0] in STANDARD_36]
    print(f"AD 2.17 extraction across {len(std_entries)} standard aerodromes\n")
    print(f"{'ICAO':6}{'#fields':>9}  remarks")
    print("-" * 90)

    failed = []
    note_leak = []
    all_warnings = []
    for icao, name, start, end in std_entries:
        page_words = {}
        for p in range(start, min(start + 8, end + 1)):
            page = doc.load_page(p - 1)
            raw = page.get_text("words")
            page_words[p] = [(w[0], w[1], w[2], w[3], w[4]) for w in raw if w[4].strip()]

        segments = segment_aerodrome(icao, page_words, classify_page)
        ad217_segs = [s for s in segments if s.subsection == "2.17"]

        result = extractor.extract(icao, ad217_segs)
        issues = extractor.validate(result)
        errors = [i for i in issues if i.severity == "error"]

        if errors:
            failed.append(icao)
        if result.warnings:
            all_warnings.extend(f"{icao}: {w}" for w in result.warnings)

        remarks = next((r["detail"] for r in result.records if r["field"] == "remarks"), None)
        if remarks and ("asterisk" in remarks.lower() or "annex 11" in remarks.lower()):
            note_leak.append(icao)

        n = len([r for r in result.records if r["field"] is not None])
        status = "" if not errors else f"  FAIL: {'; '.join(i.message for i in errors)}"
        print(f"{icao:6}{n:>9}  {str(remarks)[:55]}{status}")

    print("\n" + "=" * 90)
    print(f"clean: {len(std_entries) - len(failed)}/{len(std_entries)}")
    if failed:
        print(f"FAILED: {failed}")
    print(f"boilerplate-note leak into remarks: {note_leak if note_leak else 'NONE'}")
    if all_warnings:
        print(f"\nWarnings:")
        for w in all_warnings:
            print(f"  {w}")
    print("=" * 90)
    sys.exit(1 if failed or note_leak else 0)


if __name__ == "__main__":
    main()
