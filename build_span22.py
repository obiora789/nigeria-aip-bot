#!/usr/bin/env python3
"""
build_span22.py — generate _span22.json, the AD 2.22 page-span map.

WHY THIS IS NEEDED
------------------
ad222_extractor.py and validate_ad222.py both READ _span22.json
({icao: {"s22": start_page, "end22": end_page}}), but no script in this
codebase GENERATES it. It was evidently produced once, interactively, and
never saved as a standalone file. This rebuilds it.

WHY THE SPAN CAN'T BE FOUND FROM SEGMENTS ALONE
------------------------------------------------
AD 2.22's own PBN coding-table pages (2.22.7.x) classify as CHART_PLATE, not
AD_CONTENT — their headers are the bare "2.22.7 ..." form with a page-reference
code and no "DNxx AD 2.NN" prefix (see ad222_extractor.py's docstring). That
means segment_aerodrome() never sees those pages, so the span cannot be
derived by asking "which pages did segmentation assign to AD 2.22?" — it has
to be found directly from raw page text, independent of classify_page.

HOW THE BOUNDARY IS DETECTED
-----------------------------
Reuses the EXACT header regexes from ad222_extractor.py — not reimplemented,
imported directly — so this survey and the extractor's own band-slicing can
never disagree about what counts as the start or end of AD 2.22. Per
aerodrome, within its own known page range (from aip_structure.AERODROMES):
  s22   = first page whose text matches _H22_RE ("AD 2.22 FLIGHT")
  end22 = the page whose text matches _H23_RE ("AD 2.23 ADDITIONAL") or
          _HC_RE ("CHARTS RELATED TO AN...") — INCLUSIVE, not end22-1, because
          band_2_22() does the within-page slicing itself (AD 2.22 content on
          that same page, above the 2.23/charts heading, is still real 2.22
          content and must be in the loaded span for the extractor to see it).
If no 2.23/charts heading is found before the aerodrome's own range ends,
end22 falls back to the aerodrome's own last page (rare; flagged in output).

Usage:
    python build_span22.py Complete_AIP2026.pdf
    python build_span22.py Complete_AIP2026.pdf DNKS DNAA   # subset, for spot-checks
"""
import json
import os
import sys

import fitz  # PyMuPDF

from aip_structure import AERODROMES, STANDARD_36
from ad222_extractor import _H22_RE, _H23_RE, _HC_RE

OUT_PATH = "_span22.json"


def find_span(doc, start, end):
    """Scan one aerodrome's known page range for the 2.22 start header and the
    2.23/charts end header. Returns (s22, end22, note) — note is None on a
    clean find, else a short flag string for manual review."""
    s22 = None
    end22 = None
    for p in range(start, end + 1):
        text = doc.load_page(p - 1).get_text("text")
        if s22 is None and _H22_RE.search(text):
            s22 = p
            continue
        if s22 is not None and (_H23_RE.search(text) or _HC_RE.search(text)):
            end22 = p
            break

    if s22 is None:
        return None, None, "no AD 2.22 header found in range — genuinely absent " \
                            "or a formatting variant (check by hand)"
    if end22 is None:
        return s22, end, "no AD 2.23/charts heading found after s22 — end22 " \
                          "fell back to the aerodrome's own last page; verify " \
                          "this aerodrome's AD 2.22 doesn't run into the next " \
                          "aerodrome's pages"
    return s22, end22, None


def main():
    if len(sys.argv) < 2:
        print("usage: python build_span22.py <AIP.pdf> [ICAO ...]")
        sys.exit(2)
    pdf_path = sys.argv[1]
    only = set(a.upper() for a in sys.argv[2:]) or None

    existing = json.load(open(OUT_PATH)) if os.path.exists(OUT_PATH) else {}

    entries = [e for e in AERODROMES if e[0] in STANDARD_36
               and (only is None or e[0] in only)]

    doc = fitz.open(pdf_path)
    flagged = []
    for icao, name, start, end in entries:
        s22, end22, note = find_span(doc, start, end)
        if s22 is None:
            flagged.append(f"{icao}: {note}")
            continue
        existing[icao] = {"s22": s22, "end22": end22}
        tag = "OK" if note is None else "FLAGGED"
        print(f"  {tag:8} {icao:6} s22={s22:<5} end22={end22:<5}"
              + (f"  — {note}" if note else ""))
        if note:
            flagged.append(f"{icao}: {note}")

    json.dump(existing, open(OUT_PATH, "w"), indent=2)
    print(f"\nWrote {OUT_PATH} — {len(existing)} aerodrome(s) total "
          f"({len(entries)} processed this run).")
    if flagged:
        print(f"\n{len(flagged)} flagged for manual review before trusting the span:")
        for f in flagged:
            print(f"  {f}")
    print("\nNext: spot-check a few spans against the real PDF, then run:")
    print("  python validate_ad222.py Complete_AIP2026.pdf")
    print("to confirm no word loss before wiring this into vectorise_aip_v3.py.")


if __name__ == "__main__":
    main()
