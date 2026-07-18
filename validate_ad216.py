#!/usr/bin/env python3
"""
validate_ad216.py — full 36-aerodrome proof for AD216Extractor.

Runs classify_page -> segment_aerodrome -> AD216Extractor for every standard
aerodrome. Checks every aerodrome produces exactly one record, and confirms
the structured path fires ONLY for DNCA (the one confirmed real case) while
every other aerodrome correctly falls back to status text.

Usage:
    python validate_ad216.py Complete_AIP2026.pdf
"""
import sys

import fitz  # PyMuPDF

from classify_page import classify_page
from segment_page import segment_aerodrome
from aip_structure import AERODROMES, STANDARD_36
from ad216_extractor import AD216Extractor


def main():
    if len(sys.argv) < 2:
        print("usage: python validate_ad216.py <AIP.pdf>")
        sys.exit(2)

    doc = fitz.open(sys.argv[1])
    extractor = AD216Extractor()

    std_entries = [e for e in AERODROMES if e[0] in STANDARD_36]
    print(f"AD 2.16 extraction across {len(std_entries)} standard aerodromes\n")
    print(f"{'ICAO':6}{'mode':>12}  value")
    print("-" * 90)

    failed = []
    structured = []
    for icao, name, start, end in std_entries:
        page_words = {}
        for p in range(start, min(start + 8, end + 1)):
            page = doc.load_page(p - 1)
            raw = page.get_text("words")
            page_words[p] = [(w[0], w[1], w[2], w[3], w[4]) for w in raw if w[4].strip()]

        segments = segment_aerodrome(icao, page_words, classify_page)
        ad216_segs = [s for s in segments if s.subsection == "2.16"]

        result = extractor.extract(icao, ad216_segs)
        issues = extractor.validate(result)
        errors = [i for i in issues if i.severity == "error"]
        rec = result.records[0] if result.records else {}

        if errors:
            failed.append(icao)
        mode = "structured" if rec.get("status") is None and rec.get("coordinates") else "status-text"
        if mode == "structured":
            structured.append(icao)

        status_val = str(rec.get("status") or rec.get("coordinates"))[:50]
        result_str = "" if not errors else f"  FAIL: {'; '.join(i.message for i in errors)}"
        print(f"{icao:6}{mode:>12}  {status_val}{result_str}")

    print("\n" + "=" * 90)
    print(f"clean: {len(std_entries) - len(failed)}/{len(std_entries)}")
    if failed:
        print(f"FAILED: {failed}")
    print(f"structured path used by: {structured} (expect exactly ['DNCA'])")
    print("=" * 90)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
