#!/usr/bin/env python3
"""
validate_ad25.py — full 36-aerodrome proof for AD25Extractor.

Runs classify_page -> segment_aerodrome -> AD25Extractor for every standard
aerodrome. Checks every declared facility label canonicalizes to one of the
7 known categories, and — the specific case this extractor's own bug-fix was
built against — that DNMM's cross-page (851-852) subsection comes out in
correct field order with a clean Remarks value.

Usage:
    python validate_ad25.py Complete_AIP2026.pdf
"""
import sys

import fitz  # PyMuPDF

from classify_page import classify_page
from segment_page import segment_aerodrome
from aip_structure import AERODROMES, STANDARD_36
from ad25_extractor import AD25Extractor


def main():
    if len(sys.argv) < 2:
        print("usage: python validate_ad25.py <AIP.pdf>")
        sys.exit(2)

    doc = fitz.open(sys.argv[1])
    extractor = AD25Extractor()

    std_entries = [e for e in AERODROMES if e[0] in STANDARD_36]
    print(f"AD 2.5 extraction across {len(std_entries)} standard aerodromes\n")
    print(f"{'ICAO':6}{'#facilities':>12}  status")
    print("-" * 70)

    failed = []
    all_warnings = []
    dnmm_order = None
    for icao, name, start, end in std_entries:
        page_words = {}
        for p in range(start, min(start + 4, end + 1)):
            page = doc.load_page(p - 1)
            raw = page.get_text("words")
            page_words[p] = [(w[0], w[1], w[2], w[3], w[4]) for w in raw if w[4].strip()]

        segments = segment_aerodrome(icao, page_words, classify_page)
        ad25_segs = [s for s in segments if s.subsection == "2.5"]

        result = extractor.extract(icao, ad25_segs)
        issues = extractor.validate(result)
        errors = [i for i in issues if i.severity == "error"]

        if errors:
            failed.append(icao)
        if result.warnings:
            all_warnings.extend(f"{icao}: {w}" for w in result.warnings)
        if icao == "DNMM":
            dnmm_order = [r["facility"] for r in result.records]
            dnmm_remarks = next((r["detail"] for r in result.records
                                  if r["facility"] == "remarks"), None)

        status = "OK" if not errors else f"FAIL: {'; '.join(i.message for i in errors)}"
        n = len([r for r in result.records if r["facility"] is not None])
        flag = "" if n == 7 else f"  ({n} of 7 — check if genuine)"
        print(f"{icao:6}{n:>12}  {status}{flag}")

    print("\n" + "=" * 70)
    print(f"clean: {len(std_entries) - len(failed)}/{len(std_entries)}")
    if failed:
        print(f"FAILED: {failed}")
    if all_warnings:
        print(f"\nWarnings (review, not necessarily errors):")
        for w in all_warnings:
            print(f"  {w}")

    expected_order = ["hotels", "restaurants", "transportation", "medical_facilities",
                       "bank_post_office", "tourist_office", "remarks"]
    print(f"\nDNMM (the cross-page case this fix was built for):")
    print(f"  field order: {dnmm_order}")
    print(f"  order correct: {dnmm_order == expected_order}")
    print(f"  Remarks value clean (no header bleed): {dnmm_remarks!r}")
    print("=" * 70)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
