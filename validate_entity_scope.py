#!/usr/bin/env python3
"""
validate_entity_scope.py — evidence for entity_scope.py against the REAL AIP,
all 36 aerodromes, per this project's rule that nothing is signed off until it
is validated across every aerodrome.

Two reports:

  1. COVERAGE. Every aerodrome's AD 2.22 must be blocked from free synthesis.
     Because the guard keys on SECTION IDENTITY and not on content, this is
     true by construction -- which is the point. The check is kept anyway, so
     that a future "optimisation" that reintroduces content sniffing fails
     here loudly instead of silently.

  2. WHAT IS ACTUALLY PUBLISHED. A matrix of procedure kinds per aerodrome.
     This is the report that exposed the previous version's blind spot: a
     label-based guard (holding/letdown/missed) called six aerodromes
     "no procedures published" when five of them publish PBN, radar, VFR,
     approach-minima and takeoff-minima content -- and DNIL publishes
     per-runway DEPARTURE procedures where RWY 05 turns RIGHT and RWY 23 turns
     LEFT. Keep reading this matrix after every AIRAC cycle: it is the check
     that catches a content assumption going stale.

WHY pypdfium2 AND NOT pdfplumber
--------------------------------
pdfplumber was killed by the memory limit partway through the 1,073-page
document even with per-page flush_cache(). pypdfium2 reads the same text about
two orders of magnitude faster and within memory. pdfplumber is still the right
tool for word-position work; for a full-document text sweep it is the wrong one.

USAGE
-----
    python validate_entity_scope.py [path/to/Complete_AIP2026.pdf]
Exits non-zero on any coverage miss, so it can gate a deploy.
"""
import json
import os
import re
import sys
from collections import defaultdict

import pypdfium2 as pdfium

import entity_scope

DEFAULT_PDF = "/mnt/user-data/uploads/Complete_AIP2026.pdf"
cp
agent.py / tmp / agent_new.py
cp
schemas.py / tmp / schemas_new.py

git
checkout
agent.py
schemas.py  # only these two go back
python
measure_intent.py
stress_set_clean.csv - -limit
300 - -tag
before

cp / tmp / agent_new.py
agent.py  # restore the new versions
cp / tmp / schemas_new.py
schemas.py
python
measure_intent.py
stress_set_clean.csv - -limit
300 - -tag
after

python
measure_intent.py - -compare
before.json
after.json
CACHE = "ad222_by_icao.json"

# The page's OWN header furniture, matched at the START of the page only.
# This distinction is load-bearing and was got wrong first time round:
# the table of contents (AD 0.6-x) lists entries like
#   "DNAA AD 2.22 - FLIGHT PROCEDURES . . . . . . . AD 2-DNAA-8"
# whose PAGE REFERENCE is textually identical to page furniture. Searching
# anywhere in the page therefore attributed TOC pages to aerodromes and, worse,
# a TOC page mentioning both 2.22 and 2.23 flipped a naive state machine on
# hundreds of pages early -- sweeping AD 2.1-2.21 content into DNMM's "AD 2.22"
# (66,932 chars, of which ~25,000 belonged to other sections).
_HDR_FURNITURE = re.compile(r"^\s*AD\s*2-(DN[A-Z]{2})-\d+", re.I)

# Real section headings only. The negative lookahead rejects a TOC line, which
# always continues into a dot leader.
_S_2_22 = re.compile(r"DN[A-Z]{2}\s+AD\s*2\.22\b(?!\s*[-\u2013]?\s*[A-Z ]*\.\s*\.)", re.I)
_S_2_23 = re.compile(r"DN[A-Z]{2}\s+AD\s*2\.23\b(?!\s*[-\u2013]?\s*[A-Z ]*\.\s*\.)", re.I)


def collect_ad222_by_icao(pdf_path):
    """Per-aerodrome AD 2.22 text, sliced heading-to-heading.

    Slicing happens on the aerodrome's CONCATENATED text, not page by page,
    because one page can hold the tail of AD 2.21, all of AD 2.22 and the head
    of AD 2.23 -- page granularity cannot express that boundary."""
    book = defaultdict(list)
    doc = pdfium.PdfDocument(pdf_path)
    for i in range(len(doc)):
        page = doc[i]
        tp = page.get_textpage()
        t = tp.get_text_range()
        tp.close()
        page.close()
        m = _HDR_FURNITURE.match(t)
        if m:
            book[m.group(1).upper()].append(t)
    out = {}
    for icao, pages in book.items():
        if icao == "DNXX":            # a literal placeholder in the document
            continue
        full = "\n".join(pages)
        a = _S_2_22.search(full)
        if not a:
            out[icao] = ""
            continue
        b = _S_2_23.search(full, a.end())
        out[icao] = full[a.start(): b.start() if b else len(full)]
    return out


class _Chunk:
    """Minimal stand-in for models.AIPResult (only aip_section is read)."""
    def __init__(self, content, aip_section):
        self.content, self.aip_section = content, aip_section


def main():
    pdf_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PDF
    if os.path.exists(CACHE):
        print(f"(using cached extraction {CACHE}; delete it to re-extract)\n")
        data = json.load(open(CACHE))
    else:
        print("Extracting AD 2.22 text per aerodrome from the real AIP...")
        data = collect_ad222_by_icao(pdf_path)
        json.dump(data, open(CACHE, "w"))

    # ---- Report 1: coverage --------------------------------------------
    misses = [ic for ic in sorted(data)
              if not entity_scope.blocks_free_synthesis(
                  [_Chunk(data[ic], "AD 2.22")])]
    print(f"Aerodromes with AD 2.22 text : {len(data)}")
    print(f"Blocked from free synthesis  : {len(data) - len(misses)}")
    print(f"Misses                       : {', '.join(misses) or 'none'}\n")

    # ---- Report 2: what is actually published --------------------------
    kinds = [k for k, _ in entity_scope._PROCEDURE_KINDS]
    print("What each aerodrome actually publishes in AD 2.22:")
    print(f"{'ICAO':6}" + "".join(f"{k[:9]:>10}" for k in kinds))
    print("-" * (6 + 10 * len(kinds)))
    totals = dict.fromkeys(kinds, 0)
    for ic in sorted(data):
        found = set(entity_scope.procedure_kinds(data[ic]))
        for k in found:
            totals[k] += 1
        print(f"{ic:6}" + "".join(
            f"{('Y' if k in found else '.'):>10}" for k in kinds))
    print("-" * (6 + 10 * len(kinds)))
    print(f"{'TOTAL':6}" + "".join(f"{totals[k]:>10}" for k in kinds))
    print("\nA guard keyed on any SUBSET of these columns has a blind spot in")
    print("every other column. entity_scope keys on the SECTION instead, so it")
    print("has no column to miss -- these figures are evidence, not a filter.")

    if misses:
        print(f"\n*** {len(misses)} COVERAGE MISS(ES): {', '.join(misses)}")
        return 1
    print("\nNo misses.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
