#!/usr/bin/env python3
"""
validate_enr51.py — full-coverage proof for ENR51Extractor.

Mirrors the 23 AD validators, but the denominator is "every area the AIP
publishes" rather than "36 aerodromes".

WHAT THIS PROVES, AND WHAT IT DOES NOT
--------------------------------------
It proves COMPLETENESS and INTERNAL CONSISTENCY:
  1. every DNP/DNR/DND identifier on the pages became a record (no silent drop)
  2. no identifier was invented
  3. every record has a position, in one of the two published forms
  4. every record has an upper limit
  5. no duplicate identifiers
  6. every extracted value appears VERBATIM in the source page text

Check 6 is the important one and is why this is worth running: it catches a
value that was assembled, reformatted or spliced rather than copied. It cannot
catch a value copied from the WRONG ROW — for that, see the spot-check mode
below, which prints records beside their source text for a human to read.

  python ingestion/validate/validate_enr51.py Complete_AIP2026.pdf              # full check
  python ingestion/validate/validate_enr51.py Complete_AIP2026.pdf --spot DND45 # read one area
  python ingestion/validate/validate_enr51.py Complete_AIP2026.pdf --spot-all   # read all 57

The spot-check is not optional ceremony. Extraction bugs in this project have
repeatedly passed automated checks while being obviously wrong to a reader —
a column header stored as a value, a value welded to the wrong label. Reading
ten areas against the PDF takes ten minutes and is the only thing that catches
that class.

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

from enr51_extractor import ENR51Extractor, _AREA_ID_RE

# ENR 5.1 content pages carry the running header "ENR 5.1-<n>". The table of
# contents also mentions "ENR 5.1" but never in that page-number form, which is
# what keeps TOC pages out — the same trap that once contaminated an AD 2.22
# extraction with 500 pages of unrelated sections.
# Match the page's OWN running header, at the very start of the page. A TOC
# page lists "ENR 5.1 PROHIBITED... ENR 5.1-1" in its body, so searching
# anywhere pulls in pages 14 and 121 (ENR 0.6 contents). They contribute no
# records so the count survives, but a filter that admits the TOC is one
# document change away from admitting its text too — this exact looseness once
# swept 500 unrelated pages into an AD 2.22 extraction.
_PAGE_HDR_RE = re.compile(r"^\s*ENR\s*5\.1\s*-\s*\d+")

# Identifiers cited in the naming-convention paragraph on the first page, not
# real areas: "areas are assigned numbers and letters in the following manner:
# DNP1, DND1, DNR1, etc." Excluded from the expected set by their context, not
# by hard-coding the ids.
_EXAMPLE_CONTEXT_RE = re.compile(
    r"following manner\s*:\s*[^.]{0,80}", re.I)


def load_pages(pdf_path):
    doc = pdfium.PdfDocument(pdf_path)
    pages = []
    for i in range(len(doc)):
        page = doc[i]
        tp = page.get_textpage()
        t = tp.get_text_range()
        tp.close()
        page.close()
        if _PAGE_HDR_RE.match(t):
            pages.append((i, t))
    return pages


def expected_ids(pages):
    """Every area id published on the pages, minus those appearing only inside
    the naming-convention example sentence."""
    real, example = set(), set()
    for _, t in pages:
        for m in _EXAMPLE_CONTEXT_RE.finditer(t):
            example |= {re.sub(r"\s+", "", x)
                        for x in re.findall(r"\bDN[PRD]\s?\d{1,3}\b", m.group(0))}
        real |= {re.sub(r"\s+", "", x)
                 for x in re.findall(r"\bDN[PRD]\s?\d{1,3}\b", t)}
    # An id that appears ONLY in the example sentence is not a real area.
    only_example = set()
    for eid in example:
        occurrences = sum(len(re.findall(rf"\b{eid[:3]}\s?{eid[3:]}\b", t))
                          for _, t in pages)
        if occurrences <= 1:
            only_example.add(eid)
    return real - only_example, only_example


def _norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def verbatim_in_source(value, blob):
    """True if `value` appears in the source text, tolerating the whitespace
    and line-break differences extraction introduces."""
    if not value:
        return True
    v = re.escape(_norm(value)).replace(r"\ ", r"\s*")
    return re.search(v, blob, re.I | re.S) is not None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--spot", help="print one area beside its source text")
    ap.add_argument("--spot-all", action="store_true",
                    help="print every area for human review")
    a = ap.parse_args()

    pages = load_pages(a.pdf)
    print(f"ENR 5.1 content pages: {len(pages)} "
          f"({', '.join(str(i) for i, _ in pages)})\n")
    if not pages:
        print("No ENR 5.1 content pages found — has the section moved?")
        return 1

    blob = "\n".join(t for _, t in pages)
    ex = ENR51Extractor()
    ex.segment_text = lambda _segments: blob
    result = ex.extract("ENR", [1])
    records = {r["scope_id"]: r for r in result.records}

    # ---- spot-check modes -------------------------------------------------
    if a.spot or a.spot_all:
        targets = ([a.spot.upper()] if a.spot
                   else sorted(records, key=lambda k: (k[2], int(k[3:]))))
        for aid in targets:
            rec = records.get(aid)
            if not rec:
                print(f"{aid}: NOT EXTRACTED")
                continue
            print("=" * 72)
            print(f"{aid}  ({rec['family']})  name={rec['name']!r}")
            print(f"  upper : {rec['upper_limit']!r}")
            print(f"  lower : {rec['lower_limit']!r}")
            # Print EVERY vertex. An earlier [:4] slice here made DNR30 (6
            # points) and DNR27 (5) look truncated and sent a real diagnosis
            # after a data bug that did not exist — the extractor had stored
            # them all along. A reviewer cannot check a polygon they cannot
            # see, so display truncation in a verification tool is itself a
            # defect.
            _c = rec["coordinates"] or []
            print(f"  posn  : {rec['position_kind']}  ({len(_c)} point(s))")
            for _pt in _c:
                print(f"            {_pt}")
            print(f"  limits: {_norm(rec.get('lateral_limits'))[:300] or None}")
            print(f"  --- source text as extracted ---")
            print("  " + _norm(rec["raw_text"])[:600])
            print()
        print("Read each against the PDF. The automated checks below cannot "
              "tell a correct value from one copied out of the adjacent row.")
        return 0

    # ---- automated checks -------------------------------------------------
    want, examples = expected_ids(pages)
    got = set(records)
    failures = []

    missed = sorted(want - got)
    invented = sorted(got - want)
    print(f"{'ids published on the pages':38} {len(want)}")
    print(f"{'ids extracted as records':38} {len(got)}")
    if examples:
        print(f"{'excluded (naming-convention examples)':38} "
              f"{len(examples)}  {sorted(examples)}")
    if missed:
        failures.append(f"MISSED {len(missed)}: {missed}")
    if invented:
        failures.append(f"INVENTED {len(invented)}: {invented}")

    no_pos = [k for k, r in records.items() if not r.get("position_kind")]
    no_upper = [k for k, r in records.items() if not r.get("upper_limit")]
    if no_pos:
        failures.append(f"NO POSITION {len(no_pos)}: {sorted(no_pos)}")
    if no_upper:
        failures.append(f"NO UPPER LIMIT {len(no_upper)}: {sorted(no_upper)}")

    # Every value must be COPIED, not composed. This is the check that catches
    # a reformatted or spliced value — the failure mode automated tests in this
    # project have historically missed.
    not_verbatim = []
    for aid, rec in records.items():
        for field in ("upper_limit", "lower_limit", "name"):
            val = rec.get(field)
            if val and not verbatim_in_source(val, blob):
                not_verbatim.append(f"{aid}.{field}={val!r}")
        for c in (rec.get("coordinates") or []):
            if not verbatim_in_source(c, blob):
                not_verbatim.append(f"{aid}.coord={c!r}")
    if not_verbatim:
        failures.append(f"NOT VERBATIM {len(not_verbatim)}: {not_verbatim[:6]}")

    # FAMILY must follow the identifier letter. The AIP states the convention
    # itself ("DNP1, DND1, DNR1"), so this is checkable, not a preference. An
    # earlier version tracked the most recent section heading and misfiled both
    # BOUNDARY rows — DNP8 as restricted, DNR44 as danger. Family decides
    # whether a pilot is prohibited from an area or merely restricted.
    # NUMBERING IS ONE CONTINUOUS SEQUENCE across the three families, not a
    # per-family series: DNP1-8, then DNR9-44, then DND45-57. Confirmed by the
    # publisher — the sequence exists to enumerate every area to avoid, so a
    # GAP means an area is missing from the extraction or from the document,
    # and that is precisely the failure a pilot cannot detect.
    #
    # This is a stronger check than counting identifiers, which only proves we
    # captured what we saw. A run from 1 to N with no holes proves we saw
    # everything the sequence claims to contain. (An earlier version of this
    # review wrongly flagged "DNR2-DNR8 missing" by assuming each letter had
    # its own series.)
    nums = sorted(int(k[3:]) for k in records)
    if nums:
        expected_run = set(range(min(nums), max(nums) + 1))
        gaps = sorted(expected_run - set(nums))
        if gaps:
            failures.append(f"SEQUENCE GAPS {len(gaps)}: {gaps} "
                            f"(numbering runs {min(nums)}-{max(nums)} unbroken)")
        print(f"\nsequence        : {min(nums)}-{max(nums)}, "
              f"{'unbroken' if not gaps else str(len(gaps)) + ' GAP(S)'}")
        # The families must partition that run in order, never interleave.
        order = [(int(k[3:]), k[2]) for k in sorted(records, key=lambda x: int(x[3:]))]
        transitions = [b for (_, a), (_, b) in zip(order, order[1:]) if a != b]
        if len(transitions) > 2 or transitions != sorted(set(transitions),
                                                         key="PRD".index)[:len(transitions)]:
            failures.append(f"FAMILY BLOCKS INTERLEAVED: transitions {transitions}")
        else:
            bounds = {}
            for n, letter in order:
                bounds.setdefault(letter, [n, n])[1] = n
            print("family blocks   : " + ", ".join(
                f"DN{L}{v[0]}-{v[1]}" for L, v in bounds.items()))

    fam_by_letter = {"P": "prohibited", "R": "restricted", "D": "danger"}
    wrong_family = [f"{k}:{r['family']}" for k, r in records.items()
                    if r.get("family") != fam_by_letter.get(k[2])]
    if wrong_family:
        failures.append(f"FAMILY MISMATCH {len(wrong_family)}: {wrong_family[:8]}")

    # EVERY vertex must be captured. A polygon missing a corner is a different
    # polygon, and nothing downstream can tell that it was truncated.
    coord_re = re.compile(r"\*?\d{4,8}(?:\.\d+)?[NS]\s*\d{5,9}(?:\.\d+)?[EW]")
    dropped = []
    for aid, rec in records.items():
        in_src = len(coord_re.findall(rec.get("raw_text") or ""))
        n_kept = len(rec.get("coordinates") or [])
        if n_kept < in_src:
            dropped.append(f"{aid} {n_kept}/{in_src}")
    if dropped:
        failures.append(f"COORDINATES DROPPED {len(dropped)}: {dropped[:8]}")

    # A vertical limit stated in feet must keep its datum. "40 000 FT" and
    # "40 000 FT ALT" are different claims, and a bare figure leaves the reader
    # to guess between height above ground and altitude above sea level.
    bare_ft = []
    for aid, rec in records.items():
        for field in ("upper_limit", "lower_limit"):
            v = rec.get(field) or ""
            if re.search(r"\d\s*(FT|ft)\s*$", v):
                ctx = re.search(re.escape(_norm(v)).replace(r"\ ", r"\s*")
                                + r"\s*(AGL|AMSL|MSL|ALT)\b", blob, re.I)
                if ctx:
                    bare_ft.append(f"{aid}.{field}={v!r} (source says "
                                   f"{ctx.group(1)})")
    if bare_ft:
        failures.append(f"DATUM DROPPED {len(bare_ft)}: {bare_ft[:6]}")

    from collections import Counter
    fams = Counter(r["family"] for r in records.values())
    poss = Counter(r["position_kind"] for r in records.values())
    print(f"\nby family        : {dict(fams)}")
    print(f"by position kind : {dict(poss)}")
    print(f"extractor warnings: {len(result.warnings)}")
    for w in result.warnings[:8]:
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
    print(f"PASS — {len(got)} areas, all published ids captured, none invented,")
    print("       every value present verbatim in the source.")
    print("\nNow run --spot-all and read a sample against the PDF. Verbatim")
    print("presence does not prove a value came from the RIGHT row.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
