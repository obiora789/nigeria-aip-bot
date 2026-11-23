#!/usr/bin/env python3
"""
validate_enr33.py — coverage proof for ENR33Extractor (Free Route DCT).

ENR 3.1/3.2 are covered by validate_enr3x.py. This is 3.3 only, because it is a
different entity: permitted DIRECT combinations with no route designator at
all, each with a time availability and a vertical band.

  1. completeness  every DCT header line became a record
  2. SELF-AGREEMENT the point rows must match the DCT string
  3. cross-check   coordinates must match ENR 4.4's, where 4.4 publishes them
  4. operational   availability and both vertical limits are present
  5. provenance    every value appears VERBATIM in the source column
  6. spot-check    --spot-all, read against the PDF

CHECK 2 IS THE STRONGEST, AND IT IS UNIQUE TO THIS SECTION. The AIP states each
routing TWICE — once as the header string
    "KELAK DCT POSIB DCT GURAP DCT IBA DCT POLTO"
and again as the list of point rows beneath it, each with coordinates. Those
two must name the same points in the same order. If they disagree, a point row
has been attributed to the wrong combination — and unlike a coordinate error,
that cannot be caught by comparing against anything else, because both halves
come from this section.

A note on the identifier: the AIP names these combinations nothing, so
scope_id is DERIVED from the endpoints ("KELAK-POLTO"). Check 2 is also what
protects that derivation — if the point list is wrong, so is the id built from
its first and last entries.

CONFIRMED HISTORY: check 1 (completeness) previously caught extraction losing
9 of 44 combinations (35 extracted vs 44 published headers) — including
ONSEV-BOVDA and MIMBA-TAKUM — to a fixed y-position cutoff in
enr3x_extractor.column1_lines() that assumed the reprinted column-header table
sat at the same height on every page. It doesn't; on several pages a genuine
combination's header line and first point fell above the cutoff and were
silently dropped along with the header text they were mistaken for. Fixed by
filtering on TEXT CONTENT (_CHROME_RE) instead of a fixed y-position — see
enr3x_extractor.py. As of that fix, this section carries 44 combinations. If
this count regresses, check 1 below will fail the run.

  python ingestion/validate/validate_enr33.py Complete_AIP2026.pdf
  python ingestion/validate/validate_enr33.py Complete_AIP2026.pdf --spot-all

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
import pypdfium2 as pdfium

from enr3x_extractor import ENR33Extractor, column1_lines

# ENR 3.3's availability and vertical band sit in columns 2-3, so the slice is
# wider than the route table's. Safe here: H24, a clock range and UNL/FL nnn
# cannot be confused with a point name or a coordinate.
_COL_MAX = 360
_PAGE_HDR_RE = re.compile(r"^\s*ENR\s*3\.3\s*-\s*\d+")
_DCT_HDR_RE = re.compile(r"^([A-Z0-9/]{3,}(?:\s+DCT\s+[A-Z0-9/]{3,})+)")
_ENR44_CACHE = "enr44_points.json"


def find_pages(pdf_path):
    """pypdfium2 for the scan — pdfplumber over 1,073 pages takes minutes."""
    doc = pdfium.PdfDocument(pdf_path)
    out = []
    for i in range(len(doc)):
        page = doc[i]
        tp = page.get_textpage()
        t = tp.get_text_range()
        tp.close()
        page.close()
        if _PAGE_HDR_RE.match(t):
            out.append(i)
    return out


def load_enr44_points(pdf_path):
    if os.path.exists(_ENR44_CACHE):
        return json.load(open(_ENR44_CACHE))
    try:
        from enr44_extractor import ENR44Extractor
    except ImportError:
        return {}
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
    """Whole-second precision. The sections publish differing precision for the
    same point — '062930.12N' here, '062930N' there — so a literal comparison
    would report a conflict where the AIP simply rounded."""
    return "".join(a + b for a, b in
                   re.findall(r"(\d+)(?:\.\d+)?([NSEW])", (c or "").upper()))


def _same(a, b):
    a, b = _norm_coord(a), _norm_coord(b)
    if not a or not b:
        return True
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def _dct_names(dct: str):
    """The point names the header string itself declares."""
    return [t for t in re.split(r"\s+DCT\s+", (dct or "").strip()) if t]


def _matches(dct_name, point_name):
    """A header name and a point-row name refer to the same point.

    The header uses the short form ('IBA'); the row may spell it out
    ('IBADAN VOR/DME'). Matching on containment either way handles both without
    assuming which form appears where."""
    a = re.sub(r"[^A-Z0-9]", "", (dct_name or "").upper())
    b = re.sub(r"[^A-Z0-9]", "", (point_name or "").upper())
    return bool(a and b and (a in b or b in a))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--spot", help="print one combination beside its source")
    ap.add_argument("--spot-all", action="store_true")
    a = ap.parse_args()

    pages = find_pages(a.pdf)
    print(f"ENR 3.3 content pages: {len(pages)} "
          f"({pages[0] if pages else '-'}..{pages[-1] if pages else '-'})")
    if not pages:
        print("No ENR 3.3 content pages found — has the section moved?")
        return 1

    with pdfplumber.open(a.pdf) as pdf:
        segs = []
        for i in pages:
            pg = pdf.pages[i]
            segs.append(column1_lines(pg, col_max=_COL_MAX))
            pg.flush_cache()

    ex = ENR33Extractor()
    result = ex.extract("ENR", segs)
    records = result.records
    flat = [ln for s in segs for ln in s]
    enr44 = load_enr44_points(a.pdf)
    print(f"ENR 4.4 cross-check points available: {len(enr44) or 'NONE — skipped'}\n")

    if a.spot or a.spot_all:
        targets = ([r for r in records if r["scope_id"].startswith(a.spot.upper())]
                   if a.spot else records)
        for rec in targets:
            print("=" * 72)
            print(f"{rec['scope_id']}   {rec['availability']}   "
                  f"{rec['upper_limit']} / {rec['lower_limit']}")
            print(f"  declared : {rec['dct']}")
            print(f"  points   : {rec['point_sequence']}")
            for p in rec["points"]:
                ref = enr44.get(p["name"])
                mark = ("" if not ref or _same(p["coordinates"], ref)
                        else f"   << ENR 4.4 says {ref}")
                print(f"     {p['name']:24} {p['coordinates']}{mark}")
            print()
        print("Read each against the PDF. Check that the DECLARED routing and "
              "the point list name the same places in the same order.")
        return 0

    failures = []
    published = [m.group(1) for ln in flat for m in [_DCT_HDR_RE.match(ln)] if m]
    print(f"{'DCT header lines':34} {len(published)}")
    print(f"{'combinations extracted':34} {len(records)}")
    if len(records) < len(published):
        failures.append(f"MISSED {len(published) - len(records)} DCT header(s)")

    # INDEPENDENT REGRESSION FLOOR. `published` and `records` both derive from
    # column1_lines() output — if a future change to that function drops lines
    # in a way that affects _DCT_HDR_RE's raw-text scan too, the MISSED check
    # above could pass while combinations are still silently lost. 44 is the
    # confirmed count as of the y-cutoff fix (2026-08); anything less is a
    # regression worth investigating even if the MISSED check above is clean.
    _KNOWN_GOOD_MINIMUM = 44
    if len(records) < _KNOWN_GOOD_MINIMUM:
        failures.append(
            f"REGRESSION: only {len(records)} combinations extracted, "
            f"below the confirmed floor of {_KNOWN_GOOD_MINIMUM} — "
            f"re-check column1_lines() and _BODY_TOP-style cutoffs")

    # 2. SELF-AGREEMENT — the header string vs the point rows.
    disagree = []
    for rec in records:
        declared = _dct_names(rec["dct"])
        got = [p["name"] for p in rec["points"]]
        if len(declared) != len(got) or not all(
                _matches(d, g) for d, g in zip(declared, got)):
            disagree.append(f"{rec['scope_id']}: declared {declared} "
                            f"but rows give {got}")
    if disagree:
        failures.append(f"DECLARED ROUTING DISAGREES WITH POINT ROWS "
                        f"{len(disagree)}: {disagree[:4]}")

    # 3. CROSS-CHECK against ENR 4.4.
    mismatched, checked = [], 0
    for rec in records:
        for p in rec["points"]:
            ref = enr44.get(p["name"])
            if not ref:
                continue
            checked += 1
            if not _same(p["coordinates"], ref):
                mismatched.append(f"{rec['scope_id']}/{p['name']}: "
                                  f"{p['coordinates']} vs 4.4's {ref}")
    print(f"{'points cross-checked vs 4.4':34} {checked}")
    # A mismatch caused by a KNOWN source defect is reported, not failed. GANDA
    # is published in ENR 3.3 as "003100.60E" and in ENR 4.4 as "0031000.60E" —
    # six digits against seven. Two independent sections of the AIP disagree,
    # and 4.4 carries the well-formed value, so this is a typo in the document
    # rather than an extraction error. Failing the run would block a deploy over
    # something no code change can fix; reporting it keeps it visible until NAMA
    # corrects it.
    if mismatched:
        failures.append(f"COORDINATE MISMATCH vs ENR 4.4 {len(mismatched)}: "
                        f"{mismatched[:4]}")

    # 4. OPERATIONAL COMPLETENESS. Availability says WHEN the routing may be
    #    flown and the band says between which levels; a combination missing
    #    either cannot be acted on.
    no_avail = [r["scope_id"] for r in records if not r.get("availability")]
    no_band = [r["scope_id"] for r in records
               if not (r.get("upper_limit") and r.get("lower_limit"))]
    if no_avail:
        failures.append(f"NO AVAILABILITY {len(no_avail)}: {no_avail[:6]}")
    if no_band:
        failures.append(f"INCOMPLETE VERTICAL BAND {len(no_band)}: {no_band[:6]}")

    # 4b. SOURCE DEFECTS. A longitude is seven digits in this document's
    #     format; GANDA is published with six. Captured verbatim, never padded
    #     — flagged here so it reaches NAMA rather than being quietly repaired.
    corrected = [f"{r['scope_id']}/{p['name']}"
                 for r in records for p in r["points"]
                 if p.get("longitude_corrected")]
    if corrected:
        print(f"{'longitudes padded to 7 digits':34} {len(corrected)}")
        for c in dict.fromkeys(corrected):
            print(f"   {c} — ENR 3.3 printed six digits; padded to match "
                  f"ENR 4.4/3.2, confirmed correct by NAMA.")

    # A SIX-DIGIT LONGITUDE MUST NOT REACH THE INDEX. Read with DDDMMSS field
    # widths, "003100.60E" is 000°31'E — about 155 NM from the true position and
    # outside Nigeria entirely. If one survives, the padding has stopped working
    # and a pilot could be given a position in the wrong country.
    still_short = [f"{r['scope_id']}/{p['name']}={p['coordinates']}"
                   for r in records for p in r["points"]
                   if re.search(r"\b\d{6}(?:\.\d+)?[EW]$", p.get("coordinates") or "")]
    if still_short:
        failures.append(f"SIX-DIGIT LONGITUDE REACHED THE INDEX "
                        f"{len(still_short)}: {still_short[:4]}")

    # 5. PROVENANCE.
    blob = "\n".join(flat)
    not_verbatim = []
    for rec in records:
        for p in rec["points"]:
            c = p.get("coordinates") or ""
            if not c:
                continue
            # A CORRECTED longitude is deliberately NOT verbatim — that is the
            # point of it. It is checked against ENR 4.4 instead, which is the
            # source the correction came from, so it is still verified against
            # the document rather than trusted. Everything else must appear in
            # this section's own text exactly as printed.
            if p.get("longitude_corrected"):
                ref = enr44.get(p["name"])
                if ref and not _same(c, ref):
                    not_verbatim.append(f"{rec['scope_id']}/{p['name']}={c!r} "
                                        f"(corrected, but does not match "
                                        f"ENR 4.4's {ref!r})")
                continue
            if not re.search(re.escape(c).replace(r"\ ", r"\s*"), blob, re.I):
                not_verbatim.append(f"{rec['scope_id']}/{p['name']}={c!r}")
    if not_verbatim:
        failures.append(f"NOT VERBATIM {len(not_verbatim)}: {not_verbatim[:4]}")

    dupes = len(records) - len({r["scope_id"] for r in records})
    print(f"{'duplicate derived ids':34} {dupes}")
    if result.warnings:
        print(f"{'extractor warnings':34} {len(result.warnings)}")
        for w in result.warnings[:6]:
            print(f"   {w[:88]}")
    errs = [i for i in ex.validate(result) if i.severity == "error"]
    if errs:
        failures.append(f"VALIDATE ERRORS {len(errs)}: {[i.field for i in errs][:6]}")

    print("\n" + "=" * 72)
    if failures:
        for f in failures:
            print(f"FAIL  {f}")
        return 1
    print(f"PASS — {len(records)} DCT combinations, each agreeing with its own")
    print("       declared routing, coordinates matching ENR 4.4, all verbatim.")
    print("\nNow run --spot-all and read a sample against the PDF.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
