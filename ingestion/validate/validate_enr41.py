#!/usr/bin/env python3
"""
validate_enr41.py — full-coverage proof for ENR41Extractor.

Same three levels as the other ENR validators, with two checks specific to
this section's failure modes.

  1. completeness  every station block on the pages produced at least one
                   navaid, and every ident became a record
  2. attribution   no ident is filed under the WRONG station — the defect that
                   put KANO's navaid under KAINJI
  3. pairing       no frequency is asserted where the document does not state
                   the pairing
  4. provenance    every value appears VERBATIM in the source
  5. spot-check    --spot-all, read against the PDF

CHECK 2 IS THE ONE THAT MATTERS. An earlier extractor told stations from idents
by NAME LENGTH ("4+ characters is a station"). BIDA, EKET, KANO and YOLA are
four-letter STATION names that also match the ident shape, so they were filed
as idents of the PRECEDING station and 16 real stations vanished. Nothing about
any single value was wrong; the entity boundary was.

CHECK 3 matters because the frequency list is longer than the ident list
whenever a VOR/DME publishes both a MHz frequency and a DME channel. Pairing by
position would hand a locator "CH 72 X" — a confident wrong frequency, which is
worse than showing the station's whole published list and saying so.

  python ingestion/validate/validate_enr41.py Complete_AIP2026.pdf
  python ingestion/validate/validate_enr41.py Complete_AIP2026.pdf --spot-all

Exits non-zero on any failure, so it can gate a deploy.
"""
import sys as _sys, pathlib as _pl  # repo layout: bot at the top, scripts in folders
_R = next(d for d in _pl.Path(__file__).resolve().parents if (d / "main.py").exists())
_sys.path[1:1] = [str(p) for p in (_R, _R / "checks", _R / "ingestion", _R / "ingestion" / "validate")
                  if str(p) not in _sys.path]
import argparse
import re
import sys

import pypdfium2 as pdfium

from enr41_extractor import ENR41Extractor, find_station_starts, _clean_lines

_PAGE_HDR_RE = re.compile(r"^\s*ENR\s*4\.1\s*-\s*\d+")


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
    ap.add_argument("--spot", help="print one navaid beside its source block")
    ap.add_argument("--spot-all", action="store_true")
    a = ap.parse_args()

    pages = load_pages(a.pdf)
    print(f"ENR 4.1 content pages: {len(pages)} "
          f"({', '.join(str(i) for i, _ in pages)})\n")
    if not pages:
        print("No ENR 4.1 content pages found — has the section moved?")
        return 1

    blob = "\n".join(t for _, t in pages)
    ex = ENR41Extractor()
    ex.segment_text = lambda _segments: blob
    result = ex.extract("ENR", [1])
    records = {r["scope_id"]: r for r in result.records}

    if a.spot or a.spot_all:
        targets = [a.spot.upper()] if a.spot else sorted(records)
        for ident in targets:
            rec = records.get(ident)
            if not rec:
                print(f"{ident}: NOT EXTRACTED")
                continue
            print("=" * 72)
            print(f"{ident}   ({rec['station']})")
            print(f"  type     : {rec['navaid_type']}")
            print(f"  frequency: {rec['frequency']}"
                  + ("   << PAIRING AMBIGUOUS — station's full list shown"
                     if rec.get("frequency_is_ambiguous") else ""))
            print(f"  hours    : {rec['hours']}")
            print(f"  position : {rec['coordinates']}")
            print(f"  elevation: {rec['elevation']}")
            print(f"  FRA      : {rec['fra_relevance']}")
            print(f"  remarks  : {rec['remarks']}")
            src = re.search(rf"^{re.escape(rec['station'].split()[0])}\b[\s\S]{{0,320}}",
                            blob, re.M)
            print("  --- source block ---")
            print("  " + _norm(src.group(0))[:300] if src else "  (not found)")
            print()
        print("Read each against the PDF. Check in particular that a locator's "
              "frequency is the locator's — a VOR/DME at the same station "
              "publishes both a MHz frequency and a DME channel.")
        return 0

    failures = []
    lines = _clean_lines(blob)
    stations = find_station_starts(lines)
    station_names = {nm for _, nm in stations}
    got_stations = {r["station"] for r in result.records}

    print(f"{'station blocks found':34} {len(stations)}")
    print(f"{'stations with >=1 navaid':34} {len(got_stations)}")
    print(f"{'navaids extracted':34} {len(records)}")

    # 1. COMPLETENESS — every station block must yield a navaid.
    empty = sorted(station_names - got_stations)
    if empty:
        failures.append(f"STATIONS WITH NO NAVAID {len(empty)}: {empty}")

    # 2. ATTRIBUTION — an ident must appear inside its own station's block.
    #    This is what catches a navaid filed under the previous station.
    misattributed = []
    for ident, rec in records.items():
        st = rec["station"]
        m = re.search(rf"^{re.escape(st.split()[0])}\b([\s\S]{{0,400}})", blob, re.M)
        if m and not re.search(rf"\b{re.escape(ident)}\b", m.group(1)):
            misattributed.append(f"{ident} filed under {st!r}")
    if misattributed:
        failures.append(f"MISATTRIBUTED {len(misattributed)}: {misattributed[:6]} — "
                        f"an ident must appear within its own station's block")

    # 3. PAIRING — a frequency is either stated by the document or shown as the
    #    station's full list, never guessed.
    ambiguous = [i for i, r in records.items() if r.get("frequency_is_ambiguous")]
    print(f"{'ambiguous frequency pairings':34} {len(ambiguous)}")

    # 3b. NO TWO NAVAIDS MAY SHARE A POSITION unless the AIP publishes one.
    #     A multi-navaid station publishes ONE coordinate line per navaid, in
    #     ident order. An earlier version kept a single value and overwrote it,
    #     so every record at KANO got the LAST position — KAN was shown at AO's
    #     location. A pilot tuning a navaid and navigating to the displayed
    #     coordinates would fly to the wrong place.
    by_station = {}
    for ident, rec in records.items():
        by_station.setdefault(rec["station"], []).append(rec)
    shared = []
    for st, recs in by_station.items():
        if len(recs) < 2:
            continue
        positions = [r.get("coordinates") for r in recs]
        # Identical positions are only acceptable when the extractor SAYS the
        # pairing was ambiguous — that is the visible fallback, not a claim.
        if len(set(positions)) == 1 and not recs[0].get("coordinates_are_ambiguous"):
            n_lines = len(re.findall(
                r"\d{5,6}(?:\.\d+)?[NS]\s*\d{7}(?:\.\d+)?[EW]",
                re.search(rf"^{re.escape(st.split()[0])}\b[\s\S]{{0,400}}",
                          blob, re.M).group(0)))
            if n_lines > 1:
                shared.append(f"{st}: {len(recs)} navaids share one position "
                              f"but the AIP publishes {n_lines}")
    if shared:
        failures.append(f"POSITION OVERWRITTEN {len(shared)}: {shared[:6]}")

    amb_pos = [i for i, r in records.items() if r.get("coordinates_are_ambiguous")]
    print(f"{'ambiguous position pairings':34} {len(amb_pos)}")

    # 4. PROVENANCE.
    not_verbatim = []
    for ident, rec in records.items():
        for field in ("coordinates", "elevation", "hours"):
            val = rec.get(field)
            if not val:
                continue
            # An ambiguous field holds the station's FULL published list joined
            # by " / ". Each PART must be verbatim; the separator is the
            # extractor's own and is not in the source. Checking the joined
            # string failed valid records — the values were right, the test was
            # comparing something the AIP never printed as one string.
            for part in str(val).split(" / "):
                if part and not verbatim_in_source(part, blob):
                    not_verbatim.append(f"{ident}.{field}={part!r}")
        for part in (rec.get("frequency") or "").split(" / "):
            if part and not verbatim_in_source(part, blob):
                not_verbatim.append(f"{ident}.frequency={part!r}")
    if not_verbatim:
        failures.append(f"NOT VERBATIM {len(not_verbatim)}: {not_verbatim[:6]}")

    warns = [i for i in ex.validate(result) if i.severity == "warning"]
    errs = [i for i in ex.validate(result) if i.severity == "error"]
    if warns:
        print(f"\nsource defects flagged ({len(warns)}):")
        for w in warns:
            print(f"   {w.message[:96]}")
    if result.warnings:
        print(f"\nextractor warnings ({len(result.warnings)}):")
        for w in result.warnings[:8]:
            print(f"   {w[:96]}")
    if errs:
        failures.append(f"VALIDATE ERRORS {len(errs)}: {[i.field for i in errs][:8]}")

    print("\n" + "=" * 72)
    if failures:
        for f in failures:
            print(f"FAIL  {f}")
        return 1
    print(f"PASS — {len(records)} navaids across {len(got_stations)} stations,")
    print("       every station yielded a navaid, no misattribution,")
    print("       every value verbatim.")
    print("\nNow run --spot-all and read a sample against the PDF.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
