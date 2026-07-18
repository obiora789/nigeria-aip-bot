#!/usr/bin/env python3
"""
validate_ad22.py — full 36-aerodrome proof for AD22Extractor.

Runs classify_page -> segment_aerodrome -> AD22Extractor for every standard
aerodrome. Checks: ARP coordinates present for all 36 (a real, universal
field); elevation present in at least one unit for all 36; and reports the
genuine per-aerodrome nulls (feet-only elevations, missing temperatures) as
information, not failures — the whole point of null-over-guess is that a
None here reflects the source, not a bug.

Usage:
    python validate_ad22.py Complete_AIP2026.pdf
"""
import sys

import fitz  # PyMuPDF

from classify_page import classify_page
from segment_page import segment_aerodrome
from aip_structure import AERODROMES, STANDARD_36
from ad22_extractor import AD22Extractor


def main():
    if len(sys.argv) < 2:
        print("usage: python validate_ad22.py <AIP.pdf>")
        sys.exit(2)

    doc = fitz.open(sys.argv[1])
    extractor = AD22Extractor()

    std_entries = [e for e in AERODROMES if e[0] in STANDARD_36]
    print(f"AD 2.2 extraction across {len(std_entries)} standard aerodromes\n")
    print(f"{'ICAO':6}{'lat':>11}{'lon':>11}{'elev_m':>9}{'elev_ft':>9}{'temp_c':>8}  status")
    print("-" * 80)

    failed = []
    null_notes = []
    for icao, name, start, end in std_entries:
        page_words = {}
        for p in range(start, min(start + 3, end + 1)):
            page = doc.load_page(p - 1)
            raw = page.get_text("words")
            page_words[p] = [(w[0], w[1], w[2], w[3], w[4]) for w in raw if w[4].strip()]

        segments = segment_aerodrome(icao, page_words, classify_page)
        ad22_segs = [s for s in segments if s.subsection == "2.2"]

        result = extractor.extract(icao, ad22_segs)
        issues = extractor.validate(result)
        errors = [i for i in issues if i.severity == "error"]
        rec = result.records[0] if result.records else {}

        if errors:
            failed.append(icao)
        if rec.get("elevation_m") is None:
            null_notes.append(f"{icao}: no metres elevation (feet-only)")
        if rec.get("reference_temp_c") is None:
            null_notes.append(f"{icao}: no reference temperature in source")

        status = "OK" if not errors else f"FAIL: {'; '.join(i.message for i in errors)}"
        print(f"{icao:6}{str(rec.get('arp_lat')):>11}{str(rec.get('arp_lon')):>11}"
              f"{str(rec.get('elevation_m')):>9}{str(rec.get('elevation_ft')):>9}"
              f"{str(rec.get('reference_temp_c')):>8}  {status}")

    print("\n" + "=" * 70)
    print(f"clean: {len(std_entries) - len(failed)}/{len(std_entries)}")
    if failed:
        print(f"FAILED: {failed}")
    if null_notes:
        print(f"\nGenuine source nulls (not failures — confirm these match the PDF):")
        for n in null_notes:
            print(f"  {n}")
    print("=" * 70)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
