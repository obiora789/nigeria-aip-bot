#!/usr/bin/env python3
"""
validate_ad21.py — full 36-aerodrome proof for AD21Extractor.

Runs classify_page -> segment_aerodrome -> AD21Extractor for every standard
aerodrome and checks: every aerodrome produces exactly one record, city and
aerodrome_name are both non-null (null-over-guess: a null here means the
parser genuinely couldn't find the data, not a fabricated value), and the
record's icao matches the aerodrome being processed.

Usage:
    python validate_ad21.py Complete_AIP2026.pdf
"""
import sys

import fitz  # PyMuPDF

from classify_page import classify_page
from segment_page import segment_aerodrome
from aip_structure import AERODROMES, STANDARD_36
from ad21_extractor import AD21Extractor


def main():
    if len(sys.argv) < 2:
        print("usage: python validate_ad21.py <AIP.pdf>")
        sys.exit(2)

    doc = fitz.open(sys.argv[1])
    extractor = AD21Extractor()

    std_entries = [e for e in AERODROMES if e[0] in STANDARD_36]
    print(f"AD 2.1 extraction across {len(std_entries)} standard aerodromes\n")
    print(f"{'ICAO':6}{'city':22}{'aerodrome_name':30}{'status'}")
    print("-" * 80)

    failed = []
    for icao, name, start, end in std_entries:
        # AD 2.1 always sits on the aerodrome's first page — but pull the
        # whole segment set via segment_aerodrome for consistency with how
        # every later subsection extractor will be invoked.
        page_words = {}
        for p in range(start, end + 1):
            page = doc.load_page(p - 1)
            raw = page.get_text("words")
            words = [(w[0], w[1], w[2], w[3], w[4]) for w in raw if w[4].strip()]
            page_words[p] = words
            # AD 2.1 is always resolved on/near the first page; stop once we
            # have enough pages to see it close (avoids reading a whole
            # 20-40 page aerodrome range just for its first subsection).
            if p >= start + 2:
                break

        segments = segment_aerodrome(icao, page_words, classify_page)
        ad21_segs = [s for s in segments if s.subsection == "2.1"]

        result = extractor.extract(icao, ad21_segs)
        issues = extractor.validate(result)
        errors = [i for i in issues if i.severity == "error"]

        rec = result.records[0] if result.records else {}
        status = "OK" if not errors else f"FAIL: {'; '.join(i.message for i in errors)}"
        if errors:
            failed.append(icao)
        print(f"{icao:6}{str(rec.get('city')):22}{str(rec.get('aerodrome_name')):30}{status}")

    print("\n" + "=" * 60)
    print(f"clean: {len(std_entries) - len(failed)}/{len(std_entries)}")
    if failed:
        print(f"FAILED: {failed}")
    print("=" * 60)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
