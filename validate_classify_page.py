#!/usr/bin/env python3
"""
validate_classify_page.py — full-document proof for classify_page.py.

Runs classify_page() against every page of the AIP using fitz word-position
extraction (the same method the production extractor uses), and reports the
manifest completeness picture: are all 36 standard aerodromes present, are all
4 heliports present, is DNXX correctly isolated, and are there any real gaps
(a page that should be classified as content but isn't) versus benign ones
(genuinely blank verso pages).

This full-scale run was impractical in the development sandbox (some chart
pages have thousands of vector-drawn text fragments, which is slow for
word-clustering regardless of library) — run it here instead, where the
earlier 36-aerodrome AD 2.2/AD 2.12 sweeps already completed quickly.

Usage:
    python validate_classify_page.py Complete_AIP2026.pdf
"""
import sys
import fitz  # PyMuPDF

from classify_page import classify_page
from aip_structure import AERODROMES, STANDARD_36, HELIPORT_ICAO, SPECIMEN_ICAO

# Categories that are legitimately NOT structured-admin content and must never
# be counted as "gaps": chart plates and chart-index pages are owned by
# extract_charts.py; special chart sections (DNKK/GEN) have no admin data model.
_NON_GAP = {"CHART_PLATE", "CHART_INDEX", "SPECIAL_CHART_SECTION",
            "AD_SPECIMEN", "AD3_HELIPORT_PROC"}


def main():
    if len(sys.argv) < 2:
        print("usage: python validate_classify_page.py <AIP.pdf>")
        sys.exit(2)

    doc = fitz.open(sys.argv[1])
    results = []
    for i in range(doc.page_count):
        page = doc.load_page(i)
        raw = page.get_text("words")  # (x0,y0,x1,y1,text,block,line,word)
        words = [(w[0], w[1], w[2], w[3], w[4]) for w in raw if w[4].strip()]
        results.append(classify_page(i + 1, words))
        if (i + 1) % 100 == 0:
            print(f"  ... {i+1}/{doc.page_count} pages classified", file=sys.stderr)

    cats = {}
    for r in results:
        cats[r.category] = cats.get(r.category, 0) + 1

    print("\n=== category counts ===")
    for c, n in sorted(cats.items(), key=lambda kv: -kv[1]):
        print(f"  {c:20} {n}")

    ad_content = [r for r in results if r.category == "AD_CONTENT"]
    ad_icaos = set(r.icao for r in ad_content)
    print(f"\nAD_CONTENT aerodromes: {len(ad_icaos)} (want 36)")
    missing = STANDARD_36 - ad_icaos
    extra = ad_icaos - STANDARD_36
    if missing:
        print(f"  MISSING: {sorted(missing)}")
    if extra:
        print(f"  UNEXPECTED (investigate): {sorted(extra)}")
    if not missing and not extra:
        print("  clean — exactly the 36 expected aerodromes")

    heli_pages = [r for r in results if r.category in ("AD_HELIPORT", "AD3_HELIPORT_PROC")]
    heli_icaos = set(r.icao for r in heli_pages)
    print(f"\nHeliport pages found for: {sorted(heli_icaos)} (want {sorted(HELIPORT_ICAO)})")
    if heli_icaos != HELIPORT_ICAO:
        print(f"  MISMATCH — missing: {HELIPORT_ICAO-heli_icaos}, unexpected: {heli_icaos-HELIPORT_ICAO}")

    specimen_pages = [r for r in results if r.category == "AD_SPECIMEN"]
    print(f"\nAD_SPECIMEN (DNXX) pages: {len(specimen_pages)}")

    blank_pages = [r for r in results if r.category == "BLANK"]
    print(f"\nBLANK pages: {len(blank_pages)} total (verso pages / no extractable text)")

    unknown = [r for r in results if r.category == "UNKNOWN"]
    print(f"\nUNKNOWN pages: {len(unknown)} total (genuinely unclassified — investigate every one)")
    for r in unknown:
        # give context: is this page near/inside a known boundary, or fully
        # outside every AERODROMES range (front-matter zone)?
        nearby = None
        for icao, name, start, end in AERODROMES:
            if start - 2 <= r.page_index <= end + 2:
                where = "inside" if start <= r.page_index <= end else "just outside"
                nearby = f"{where} {icao}'s range ({start}-{end})"
                break
        print(f"    p{r.page_index:5d}  {nearby or '(outside every AERODROMES boundary — front matter zone)'}")

    # The real signal: is any UNKNOWN page sandwiched INSIDE one aerodrome's own
    # page range? BLANK is now its own category (classify_page is the single
    # source of truth for blank-ness — this validator no longer re-derives it),
    # so anything still landing in UNKNOWN here is neither blank, front matter,
    # nor any recognized content/chart type, and deserves a look.
    ad_by_idx = {r.page_index: r for r in results
                 if r.category not in ("UNKNOWN",) and r.icao in STANDARD_36}
    unknown_idx = {r.page_index for r in unknown}
    sorted_ad = sorted(ad_by_idx.keys())
    real_gaps = []
    for a, b in zip(sorted_ad, sorted_ad[1:]):
        if b - a > 1 and ad_by_idx[a].icao == ad_by_idx[b].icao:
            for p in range(a + 1, b):
                if p in unknown_idx:
                    real_gaps.append((ad_by_idx[a].icao, p))

    print(f"\nsandwiched UNKNOWN pages inside an aerodrome's range: {len(real_gaps)}")
    for icao, p in real_gaps:
        print(f"    {icao} p{p}  <-- open this page and check")
    genuine = real_gaps

    ok = not missing and not extra and heli_icaos == HELIPORT_ICAO and not genuine
    print("\n" + "=" * 60)
    print("RESULT: " + ("CLEAN — classifier is complete and correct" if ok
                         else "ISSUES FOUND — see flagged items above"))
    print("=" * 60)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
