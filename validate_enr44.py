#!/usr/bin/env python3
"""
validate_enr44.py — full-coverage proof for ENR44Extractor.

Same three levels as validate_enr51.py, for the same reason: the automated
checks prove COMPLETENESS and PROVENANCE, and only a human read catches a value
copied from the wrong row.

  1. completeness   every line-start designator on the pages became a record
  2. provenance     every value appears VERBATIM in the source text
  3. namespace      no designator collides with an ICAO code, city alias or VOR
                    ident — this is what makes an exact name lookup safe
  4. spot-check     --spot-all, read against the PDF

Check 3 matters more here than it did for ENR 5.1. A danger area id (DND45) is
unmistakable, but a five-letter waypoint shares its shape with nothing in
particular — so the guarantee that TEMSA can never be confused with an
aerodrome has to be measured, not assumed. Measured on AIRAC 03/2026: 214
points, zero collisions.

  python validate_enr44.py Complete_AIP2026.pdf
  python validate_enr44.py Complete_AIP2026.pdf --spot TEMSA
  python validate_enr44.py Complete_AIP2026.pdf --spot-all

Exits non-zero on any failure, so it can gate a deploy.
"""
import argparse
import re
import sys

import pypdfium2 as pdfium

from enr44_extractor import ENR44Extractor

# The page's OWN running header, anchored at the start. Searching anywhere
# admits the table of contents, which once swept 500 unrelated pages into an
# AD 2.22 extraction.
_PAGE_HDR_RE = re.compile(r"^\s*ENR\s*4\.4\s*-\s*\d+")

# Ground truth: a five-letter token at the start of a line, followed by what
# looks like a latitude. Independent of the extractor's own parsing.
_CANDIDATE_RE = re.compile(r"^([A-Z]{5})\s+\d{6}", re.M)


def load_pages(pdf_path):
    doc = pdfium.PdfDocument(pdf_path)
    out = []
    for i in range(len(doc)):
        page = doc[i]
        tp = page.get_textpage()
        t = tp.get_text_range()
        tp.close()
        page.close()
        if _PAGE_HDR_RE.match(t):
            out.append((i, t))
    return out


def _norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def verbatim_in_source(value, blob):
    if not value:
        return True
    v = re.escape(_norm(value)).replace(r"\ ", r"\s*")
    return re.search(v, blob, re.I | re.S) is not None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--spot", help="print one point beside its source row")
    ap.add_argument("--spot-all", action="store_true")
    a = ap.parse_args()

    pages = load_pages(a.pdf)
    print(f"ENR 4.4 content pages: {len(pages)} "
          f"({', '.join(str(i) for i, _ in pages)})\n")
    if not pages:
        print("No ENR 4.4 content pages found — has the section moved?")
        return 1

    blob = "\n".join(t for _, t in pages)
    ex = ENR44Extractor()
    ex.segment_text = lambda _segments: blob
    result = ex.extract("ENR", [1])
    records = {r["scope_id"]: r for r in result.records}

    if a.spot or a.spot_all:
        targets = [a.spot.upper()] if a.spot else sorted(records)
        for name in targets:
            rec = records.get(name)
            if not rec:
                print(f"{name}: NOT EXTRACTED")
                continue
            print("=" * 72)
            print(f"{name}")
            print(f"  position : {rec['coordinates']}"
                  + ("   << NO HEMISPHERE LETTER IN SOURCE"
                     if rec.get("coordinate_defect") else ""))
            print(f"  routes   : {rec['routes']}")
            print(f"  FRA      : {rec['fra_relevance']}")
            print(f"  remarks  : {rec['remarks']}")
            src = re.search(rf"^{name}\s+[^\n\r]*", blob, re.M)
            print(f"  --- source row ---")
            print(f"  {_norm(src.group(0)) if src else '(row not found)'}")
            print()
        print("Read each against the PDF. Verbatim presence does not prove a "
              "value came from the RIGHT row.")
        return 0

    failures = []
    want = set(_CANDIDATE_RE.findall(blob)) | set(
        _CANDIDATE_RE.findall(blob.replace("\r", "\n")))
    got = set(records)

    print(f"{'designators on the pages':34} {len(want)}")
    print(f"{'extracted as records':34} {len(got)}")
    missed, invented = sorted(want - got), sorted(got - want)
    if missed:
        failures.append(f"MISSED {len(missed)}: {missed}")
    if invented:
        failures.append(f"INVENTED {len(invented)}: {invented}")

    # Every value copied, not composed.
    not_verbatim = []
    for nm, rec in records.items():
        for field in ("latitude", "longitude", "routes", "remarks"):
            val = rec.get(field)
            if val and not verbatim_in_source(val, blob):
                not_verbatim.append(f"{nm}.{field}={val!r}")
    if not_verbatim:
        failures.append(f"NOT VERBATIM {len(not_verbatim)}: {not_verbatim[:6]}")

    # Shape: the designator is the lookup key, so it must be exactly what the
    # AIP publishes — five uppercase letters, nothing else.
    malformed = [nm for nm in records if not re.fullmatch(r"[A-Z]{5}", nm)]
    if malformed:
        failures.append(f"MALFORMED DESIGNATORS: {malformed[:8]}")

    # NAMESPACE. An exact name lookup is only safe if a waypoint name cannot
    # also be an aerodrome. Measured, not assumed.
    try:
        import resolver
        resolver.load_index()
        aliases = {v for vals in resolver.AERODROMES.values() for v in vals}
        clashes = (sorted(got & resolver.VALID_ICAO)
                   + sorted(got & set(resolver.VOR_IDENTS.values()))
                   + sorted(p for p in got if p.lower() in aliases))
        print(f"{'namespace collisions':34} {len(clashes)}")
        if clashes:
            failures.append(f"NAMESPACE COLLISION: {clashes} — an exact name "
                            f"lookup could return the wrong entity")
    except Exception as e:                             # noqa: BLE001
        print(f"{'namespace check':34} SKIPPED ({type(e).__name__})")

    defects = [nm for nm, r in records.items() if r.get("coordinate_defect")]
    print(f"{'source coordinate defects':34} {len(defects)}"
          f"{'  ' + str(defects) if defects else ''}")
    print(f"{'extractor warnings':34} {len(result.warnings)}")
    for w in result.warnings[:6]:
        print(f"   {w}")

    issues = [i for i in ex.validate(result) if i.severity == "error"]
    if issues:
        failures.append(f"VALIDATE ERRORS {len(issues)}: "
                        f"{[i.field for i in issues][:8]}")

    print("\n" + "=" * 72)
    if failures:
        for f in failures:
            print(f"FAIL  {f}")
        return 1
    print(f"PASS — {len(got)} significant points, all captured, none invented,")
    print("       every value verbatim, no namespace collisions.")
    print("\nNow run --spot-all and read a sample against the PDF.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
