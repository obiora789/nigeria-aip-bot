#!/usr/bin/env python3
"""
check_fields.py — does the model set one extraction field as expected?

Car 5 replaces wording rules ("fir" in the text) with the model's own fields.
Each replacement is measured here before it is pushed: a CSV of questions,
the field, and the expected value. One extraction call per question (cents).

CSV columns: question, field, expected   (expected: true / false / a value / blank for null)

    python checks/check_fields.py checks/fields_d18.csv
"""
import sys as _sys, pathlib as _pl  # repo layout: bot at the top, scripts in folders
_R = next(d for d in _pl.Path(__file__).resolve().parents if (d / "main.py").exists())
_sys.path[1:1] = [str(p) for p in (_R, _R / "checks", _R / "ingestion", _R / "ingestion" / "validate")
                  if str(p) not in _sys.path]
import csv
import sys
from collections import defaultdict

from agent import extract_query_parameters


def _want(raw):
    raw = (raw or "").strip()
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    return raw or None


def _same(got, want):
    if isinstance(want, bool):
        return bool(got) is want
    if want is None:
        return got in (None, "")
    return str(got or "").strip().casefold() == want.casefold()


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    rows = list(csv.DictReader(open(argv[0], encoding="utf-8")))
    score, wrong = defaultdict(lambda: [0, 0]), []
    for r in rows:
        ex = extract_query_parameters(r["question"])
        got = getattr(ex, r["field"], None)
        want = _want(r["expected"])
        key = (r["field"], str(want))
        score[key][1] += 1
        if _same(got, want):
            score[key][0] += 1
        else:
            wrong.append((r["field"], r["question"], want, got))
    print(f"{len(rows)} questions\n")
    for (field, want), (ok, n) in sorted(score.items()):
        print(f"  {field:<24} expected {want:<6} {ok}/{n}")
    print(f"\nwrong ({len(wrong)}):")
    for field, q, want, got in wrong:
        print(f"  [{field}] {q!r}: expected {want!r}, got {got!r}")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
