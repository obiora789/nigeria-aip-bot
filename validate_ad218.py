#!/usr/bin/env python3
"""
validate_ad218.py — full 36-aerodrome proof for AD218Extractor.

Runs classify_page -> segment_aerodrome -> AD218Extractor for every standard
aerodrome. Checks: every ATS service has at least one parsed frequency
(null-over-guess is a hard requirement here — a comms service with zero
frequencies is a genuine extraction failure, not a valid gap), and every
frequency value falls in a plausible VHF/HF aeronautical range as a sanity
check against a repeat of the "8903KHz misparsed as 903.0 MHz" bug this
extractor was built to fix.

Usage:
    python validate_ad218.py Complete_AIP2026.pdf
"""
import sys

import fitz  # PyMuPDF

from classify_page import classify_page
from segment_page import segment_aerodrome
from aip_structure import AERODROMES, STANDARD_36
from ad218_extractor import AD218Extractor


def main():
    if len(sys.argv) < 2:
        print("usage: python validate_ad218.py <AIP.pdf>")
        sys.exit(2)

    doc = fitz.open(sys.argv[1])
    extractor = AD218Extractor()

    std_entries = [e for e in AERODROMES if e[0] in STANDARD_36]
    print(f"AD 2.18 extraction across {len(std_entries)} standard aerodromes\n")
    print(f"{'ICAO':6}{'services'}")
    print("-" * 90)

    failed = []
    implausible = []
    all_warnings = []
    for icao, name, start, end in std_entries:
        page_words = {}
        for p in range(start, min(start + 8, end + 1)):
            page = doc.load_page(p - 1)
            raw = page.get_text("words")
            page_words[p] = [(w[0], w[1], w[2], w[3], w[4]) for w in raw if w[4].strip()]

        segments = segment_aerodrome(icao, page_words, classify_page)
        ad218_segs = [s for s in segments if s.subsection == "2.18"]

        result = extractor.extract(icao, ad218_segs)
        issues = extractor.validate(result)
        errors = [i for i in issues if i.severity == "error"]

        if errors:
            failed.append(icao)
        if result.warnings:
            all_warnings.extend(f"{icao}: {w}" for w in result.warnings)

        for r in result.records:
            for f in r["frequencies"]:
                # VHF aeronautical comms: ~118-137 MHz. SAR is deliberately
                # excluded — confirmed real: 406 MHz is the genuine
                # international COSPAS-SARSAT emergency beacon frequency,
                # correctly outside the VHF voice band, not a parsing error.
                if r["service"] == "SAR":
                    continue
                if f["unit"] == "MHZ" and not (100 <= f["value"] <= 140):
                    implausible.append(f"{icao} {r['service']}: {f['value']} {f['unit']}")

        svc_str = "; ".join(
            f"{r['service']}({len(r['frequencies'])}freq)" for r in result.records)
        status = "" if not errors else f"  FAIL: {'; '.join(i.message for i in errors)}"
        print(f"{icao:6}{svc_str}{status}")

    print("\n" + "=" * 90)
    print(f"clean: {len(std_entries) - len(failed)}/{len(std_entries)}")
    if failed:
        print(f"FAILED: {failed}")
    if implausible:
        print(f"\nIMPLAUSIBLE frequency values (check for a parsing error):")
        for i in implausible:
            print(f"  {i}")
    if all_warnings:
        print(f"\nWarnings:")
        for w in all_warnings:
            print(f"  {w}")
    print("=" * 90)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
