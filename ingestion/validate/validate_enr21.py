#!/usr/bin/env python3
"""
validate_enr21.py — full-coverage proof for ENR21Extractor.

Same three levels as the ENR 5.1 and 4.4 validators. The checks differ because
the failure mode differs: ENR 2.1 has no row delimiter, so its risk is not a
dropped value but a WRONG ENTITY.

  1. hierarchy    every airspace the AIP declares became a record, and the
                  parent/child relationships match the document's own
                  "consists of" sentences
  2. no invention every record's name appears as a heading or a declaration —
                  never merely inside prose
  3. provenance   every value appears VERBATIM in the source text
  4. spot-check   --spot-all, read against the PDF

CHECK 2 IS THE ONE THAT MATTERS. An earlier extractor treated any short line
naming FIR/SECTOR/TMA as an entity, and "Kaduna TMA" — a phrase inside KANO
TMA's lateral limits ("...delineated to the south-west by Kaduna TMA") — became
an entity and ABSORBED Kano's Class B and FL 145. Kano's own record was left
with no vertical limits at all. Nothing about the values was wrong; the entity
boundary was, which is the same misattribution class arriving one level up.

  python ingestion/validate/validate_enr21.py Complete_AIP2026.pdf
  python ingestion/validate/validate_enr21.py Complete_AIP2026.pdf --spot-all

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

from enr21_extractor import ENR21Extractor, discover_entities, _clean_lines

_PAGE_HDR_RE = re.compile(r"^\s*ENR\s*2\.1\s*-\s*\d+")

# The hierarchy the AIP states in its own words. Independent of the extractor's
# discovery, so this is a genuine cross-check rather than a restatement.
_CONSISTS_RE = re.compile(
    r"\(([A-Z][A-Z\s]{2,40}?)\s+consists\s+of\s+([^)]+)\)", re.I | re.S)


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


def declared_hierarchy(blob):
    """{child: parent} straight from the AIP's own sentences."""
    out = {}
    for m in _CONSISTS_RE.finditer(blob):
        parent = _norm(m.group(1))
        for child in re.split(r"\s+and\s+|,\s*", _norm(m.group(2))):
            child = _norm(child)
            if re.search(r"\b(SECTOR|FIR|TMA|CTA)\b", child, re.I) and len(child) < 40:
                out[child] = parent
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--spot", help="print one airspace beside its source block")
    ap.add_argument("--spot-all", action="store_true")
    a = ap.parse_args()

    pages = load_pages(a.pdf)
    print(f"ENR 2.1 content pages: {len(pages)} "
          f"({', '.join(str(i) for i, _ in pages)})\n")
    if not pages:
        print("No ENR 2.1 content pages found — has the section moved?")
        return 1

    blob = "\n".join(t for _, t in pages)
    ex = ENR21Extractor()
    ex.segment_text = lambda _segments: blob
    result = ex.extract("ENR", [1])
    records = {r["scope_id"]: r for r in result.records}

    if a.spot or a.spot_all:
        targets = [a.spot] if a.spot else list(records)
        for name in targets:
            rec = next((r for k, r in records.items()
                        if k.upper().startswith((name or "").upper())), None)
            if not rec:
                print(f"{name}: NOT EXTRACTED")
                continue
            print("=" * 72)
            print(f"{rec['scope_id']}"
                  + ("   [grouping — publishes no limits of its own]"
                     if rec.get("is_grouping") else ""))
            print(f"  parent   : {rec['parent']}")
            print(f"  note     : {rec['note']}")
            print(f"  vertical : {rec['vertical_limits']}")
            print(f"  class    : {rec['airspace_class']}")
            print(f"  hours    : {rec['hours']}")
            print(f"  freq     : {rec['frequencies']}")
            print(f"  lateral  : {_norm(rec['lateral_limits'])[:400]}")
            print()
        print("Read each against the PDF. In particular, check that a TMA's "
              "vertical limits belong to THAT TMA — an adjacent one's name "
              "appearing in the lateral-limits prose is how they get swapped.")
        return 0

    failures = []
    lines = _clean_lines(blob)
    discovered = discover_entities(lines)
    declared = declared_hierarchy(blob)

    print(f"{'airspaces extracted':34} {len(records)}")
    print(f"{'declared by "consists of"':34} {len(declared)}")

    # 1. HIERARCHY matches the document's own declarations.
    for child, parent in declared.items():
        rec = records.get(child)
        if not rec:
            failures.append(f"DECLARED BUT MISSING: {child!r} "
                            f"(the AIP says it is part of {parent!r})")
            continue
        got = _norm(rec.get("parent") or "")
        if got and parent.upper() not in got.upper() \
                and got.upper() not in parent.upper():
            failures.append(f"WRONG PARENT: {child!r} recorded under {got!r}, "
                            f"the AIP says {parent!r}")

    # 2. NO INVENTED ENTITIES. Every name must appear as a heading line or in a
    #    declaration — not merely somewhere in the prose.
    heading_lines = {_norm(ln) for ln in lines}
    invented = []
    for nm in records:
        if nm in heading_lines or nm in declared:
            continue
        # a heading whose kind wrapped onto the next line
        head = _norm(nm.rsplit("(", 1)[0])
        if head in heading_lines:
            continue
        invented.append(nm)
    if invented:
        failures.append(f"POSSIBLY INVENTED {len(invented)}: {invented} — "
                        f"a name that appears only inside prose is not an entity")

    # 3. PROVENANCE.
    not_verbatim = []
    for nm, rec in records.items():
        for field in ("vertical_limits", "airspace_class", "frequencies"):
            val = rec.get(field)
            if not val:
                continue
            for part in val.split(" / "):
                if part and not verbatim_in_source(part, blob):
                    not_verbatim.append(f"{nm}.{field}={part!r}")
    if not_verbatim:
        failures.append(f"NOT VERBATIM {len(not_verbatim)}: {not_verbatim[:6]}")

    # 4. Every non-grouping airspace must publish its vertical extent.
    missing = [nm for nm, r in records.items()
               if not r.get("is_grouping") and not r.get("vertical_limits")]
    if missing:
        failures.append(f"NO VERTICAL LIMITS {len(missing)}: {missing}")

    # COLUMN SEPARATION. The unit, call sign, language and remarks are their
    # own published columns; sweeping them into lateral_limits meant a pilot
    # asking where a sector's boundary runs was also handed ATC callsigns and
    # CPDLC logon addresses. Confirmed against the printed table: "KANO
    # CONTROL" is column 2, "KANO EAST / EN / H24" column 3, the boundary
    # description column 1.
    leaked = []
    for nm, rec in records.items():
        lat = rec.get("lateral_limits") or ""
        for marker in ("CPDLC LOGON", " APP ", " TOWER", "CONTROL "):
            if marker in lat.upper():
                leaked.append(f"{nm}: {marker.strip()!r} in lateral_limits")
                break
    if leaked:
        failures.append(f"COLUMN LEAK {len(leaked)}: {leaked[:6]}")

    # A note is a whole-line parenthetical, never a bracket lifted out of the
    # boundary prose. "(083936.96N 0065200.52E) that cuts the Kano/" as a note
    # both invented a note and cut a fragment out of the limits text.
    bad_notes = [f"{nm}: {r['note'][:40]!r}" for nm, r in records.items()
                 if r.get("note") and re.match(r"^\(\s*\d{6}", r["note"])]
    if bad_notes:
        failures.append(f"COORDINATE CAPTURED AS NOTE {len(bad_notes)}: {bad_notes}")

    groups = [nm for nm, r in records.items() if r.get("is_grouping")]
    print(f"{'groupings (no own limits)':34} {len(groups)}  {groups}")
    print(f"{'extractor warnings':34} {len(result.warnings)}")
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
    print(f"PASS — {len(records)} airspaces, hierarchy matches the AIP's own")
    print("       declarations, no invented entities, every value verbatim.")
    print("\nNow run --spot-all and read a sample against the PDF.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
