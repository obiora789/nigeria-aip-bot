#!/usr/bin/env python3
"""
check_sids.py — SIDs work like approaches: measure it on the LIVE data.

  python checks/check_sids.py
      FREE (database reads only, no model calls). For every aerodrome, reads
      the stored AD 2.22 and prints what a pilot will be offered: the runways
      that publish SIDs, each runway's SIDs, whether each SID's published
      description proves itself (shown) or not (chart only), and how many SID
      chart pages are stored. Also lists any SID LEFT OUT because its own
      description climbs on a course that contradicts its runway heading —
      check those against the PDF.

  python checks/check_sids.py --model
      A few cents. Also asks the model a set of SID questions and look-alikes
      at every aerodrome that publishes SIDs, and checks that SID questions
      reach the SID choice for the RIGHT aerodrome and the look-alikes do not.

PASS RULES (--model)
  SID question   -> sid_request true, intent chart_retrieval, the aerodrome kept
  look-alike     -> sid_request false (departure frequency, take-off minima,
                    STAR, approach procedures)
"""
import sys as _sys, pathlib as _pl  # repo layout: bot at the top, scripts in folders
_R = next(d for d in _pl.Path(__file__).resolve().parents if (d / "main.py").exists())
_sys.path[1:1] = [str(p) for p in (_R, _R / "checks", _R / "ingestion", _R / "ingestion" / "validate")
                  if str(p) not in _sys.path]
import argparse
import sys

import resolver
import sids
from database import get_charts_smart, get_section_text

SID_QUESTIONS = [
    "SID {x}",
    "What are the departures from {x}",
    "SID for runway {rwy} at {x}",
    "{sid} departure {x}",
]
LOOK_ALIKES = [
    "{x} departure frequency",
    "take-off minima {x}",
    "STAR {x}",
    "Approach procedures in {x}",
]


def _place(names):
    return names[0].upper() if names[0] == "phsia" else names[0].title()


def survey():
    found = {}
    total = shown = 0
    print(f"{'ICAO':5} {'RWY':5} {'SID':10} {'description':12} charts")
    for icao, names in sorted(resolver.AERODROMES.items()):
        text = get_section_text(icao, "AD 2.22")
        left_out = []
        cat = sids.catalogue(text, left_out)
        if not cat and not left_out:
            continue
        charts = [c for c in get_charts_smart(icao, "", "")
                  if "sid" in (c.procedure_type or "").lower()]
        found[icao] = (names, cat)
        for s in cat:
            ok = sids.description(text, s) is not None
            total += 1
            shown += ok
            print(f"{icao:5} {s.runway:5} {s.name:10} {'SHOWN' if ok else 'chart only':12} "
                  f"{len(charts)}")
        for s in left_out:
            print(f"{icao:5} {s.runway:5} {s.name:10} LEFT OUT — its description climbs "
                  f"on a course that doesn't match RWY {s.runway}; check the PDF")
        if not charts:
            print(f"{icao:5}       (no SID chart pages stored)")
    print(f"\n{len(found)} aerodromes publish SID headings; {total} SIDs offered, "
          f"{shown} with a description shown verbatim, {total - shown} chart only.")
    return found


def model_check(found):
    from agent import extract_query_parameters
    rows = []
    for icao, (names, cat) in found.items():
        x = _place(names)
        first = cat[0]
        for q in SID_QUESTIONS:
            rows.append(("sid", icao, q.format(x=x, rwy=first.runway, sid=first.name)))
        for q in LOOK_ALIKES:
            rows.append(("other", icao, q.format(x=x)))
    bad = 0
    for kind, icao, q in rows:
        ex = extract_query_parameters(q)
        got = getattr(ex, "sid_request", False) if ex else None
        where = (ex.icao_code if ex and ex.icao_code in resolver.VALID_ICAO
                 else next(iter(resolver.match_name((ex.aerodrome_name or "") if ex else "")), None))
        if kind == "sid":
            ok = bool(got) and ex.intent == "chart_retrieval" and where == icao
        else:
            ok = not got
        bad += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {kind:5} {q!r:45} sid_request={got} "
              f"intent={getattr(ex, 'intent', None)} aerodrome={where}")
    print(f"\n{len(rows) - bad}/{len(rows)} as expected.")
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", action="store_true", help="also check the model's SID decision")
    args = ap.parse_args()
    resolver.load_index()
    found = survey()
    if args.model and found:
        print()
        sys.exit(1 if model_check(found) else 0)


if __name__ == "__main__":
    main()
