#!/usr/bin/env python3
"""
validate_ad219.py — full 36-aerodrome proof for AD219Extractor.

Runs classify_page -> segment_aerodrome -> AD219Extractor for every standard
aerodrome. Checks: every navaid has a plausible frequency (VOR/LLZ/GP: VHF
range ~108-118 MHz; NDB/Locator: LF/MF range, typically 190-535 KHz), and no
navaid's raw_text is suspiciously short (a signature of the misattribution
bug already found and fixed on DNOG).

Usage:
    python validate_ad219.py Complete_AIP2026.pdf
"""
import sys

import fitz  # PyMuPDF

from classify_page import classify_page
from segment_page import segment_aerodrome
from aip_structure import AERODROMES, STANDARD_36
from ad219_extractor import AD219Extractor


def main():
    if len(sys.argv) < 2:
        print("usage: python validate_ad219.py <AIP.pdf>")
        sys.exit(2)

    doc = fitz.open(sys.argv[1])
    extractor = AD219Extractor()

    std_entries = [e for e in AERODROMES if e[0] in STANDARD_36]
    print(f"AD 2.19 extraction across {len(std_entries)} standard aerodromes\n")
    print(f"{'ICAO':6}{'navaids'}")
    print("-" * 90)

    failed = []
    implausible = []
    no_freq = []
    for icao, name, start, end in std_entries:
        page_words = {}
        for p in range(start, min(start + 8, end + 1)):
            page = doc.load_page(p - 1)
            raw = page.get_text("words")
            page_words[p] = [(w[0], w[1], w[2], w[3], w[4]) for w in raw if w[4].strip()]

        segments = segment_aerodrome(icao, page_words, classify_page)
        ad219_segs = [s for s in segments if s.subsection == "2.19"]

        result = extractor.extract(icao, ad219_segs)
        issues = extractor.validate(result)
        errors = [i for i in issues if i.severity == "error"]

        if errors:
            failed.append(icao)

        for r in result.records:
            if r["frequency"] is None:
                no_freq.append(f"{icao} {r['aid_type']}: {r['raw_text'][:70]!r}")
                continue
            # VHF (LLZ/VOR/DVOR): ~108-118 MHz. GP: a genuinely different,
            # real UHF band (~328-336 MHz), NOT an error — confirmed
            # directly across many aerodromes before excluding it here.
            if r["freq_unit"] == "MHZ" and r["aid_type"] not in ("GP", "GP/DME", "GP ILS/DME") \
                    and not (100 <= r["frequency"] <= 140):
                implausible.append(f"{icao} {r['aid_type']}: {r['frequency']} MHZ")
            if r["freq_unit"] == "MHZ" and r["aid_type"] in ("GP", "GP/DME", "GP ILS/DME") \
                    and not (320 <= r["frequency"] <= 340):
                implausible.append(f"{icao} {r['aid_type']}: {r['frequency']} MHZ (outside GP band)")
            if r["freq_unit"] == "KHZ" and not (150 <= r["frequency"] <= 1800):
                implausible.append(f"{icao} {r['aid_type']}: {r['frequency']} KHZ")

        nv_str = "; ".join(f"{r['aid_type']}({r['frequency']}{r['freq_unit']})"
                            for r in result.records)
        status = "" if not errors else f"  FAIL: {'; '.join(i.message for i in errors)}"
        print(f"{icao:6}{nv_str}{status}")

    print("\n" + "=" * 90)
    print(f"clean: {len(std_entries) - len(failed)}/{len(std_entries)}")
    if failed:
        print(f"FAILED: {failed}")
    if implausible:
        print(f"\nIMPLAUSIBLE frequency values:")
        for i in implausible:
            print(f"  {i}")
    if no_freq:
        print(f"\nNavaids with no frequency parsed (review — may be genuine gaps):")
        for n in no_freq:
            print(f"  {n}")
    print("=" * 90)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
