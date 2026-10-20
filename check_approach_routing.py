#!/usr/bin/env python3
"""
check_approach_routing.py — does an approach question reach the plate flow at
EVERY published aerodrome, and do the look-alike questions stay where they are?

Confirmed live (2026-10-20), Kano:
  "What is the ILS approach in Kano" -> AD 2.17 airspace block ("Kano Approach"
                                        call sign, CTR/TMA limits)
  "What is the approach in Kano"     -> AD 2.1 city and aerodrome name

The fix is not Kano-specific, so neither is this check: it asks the same
questions about all 40 aerodromes in resolver.AERODROMES.

TWO MODES — run the cheap one first.

  python check_approach_routing.py
      ROUTE mode. One extraction call per question (240 questions, a few
      cents, ~2 min), then the same routing decision main.py makes. Tells you
      where each question is SENT. Also checks the question resolved to the
      right aerodrome — "the approach in Jos" must not become Abuja.

  python check_approach_routing.py --full
      FULL mode. Drives the REAL main.process() for every question via
      run_stress_set.run_pipeline (Telegram sends captured, nothing posted),
      and reads the path main.py logged. This is what the pilot would get.
      Slower (~10-15 min): embeddings and database reads per question.

  --only DNKN DNMM   limit to some aerodromes
  --csv FILE         write every row (default approach_routing_results.csv)

PASS RULES
  approach question -> the plate flow ("chart" / "chart_not_found" in --full;
                       chart_retrieval or approach_procedure in route mode),
                       for the aerodrome that was asked about.
                       chart_not_found is a PASS: an aerodrome with no
                       published instrument approach says so honestly. Those
                       are listed separately so you can confirm each one.
  control question  -> anywhere EXCEPT the plate flow.
"""
import argparse
import csv
import sys
from concurrent.futures import ThreadPoolExecutor

import resolver
from agent import extract_query_parameters
import synthesize

APPROACH_PHRASINGS = [
    "What is the approach in {x}",
    "What is the ILS approach in {x}",
    "{x} VOR approach",
    "RNAV approach for {x}",
]
CONTROL_PHRASINGS = [
    "{x} approach frequency",
    "frequency of {x} Approach",
    "transition altitude at {x}",
]


def _cases(only):
    rows = []
    for icao, names in sorted(resolver.AERODROMES.items()):
        if only and icao not in only:
            continue
        # names[0] is the city; PHSIA is the one aerodrome known by an acronym.
        place = names[0].upper() if names[0] == "phsia" else names[0].title()
        for p in APPROACH_PHRASINGS:
            rows.append({"icao": icao, "kind": "approach", "q": p.format(x=place)})
        for p in CONTROL_PHRASINGS:
            rows.append({"icao": icao, "kind": "control", "q": p.format(x=place)})
    return rows


def _resolved_icao(ex):
    if ex is None:
        return None
    if ex.icao_code in resolver.VALID_ICAO:
        return ex.icao_code
    hits = resolver.match_name(ex.aerodrome_name or "")
    return next(iter(hits)) if len(hits) == 1 else None


def route(row):
    """Where main.py would SEND this question (no search, no Telegram)."""
    q = row["q"]
    try:
        ex = extract_query_parameters(q)
    except Exception as e:                           # noqa: BLE001
        return {**row, "dest": f"error:{type(e).__name__}", "got_icao": None, "detail": ""}
    if ex is None:
        return {**row, "dest": "extraction_failed", "got_icao": None, "detail": ""}
    got = _resolved_icao(ex) or next(iter(resolver.match_name(q)), None)
    detail = f"intent={ex.intent} type={ex.procedure_type} sub={ex.ad2_subsection}"
    if ex.intent == "chart_retrieval":
        dest = "PLATE"
    else:
        status, payload = synthesize.synthesize_decision(q, [], ex)
        dest = "PLATE" if status == "approach_procedure" else f"{status} {payload or ''}".strip()
    return {**row, "dest": dest, "got_icao": got, "detail": detail}


def full(row):
    """What the pilot actually gets, through the real main.process()."""
    import run_stress_set
    r = run_stress_set.run_pipeline(row["q"])
    path = r.get("path") or "unknown"
    dest = "PLATE" if path in ("chart", "chart_not_found") else path
    first = (r.get("reply") or "").strip().splitlines()
    return {**row, "dest": dest, "got_icao": r.get("icao"),
            "detail": f"path={path} | {first[0][:70] if first else ''}",
            "no_chart": path == "chart_not_found"}


def judge(r):
    plate = r["dest"] == "PLATE"
    if r["kind"] == "approach":
        return plate and r.get("got_icao") == r["icao"]
    return not plate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="drive the real main.process()")
    ap.add_argument("--only", nargs="*", default=None, metavar="ICAO")
    ap.add_argument("--csv", default="approach_routing_results.csv")
    ap.add_argument("--repeat", type=int, default=1,
                    help="ask every question N times; the model is not perfectly "
                         "repeatable, so an intermittent misroute needs N>1 to show")
    a = ap.parse_args()

    resolver.load_index()
    # Load the ENR entity list ONCE, before any threads start. Eight threads
    # each loading it at the same moment made Supabase drop the HTTP/2
    # connection ("ConnectionTerminated" tracebacks in route mode) — harmless
    # to the result, but noise that hides real errors.
    try:
        resolver.published_entities()
    except Exception:                                # noqa: BLE001
        pass
    cases = _cases(set(x.upper() for x in a.only) if a.only else None) * max(1, a.repeat)
    n_ad = len({c["icao"] for c in cases})
    print(f"{'FULL' if a.full else 'ROUTE'} mode: {len(cases)} questions across "
          f"{n_ad} aerodromes" + (f" ({a.repeat} passes)" if a.repeat > 1 else "") + "\n")

    if a.full:
        # Sequential: run_pipeline swaps module-level senders, which is not
        # safe to do from several threads at once.
        results = []
        for i, c in enumerate(cases, 1):
            results.append(full(c))
            if i % 20 == 0:
                print(f"  {i}/{len(cases)}")
    else:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(route, cases))

    for r in results:
        r["ok"] = judge(r)

    bad = [r for r in results if not r["ok"]]
    by_ad = {}
    for r in results:
        by_ad.setdefault(r["icao"], []).append(r)

    print(f"{'ICAO':6} {'approach':>9} {'controls':>9}")
    for icao, rs in sorted(by_ad.items()):
        ap_ok = sum(r["ok"] for r in rs if r["kind"] == "approach")
        ap_n = sum(r["kind"] == "approach" for r in rs)
        ct_ok = sum(r["ok"] for r in rs if r["kind"] == "control")
        ct_n = sum(r["kind"] == "control" for r in rs)
        flag = "" if ap_ok == ap_n and ct_ok == ct_n else "   <-- check"
        print(f"{icao:6} {ap_ok:>4}/{ap_n:<4} {ct_ok:>4}/{ct_n:<4}{flag}")

    if bad:
        print(f"\nWRONG ({len(bad)}):")
        for r in bad:
            why = ("wrong aerodrome" if r["kind"] == "approach" and r["dest"] == "PLATE"
                   else "not the plate flow" if r["kind"] == "approach"
                   else "control sent to the plate flow")
            print(f"  [{r['icao']}] {r['q']!r}")
            print(f"        -> {r['dest']}  got={r.get('got_icao')}  ({why})  {r['detail']}")

    if a.full:
        none = sorted({r["icao"] for r in results if r.get("no_chart")})
        if none:
            print(f"\nNo matching plate found (honest 'not found', counted as PASS) at: "
                  f"{', '.join(none)}")
            print("  Confirm against AD 2.24 that these genuinely publish no such approach.")

    with open(a.csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["icao", "kind", "q", "dest", "got_icao",
                                          "ok", "detail"], extrasaction="ignore")
        w.writeheader()
        w.writerows(results)

    print(f"\n{len(results) - len(bad)}/{len(results)} correct. "
          f"Every row written to {a.csv}.")
    print("ALL OK" if not bad else f"{len(bad)} WRONG")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
