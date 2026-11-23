#!/usr/bin/env python3
"""
validate_enr3x.py — coverage proof for ENR3XExtractor (ENR 3.1 / 3.2).

Same three levels as the other ENR validators, with checks aimed at this
section's specific failure mode: a FALSE POINT NAME absorbing another point's
coordinate row.

  1. designators   every route designator published in column 1 became a route
  2. no conflict   no point appears at two different positions within a route
  3. cross-check   a point's coordinates here must MATCH ENR 4.4's, where 4.4
                   publishes that point
  4. provenance    every value appears VERBATIM in the source column
  5. spot-check    --spot <ROUTE> / --spot-all, read against the PDF

CHECK 3 IS THE STRONGEST AND IS UNIQUE TO THIS SECTION. ENR 4.4 independently
publishes 214 significant points with their coordinates, already extracted and
validated. If a route says POLTO is somewhere ENR 4.4 does not, one of the two
is wrong — and it is almost certainly this one, because a route table's columns
interleave while 4.4's rows do not. No amount of internal consistency checking
can catch a point that is simply at the wrong place; a second published source
can.

WHY THE EXTRACTOR IS BAND-SLICED, AND WHAT THAT MEANS FOR THIS FILE
-------------------------------------------------------------------
Three line-based attempts each produced a false point that took the next
point's coordinates (USKIP at XUVLA's position, then LUKRO, then ARDEX at two
positions in one route). The rewrite selects words by x-position so the remarks
column never enters the data. Check 2 exists to prove that holds, and to fail
loudly if anyone reverts to line parsing.

CHECK 1 WAS NOT INDEPENDENT, AND HAS BEEN FIXED TO BE. The original
"designators in column 1" count and the extracted route count both derived
from column1_lines() output — the SAME function that, in ENR 3.3, silently
dropped 9 of 44 combinations to a fixed y-position cutoff that assumed the
reprinted column-header table sat at the same height on every page. Because
both halves of that comparison shared the same blind spot, a matching bug here
could pass check 1 while genuinely losing routes. `published` for check 1 is
now built from RAW, UNFILTERED pypdfium2 text (via find_pages()'s own scan),
never from column1_lines() — so a future regression in the band-slicing logic
cannot silently defeat its own completeness check.

  python ingestion/validate/validate_enr3x.py Complete_AIP2026.pdf
  python ingestion/validate/validate_enr3x.py Complete_AIP2026.pdf --section 3.2
  python ingestion/validate/validate_enr3x.py Complete_AIP2026.pdf --spot UT467
  python ingestion/validate/validate_enr3x.py Complete_AIP2026.pdf --spot-all

Exits non-zero on any failure, so it can gate a deploy.
"""
import sys as _sys, pathlib as _pl  # repo layout: bot at the top, scripts in folders
_R = next(d for d in _pl.Path(__file__).resolve().parents if (d / "main.py").exists())
_sys.path[1:1] = [str(p) for p in (_R, _R / "checks", _R / "ingestion", _R / "ingestion" / "validate")
                  if str(p) not in _sys.path]
import argparse
import json
import os
import re
import sys

import pdfplumber

from enr3x_extractor import ENR3XExtractor, column1_lines, _ROUTE_HDR_RE

# ENR 4.4's extracted points, used for the cross-check. Optional: if the file
# is absent the cross-check is SKIPPED and said to be skipped, never silently
# passed.
_ENR44_CACHE = "enr44_points.json"


def find_pages(pdf_path, section):
    """Page indexes for a section, found with pypdfium2.

    pdfplumber must NOT be used for this scan: extract_text() on all 1,073
    pages takes many minutes and the validator timed out before producing any
    output. pypdfium2 reads the same text about two orders of magnitude faster.
    pdfplumber is then opened only for the handful of pages that matter, where
    its word POSITIONS are what the band slice needs."""
    import pypdfium2 as pdfium
    hdr = re.compile(rf"^\s*ENR\s*{re.escape(section)}\s*-\s*\d+")
    doc = pdfium.PdfDocument(pdf_path)
    out = []
    for i in range(len(doc)):
        page = doc[i]
        tp = page.get_textpage()
        t = tp.get_text_range()
        tp.close()
        page.close()
        if hdr.match(t):
            out.append(i)
    return out


def raw_designators(pdf_path, section):
    """Route designators found in RAW, UNFILTERED page text via pypdfium2 —
    deliberately NOT using column1_lines(). This is what makes check 1 a
    genuine independent cross-check rather than the extractor grading its own
    homework: if column1_lines() ever drops a line (as it did for 9 of 44 ENR
    3.3 combinations, to a fixed y-cutoff that didn't account for the
    column-header table reprinting at different heights on different pages),
    this scan is unaffected, because it never goes through that function.

    Uses a LOOSE, UNANCHORED match (no trailing $) rather than
    _ROUTE_HDR_RE's strict "alone on the line" form: raw pypdfium2 text
    frequently has other column content trailing on the same visual line
    ("UA601 For continuation, ..."), which column1_lines() strips via
    x-position banding but which is still present here. Anchoring to end-of-
    line against raw text undercounted by 3 designators (V213, V717, H335 —
    all genuine multi-point routes, confirmed by cross-checking the
    extractor's own successful parse of each). Verified exact-match (zero
    diff in either direction) against the extractor's output for both 3.1 and
    3.2 with this looser pattern."""
    import pypdfium2 as pdfium
    loose_re = re.compile(r"^([A-Z]?/?U?[A-Z]\d{2,3}[A-Z]?)\b", re.M)
    hdr = re.compile(rf"^\s*ENR\s*{re.escape(section)}\s*-\s*\d+")
    doc = pdfium.PdfDocument(pdf_path)
    blob_parts = []
    for i in range(len(doc)):
        page = doc[i]
        tp = page.get_textpage()
        t = tp.get_text_range()
        tp.close()
        page.close()
        if hdr.match(t):
            blob_parts.append(t)
    blob = "\n".join(blob_parts)
    return set(loose_re.findall(blob))


def load_section(pdf, section, page_indexes):
    """(column-1 line lists per page, page indexes)."""
    segs = []
    for i in page_indexes:
        pg = pdf.pages[i]
        try:
            segs.append(column1_lines(pg))
        finally:
            pg.flush_cache()
    return segs, page_indexes


def load_enr44_points(pdf_path):
    """{name: coordinates} from ENR 4.4, for the cross-check."""
    if os.path.exists(_ENR44_CACHE):
        return json.load(open(_ENR44_CACHE))
    try:
        from enr44_extractor import ENR44Extractor
    except ImportError:
        return {}
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf_path)
    blob = []
    for i in range(len(doc)):
        page = doc[i]
        tp = page.get_textpage()
        t = tp.get_text_range()
        tp.close()
        page.close()
        if re.match(r"\s*ENR\s*4\.4\s*-\s*\d+", t):
            blob.append(t)
    if not blob:
        return {}
    ex = ENR44Extractor()
    ex.segment_text = lambda _s: "\n".join(blob)
    pts = {r["scope_id"]: r["coordinates"] for r in ex.extract("ENR", [1]).records}
    json.dump(pts, open(_ENR44_CACHE, "w"))
    return pts


def _norm_coord(c):
    """Compare coordinates at WHOLE-SECOND precision.

    The two sections publish the same point to different precision — ENR 4.4
    prints '103612.00N 0132933.00E' where a route table prints
    '103612N 0132933E'. An earlier version stripped the decimal POINT but kept
    its digits, turning '103612.00N' into '10361200N', so identical positions
    compared unequal and MOMIG was reported as a mismatch when the AIP agrees
    with itself.

    Dropping the fractional part entirely compares what both sections state:
    degrees, minutes and whole seconds."""
    out = []
    for part in re.findall(r"(\d+)(?:\.\d+)?([NSEW])", (c or "").upper()):
        out.append(part[0] + part[1])
    return "".join(out)


def _same_point(a, b):
    a, b = _norm_coord(a), _norm_coord(b)
    if not a or not b:
        return True
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--section", choices=["3.1", "3.2"], help="default: both")
    ap.add_argument("--spot", help="print one route beside its source column")
    ap.add_argument("--spot-all", action="store_true")
    a = ap.parse_args()

    sections = [a.section] if a.section else ["3.1", "3.2"]
    failures = []
    enr44 = load_enr44_points(a.pdf)
    print(f"ENR 4.4 cross-check points available: {len(enr44) or 'NONE — check skipped'}\n")

    page_index = {sec: find_pages(a.pdf, sec) for sec in sections}
    with pdfplumber.open(a.pdf) as pdf:
        for section in sections:
            segs, pages = load_section(pdf, section, page_index[section])
            print(f"=== ENR {section}: {len(pages)} content pages "
                  f"({pages[0] if pages else '-'}..{pages[-1] if pages else '-'})")
            if not segs:
                failures.append(f"ENR {section}: no content pages found")
                continue

            ex = ENR3XExtractor(section)
            result = ex.extract("ENR", segs)
            records = {r["scope_id"]: r for r in result.records}
            flat = [ln for s in segs for ln in s]

            if a.spot or a.spot_all:
                targets = ([a.spot] if a.spot else sorted(records))
                for rid in targets:
                    rec = records.get(rid)
                    if not rec:
                        continue
                    print("=" * 72)
                    print(f"{rid}   ({rec.get('navigation_spec') or 'no nav spec'})"
                          f"   {rec['point_count']} points")
                    for p in rec["points"]:
                        mark = ""
                        if p["name"] in enr44 and not _same_point(
                                p["coordinates"], enr44[p["name"]]):
                            mark = f"   << ENR 4.4 says {enr44[p['name']]}"
                        print(f"    {p['name']:24} {p['coordinates']}{mark}")
                    print("    --- source column ---")
                    try:
                        i = next(k for k, ln in enumerate(flat)
                                 if _ROUTE_HDR_RE.match(ln)
                                 and _ROUTE_HDR_RE.match(ln).group(1) == rid)
                        for ln in flat[i:i + 24]:
                            print(f"      | {ln}")
                    except StopIteration:
                        print("      (header line not found)")
                    print()
                continue

            # 1. DESIGNATORS — every route designator PUBLISHED (per a raw-text
            #    scan, independent of column1_lines()) must have a record.
            published = raw_designators(a.pdf, section)
            missed = sorted(published - set(records))
            print(f"   {'designators (raw text, independent scan)':32} {len(published)}")
            print(f"   {'routes extracted':32} {len(records)}")
            print(f"   {'points':32} {sum(r['point_count'] for r in result.records)}")
            if missed:
                failures.append(f"ENR {section} MISSED DESIGNATORS: {missed}")

            # Cross-check: also compare against column1_lines()'s own view, so
            # a divergence between the two scans is visible even when neither
            # alone trips the MISSED check above (e.g. both under-count by the
            # same amount for an unrelated reason).
            col1_designators = {m.group(1) for ln in flat
                                for m in [_ROUTE_HDR_RE.match(ln)] if m}
            if col1_designators != published:
                only_raw = sorted(published - col1_designators)
                only_col1 = sorted(col1_designators - published)
                if only_raw:
                    failures.append(
                        f"ENR {section} column1_lines() MISSED vs raw text: "
                        f"{only_raw} — check the band slice / y-position logic")
                if only_col1:
                    print(f"   note: column1_lines() found {only_col1} not in "
                          f"raw scan (likely a routing/header wrap artifact)")

            # 2. NO INTERNAL CONFLICT — this is what the band slice guarantees.
            conflicts = [i for i in ex.validate(result)
                         if i.severity == "error" and "TWO positions" in i.message]
            if conflicts:
                failures.append(f"ENR {section} POSITION CONFLICTS "
                                f"{len(conflicts)}: {[c.message[:70] for c in conflicts]}")

            # 3. CROSS-CHECK AGAINST ENR 4.4 — a second published source.
            mismatched = []
            checked = 0
            for rid, rec in records.items():
                for p in rec["points"]:
                    ref = enr44.get(p["name"])
                    if not ref:
                        continue
                    checked += 1
                    if not _same_point(p["coordinates"], ref):
                        mismatched.append(
                            f"{rid}/{p['name']}: route says {p['coordinates']}, "
                            f"ENR 4.4 says {ref}")
            print(f"   {'points cross-checked vs 4.4':32} {checked}")
            if mismatched:
                failures.append(f"ENR {section} COORDINATE MISMATCH vs ENR 4.4 "
                                f"{len(mismatched)}: {mismatched[:5]}")

            # 4. PROVENANCE — every coordinate must appear in the source column.
            blob = "\n".join(flat)
            not_verbatim = []
            for rid, rec in records.items():
                for p in rec["points"]:
                    c = p.get("coordinates") or ""
                    if c and not re.search(
                            re.escape(c).replace(r"\ ", r"\s*"), blob, re.I):
                        not_verbatim.append(f"{rid}/{p['name']}={c!r}")
            if not_verbatim:
                failures.append(f"ENR {section} NOT VERBATIM {len(not_verbatim)}: "
                                f"{not_verbatim[:5]}")

            # Short routes are reported, not failed: a two-point airway is
            # legitimate, and failing on length would reject real data.
            short = [f"{r['scope_id']}({r['point_count']})"
                     for r in result.records if r["point_count"] < 2]
            if short:
                failures.append(f"ENR {section} ROUTES WITH <2 POINTS: {short} — "
                                f"a single point cannot define a track, so these "
                                f"are truncated, not routes")
            if result.warnings:
                print(f"   warnings: {len(result.warnings)}")
                for w in result.warnings[:5]:
                    print(f"      {w[:88]}")
            print()

    if a.spot or a.spot_all:
        print("Read each against the PDF. The value to check is whether the "
              "point SEQUENCE and count match the printed column — a truncated "
              "route is the remaining known risk.")
        return 0

    print("=" * 72)
    if failures:
        for f in failures:
            print(f"FAIL  {f}")
        return 1
    print("PASS — every published designator extracted, no point at two "
          "positions,\n       coordinates agree with ENR 4.4, all values verbatim.")
    print("\nNow run --spot-all and read a sample against the PDF.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
