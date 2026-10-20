#!/usr/bin/env python3
"""
check_subsection.py — did a prompt change move the AD 2.x subsection classifier?

The stress run (2026-10-20, FACTS_ENABLED=1) answered several questions from
the wrong AD 2 section compared with August: "clearance priorities" -> AD 2.22,
"flight documentation" -> AD 2.5/2.20, "taxiing to and from stands" -> AD 2.8.
Each of those is ad2_subsection, a field the extraction model fills in the SAME
call whose instructions changed today. This script measures that field alone:
one extraction call per question, no search, no database, no Telegram.

Run it on the old and the new code, same questions, and compare:

    cp stress_results.csv stress_questions.csv          # keep the question list
    git stash push -- agent.py schemas.py
    python check_subsection.py stress_questions.csv --tag sub_before
    git stash pop
    python check_subsection.py stress_questions.csv --tag sub_after
    python check_subsection.py --compare sub_before.json sub_after.json

The model is not perfectly repeatable; --repeat 2 asks every question twice
and counts a row correct only if every pass was correct.
"""
import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from agent import extract_query_parameters
import synthesize


def _expected(r):
    s = (r.get("subsection") or "").strip()
    return s if s.startswith("AD 2.") else None


def classify(q):
    try:
        ex = extract_query_parameters(q)
    except Exception as e:                           # noqa: BLE001
        return f"<error:{type(e).__name__}>"
    if ex is None:
        return "<extraction failed>"
    return synthesize._normalise_subsection(getattr(ex, "ad2_subsection", None)) or "null"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("infile", nargs="?", help="a stress_results.csv or stress set CSV")
    ap.add_argument("--tag", default="sub_run")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    a = ap.parse_args()

    if a.compare:
        b, c = (json.load(open(p)) for p in a.compare)
        bq, cq = b["rows"], c["rows"]
        common = [q for q in bq if q in cq]
        print(f"{'':28}{'before':>10}{'after':>10}")
        print(f"{'correct subsection':28}{b['correct']:>10}{c['correct']:>10}   of {b['n']}/{c['n']}")
        print(f"{'returned null':28}{b['null']:>10}{c['null']:>10}")
        lost = [q for q in common if bq[q]["ok"] and not cq[q]["ok"]]
        won = [q for q in common if not bq[q]["ok"] and cq[q]["ok"]]
        print(f"\nnow WRONG, were right ({len(lost)}):")
        for q in lost:
            print(f"  [{cq[q]['want']}] {q!r}: was {bq[q]['got']} -> now {cq[q]['got']}")
        print(f"\nnow right, were wrong ({len(won)}):")
        for q in won:
            print(f"  [{cq[q]['want']}] {q!r}: was {bq[q]['got']} -> now {cq[q]['got']}")
        return 0

    if not a.infile:
        ap.error("give a CSV, or --compare")
    rows = [r for r in csv.DictReader(open(a.infile)) if _expected(r) and r.get("question")]
    if "expected_behaviour" in (rows[0] if rows else {}):
        rows = [r for r in rows if (r.get("expected_behaviour") or "answer") != "clarify"]
    qs = [r["question"] for r in rows]
    print(f"{len(qs)} questions x {a.repeat} pass(es)\n")

    # Load the ENR entity list once, before the threads start; four threads
    # loading it together made Supabase drop the connection (harmless
    # tracebacks, but they bury the result).
    try:
        import resolver
        resolver.load_index()
        resolver.published_entities()
    except Exception:                                # noqa: BLE001
        pass

    passes = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for _ in range(max(1, a.repeat)):
            passes.append(list(pool.map(classify, qs)))

    out, by_sub = {}, defaultdict(Counter)
    for i, r in enumerate(rows):
        want = _expected(r)
        gots = [p[i] for p in passes]
        ok = all(g == want for g in gots)
        out[r["question"]] = {"want": want, "got": "/".join(sorted(set(gots))), "ok": ok}
        by_sub[want]["ok" if ok else "wrong"] += 1

    correct = sum(v["ok"] for v in out.values())
    nulls = sum("null" in v["got"] for v in out.values())
    print(f"correct subsection: {correct}/{len(out)}   null: {nulls}\n")
    for s in sorted(by_sub, key=lambda x: float(x.split()[1][2:] or 0)):
        c = by_sub[s]
        print(f"  {s:8} {c['ok']:>3}/{c['ok'] + c['wrong']:<3}")
    wrong = Counter((v["want"], v["got"]) for v in out.values() if not v["ok"])
    print("\nmost common confusions (expected -> got):")
    for (w, g), n in wrong.most_common(12):
        print(f"  {w} -> {g}: {n}")
    json.dump({"tag": a.tag, "n": len(out), "correct": correct, "null": nulls,
               "rows": out}, open(f"{a.tag}.json", "w"), indent=1)
    print(f"\nwritten to {a.tag}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
