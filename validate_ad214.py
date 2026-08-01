#!/usr/bin/env python3
"""
validate_ad214.py — full 36-aerodrome proof for AD214Extractor.

Runs classify_page -> segment_aerodrome -> AD214Extractor for every standard
aerodrome. Checks: no spurious runway designations appear (the specific bug
found and fixed while building this — PAPI angle fragments and the column-
number header row were initially misread as runway-end markers), and every
real runway has non-empty text for each end.

Usage:
    python validate_ad214.py Complete_AIP2026.pdf
"""
import re
import sys

import fitz  # PyMuPDF

from classify_page import classify_page
from segment_page import segment_aerodrome
from aip_structure import AERODROMES, STANDARD_36
from ad214_extractor import AD214Extractor


def main():
    if len(sys.argv) < 2:
        print("usage: python validate_ad214.py <AIP.pdf>")
        sys.exit(2)

    doc = fitz.open(sys.argv[1])
    extractor = AD214Extractor()

    std_entries = [e for e in AERODROMES if e[0] in STANDARD_36]
    print(f"AD 2.14 extraction across {len(std_entries)} standard aerodromes\n")
    print(f"{'ICAO':6}{'runways'}")
    print("-" * 80)

    failed = []
    suspicious = []
    all_warnings = []
    header_leaks = []
    for icao, name, start, end in std_entries:
        page_words = {}
        for p in range(start, min(start + 8, end + 1)):
            page = doc.load_page(p - 1)
            raw = page.get_text("words")
            page_words[p] = [(w[0], w[1], w[2], w[3], w[4]) for w in raw if w[4].strip()]

        segments = segment_aerodrome(icao, page_words, classify_page)
        ad214_segs = [s for s in segments if s.subsection == "2.14"]

        result = extractor.extract(icao, ad214_segs)
        issues = extractor.validate(result)
        errors = [i for i in issues if i.severity == "error"]

        if errors:
            failed.append(icao)
        if result.warnings:
            all_warnings.extend(f"{icao}: {w}" for w in result.warnings)

        # GUARD: the AIP reprints the lighting table's column-header row when
        # the table spans a page, and it must never survive into a stored
        # value. It did, in 8 of 36 aerodromes — as the entire value for five
        # of them, and appended to real lighting data for Lagos, DNBB and
        # DNFB, so a pilot read correct PALS/PAPI detail trailing off into
        # column titles. Without this check the extractor could regress to
        # that silently: nothing else here inspects the value text, and the
        # run still reported "clean: 36/36" throughout.
        for r in result.records:
            for endk, val in (r.get("end_detail") or {}).items():
                if re.search(r"\bRWY\s+APCH\s+LGT\b", val or "", re.I):
                    header_leaks.append(
                        f"{icao} [{endk}]: column header in stored value — "
                        f"{(val or '')[:60]!r}")

        rwy_str = "; ".join(r["designation"] or "general_notes" for r in result.records)
        # sanity check: a runway designation number should never exceed 36
        # (max valid runway heading /10) — anything higher signals a
        # spurious match slipped through (e.g. a stray "37"+ from unrelated
        # numeric text)
        for r in result.records:
            if r["designation"] is None:
                continue
            for end in r["designation"].replace("/", " ").split():
                num = int(re.match(r'\d+', end).group())
                if num < 1 or num > 36:
                    suspicious.append(f"{icao}: suspicious designation {r['designation']!r}")

        status = "" if not errors else f"  FAIL: {'; '.join(i.message for i in errors)}"
        print(f"{icao:6}{rwy_str}{status}")

    print("\n" + "=" * 80)
    print(f"clean: {len(std_entries) - len(failed)}/{len(std_entries)}")
    if failed:
        print(f"FAILED: {failed}")
    if suspicious:
        print(f"\nSUSPICIOUS designations (possible false-positive end-markers):")
        for s in suspicious:
            print(f"  {s}")
    if header_leaks:
        print(f"\nHEADER LEAKS ({len(header_leaks)}) — table furniture stored as data:")
        for h in header_leaks:
            print(f"  {h}")
        print("  Fix ad214_extractor._strip_table_furniture() before ingesting.")

    if all_warnings:
        print(f"\nWarnings:")
        for w in all_warnings:
            print(f"  {w}")
    print("=" * 80)
    sys.exit(1 if (failed or header_leaks) else 0)


if __name__ == "__main__":
    main()
