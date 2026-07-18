#!/usr/bin/env python3
"""
validate_ad213.py — full 36-aerodrome proof for AD213Extractor.

Runs classify_page -> segment_aerodrome -> AD213Extractor for every standard
aerodrome. Checks every runway has AT LEAST ONE declared distance populated
(a record with zero fields at all is a genuine extraction failure). Null-
over-guess now applies PER FIELD, not all-or-nothing per runway — confirmed
necessary directly on DNKT, whose real source genuinely omits TORA while
publishing TODA/ASDA/LDA; the prior all-or-nothing version discarded all
three real values just because one was missing.

Usage:
    python validate_ad213.py Complete_AIP2026.pdf
"""
import sys

import fitz  # PyMuPDF

from classify_page import classify_page
from segment_page import segment_aerodrome
from aip_structure import AERODROMES, STANDARD_36
from ad213_extractor import AD213Extractor


def main():
    if len(sys.argv) < 2:
        print("usage: python validate_ad213.py <AIP.pdf>")
        sys.exit(2)

    doc = fitz.open(sys.argv[1])
    extractor = AD213Extractor()

    std_entries = [e for e in AERODROMES if e[0] in STANDARD_36]
    print(f"AD 2.13 extraction across {len(std_entries)} standard aerodromes\n")
    print(f"{'ICAO':6}{'runways'}")
    print("-" * 90)

    failed = []
    all_warnings = []
    for icao, name, start, end in std_entries:
        page_words = {}
        # wide lookahead — AD 2.13 confirmed to sit up to 5 pages into a
        # busy aerodrome's range (DNMM/Lagos)
        for p in range(start, min(start + 8, end + 1)):
            page = doc.load_page(p - 1)
            raw = page.get_text("words")
            page_words[p] = [(w[0], w[1], w[2], w[3], w[4]) for w in raw if w[4].strip()]

        segments = segment_aerodrome(icao, page_words, classify_page)
        ad213_segs = [s for s in segments if s.subsection == "2.13"]

        result = extractor.extract(icao, ad213_segs)
        issues = extractor.validate(result)
        errors = [i for i in issues if i.severity == "error"]

        if errors:
            failed.append(icao)
        if result.warnings:
            all_warnings.extend(f"{icao}: {w}" for w in result.warnings)

        rwy_str = "; ".join(
            f"RWY{r['runway']}({r['tora_m']}/{r['toda_m']}/{r['asda_m']}/{r['lda_m']})"
            for r in result.records)
        status = "" if not errors else f"  FAIL: {'; '.join(i.message for i in errors)}"
        print(f"{icao:6}{rwy_str}{status}")

    print("\n" + "=" * 90)
    print(f"clean: {len(std_entries) - len(failed)}/{len(std_entries)}")
    if failed:
        print(f"FAILED: {failed}")
    if all_warnings:
        print(f"\nWarnings:")
        for w in all_warnings:
            print(f"  {w}")
    print("=" * 90)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
