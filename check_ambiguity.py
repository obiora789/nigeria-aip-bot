#!/usr/bin/env python3
"""
check_ambiguity.py — does Vannie treat a question as a double meaning when it
has two real readings, and ONLY then? Measured at every published aerodrome.

Since 2026-10-26 (option B) "ASK" below means: Vannie answers the usual
reading and puts one "Did you mean ...?" button under the answer.

The model decides what is ambiguous (disambiguate.judge); this measures
that decision against cases found in real data this month:

  "Sokoto approach" / "What is" / "Show me ..."  Approach ATC unit, or the plate
                                                 (confirmed live 2026-10-21)
  "minima for X"      approach minima, or take-off minima
  "clearance at X"    AD 2.7 movement-area clearing, or AD 2.22 ATC clearance
                      rules (the stress set confused these at 4 aerodromes)
  "lighting at X"     runway/approach lighting (AD 2.14), or other lighting (2.15)
  "taxiing at X"      AD 2.20 taxiing rules, or AD 2.8 taxiway data (the stress
                      set confused these)
  "charges at X"      GEN 4.1 aerodrome charges, or GEN 4.2 ANS charges

Each has a CLEAR counterpart that must NOT ask ("Sokoto approach frequency",
"clearance priorities at Lagos" ...). Asking there is a failure too: a pilot
who asked precisely should get the answer.

    python check_ambiguity.py                         # all 40 aerodromes
    python check_ambiguity.py --only DNSO DNKN
    python check_ambiguity.py --stress stress_questions.csv
          # also: how often does an ORDINARY stress-set question ask?
    python check_ambiguity.py --repeat 2              # the model is not perfectly
                                                      # repeatable; N passes, all must agree

For every question that asks, each offered option's rewritten question is
routed too, and shown, so you can see that every button leads somewhere
sensible (a plate, a frequency, the right section).
"""
import argparse
import csv
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import resolver
import synthesize
import disambiguate
from agent import extract_query_parameters

ASK = [
    ("approach-bare",   "{x} approach"),
    ("approach-what",   "What is {x} approach"),
    ("approach-show",   "Show me {x} approach"),
    ("minima",          "minima for {x}"),
    ("clearance",       "clearance at {x}"),
    ("lighting",        "lighting at {x}"),
    ("taxiing",         "taxiing at {x}"),
    ("charges",         "charges at {x}"),
]
CLEAR = [
    ("approach-freq",   "{x} approach frequency"),
    ("approach-plate",  "approach plate for {x}"),
    ("approach-ils",    "What is the ILS approach in {x}"),
    ("approach-minima", "approach minima for {x}"),
    ("takeoff-minima",  "take-off minima for {x}"),
    ("clear-prio",      "clearance priorities at {x}"),
    ("vfr-clearance",   "ATC clearance requirements for VFR flights at {x}"),
    ("rwy-lighting",    "runway lighting at {x}"),
    ("taxi-limits",     "taxiing limitations at {x}"),
    ("landing-fee",     "landing fee at {x}"),
    ("ta",              "transition altitude at {x}"),
]


def _place(names):
    return names[0].upper() if names[0] == "phsia" else names[0].title()


def _where(q):
    """Extract once; return (asks?, options, destination-if-answered, detail)."""
    try:
        ex = extract_query_parameters(q)
    except Exception as e:                           # noqa: BLE001
        return False, None, f"error:{type(e).__name__}", ""
    if ex is None:
        return False, None, "extraction_failed", ""
    opts = disambiguate.ask(q)
    detail = f"intent={ex.intent} sub={ex.ad2_subsection} appr={getattr(ex, 'approach_request', None)}"
    if ex.intent == "chart_retrieval":
        dest = "PLATE"
    elif ex.intent == "out_of_scope":
        dest = "REFUSED"
    elif getattr(ex, "gen4_charge", None):
        dest = f"charges:{ex.gen4_charge}"
    else:
        status, payload = synthesize.synthesize_decision(q, [], ex)
        dest = "PLATE" if status == "approach_procedure" else f"{status} {payload or ''}".strip()
    return bool(opts), opts, dest, detail


def _run(q, repeat):
    runs = [_where(q) for _ in range(repeat)]
    asks = [r[0] for r in runs]
    return {"asks_all": all(asks), "asks_any": any(asks), "opts": next(
        (r[1] for r in runs if r[1]), None), "dest": runs[0][2], "detail": runs[0][3]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--stress", metavar="CSV")
    ap.add_argument("--repeat", type=int, default=1)
    a = ap.parse_args()

    resolver.load_index()
    try:
        resolver.published_entities()     # once, before threads (see other checkers)
    except Exception:                                # noqa: BLE001
        pass

    only = {x.upper() for x in a.only} if a.only else None
    cases = []
    for icao, names in sorted(resolver.AERODROMES.items()):
        if only and icao not in only:
            continue
        x = _place(names)
        cases += [("ASK", tag, icao, t.format(x=x)) for tag, t in ASK]
        cases += [("CLEAR", tag, icao, t.format(x=x)) for tag, t in CLEAR]
    print(f"{len(cases)} questions x {a.repeat} pass(es)\n")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda c: _run(c[3], a.repeat), cases))

    tally = defaultdict(Counter)
    wrong, samples = [], {}
    for (want, tag, icao, q), r in zip(cases, results):
        ok = r["asks_all"] if want == "ASK" else not r["asks_any"]
        tally[(want, tag)]["ok" if ok else "bad"] += 1
        if not ok:
            wrong.append((want, tag, icao, q, r))
        if want == "ASK" and r["opts"] and tag not in samples:
            samples[tag] = (q, r["opts"])

    print(f"{'must':6} {'case':16} {'correct':>9}")
    for (want, tag), c in tally.items():
        n = c["ok"] + c["bad"]
        flag = "" if c["bad"] == 0 else "   <-- check"
        print(f"{want:6} {tag:16} {c['ok']:>4}/{n:<4}{flag}")

    if wrong:
        print(f"\nWRONG ({len(wrong)}):")
        for want, tag, icao, q, r in wrong:
            what = ("not treated as a double meaning" if want == "ASK"
                    else "treated as a double meaning when the question was clear")
            print(f"  [{icao}] {q!r} — {what} -> {r['dest']}  {r['detail']}")
            if r["opts"]:
                print("        offered: " + " | ".join(o["label"] for o in r["opts"]))

    print("\nWHERE EACH READING LEADS (default first, then the button) — check by eye:")
    for tag, (q, opts) in samples.items():
        print(f"\n  {q!r}")
        for o in opts:
            _, _, dest, detail = _where(o["question"])
            print(f"    [{o['label']}] {o['question']!r}\n        -> {dest}  {detail}")

    bad_total = len(wrong)
    if a.stress:
        rows = [r for r in csv.DictReader(open(a.stress)) if r.get("question")]
        qs = [r["question"] for r in rows]
        print(f"\nORDINARY QUESTIONS (stress set): {len(qs)} asked once each")
        with ThreadPoolExecutor(max_workers=4) as pool:
            sr = list(pool.map(lambda q: _where(q), qs))
        asked = [(rows[i], s) for i, s in enumerate(sr) if s[0]]
        print(f"  Vannie would ASK on {len(asked)}/{len(qs)} "
              f"({100.0 * len(asked) / max(1, len(qs)):.1f}%). Each should be a genuinely")
        print("  two-reading question; anything else is a needless question to the pilot:")
        for row, s in asked:
            print(f"   [{row.get('subsection', '')}] {row['question']!r}: "
                  + " | ".join(o["label"] for o in s[1]))

    print(f"\n{'ALL OK' if not bad_total else f'{bad_total} WRONG'}")
    return 1 if bad_total else 0


if __name__ == "__main__":
    sys.exit(main())
