#!/usr/bin/env python3
"""
eval_set.py — score Vannie against a ground-truth CSV (vannie_test_set.csv).

Columns expected: id, category, question, ground_truth, test_type.

Vannie answers VERBATIM from the AIP (no LLM synthesis), so the fair automated
metric is *fact recall*: do the ground-truth values appear in the returned text /
chart? Cases that need human judgement (must-not-invent, computed comparisons,
cross-aerodrome enumeration) are flagged REVIEW, not auto-graded.

Verdicts: PASS (all key facts present / correct path), PARTIAL (some facts),
FAIL (none / wrong path), REVIEW (needs a human). Also flags PLATE leaks.

Usage:
  python eval_set.py [path/to/vannie_test_set.csv]   # default ./vannie_test_set.csv
Writes vannie_eval_results.csv for manual review of PARTIAL/FAIL/REVIEW rows.
Cost: ~63 gpt-4o-mini extractions + embeddings. Cents. Hits LIVE OpenAI+Supabase.
"""
import csv
import re
import sys

from e2e import PLATE_MARKERS, run_pipeline

NUM = re.compile(r"\d+(?:\.\d+)?")
SECTIONS = re.compile(r"\b(?:AD|ENR|GEN)\s?\d+(?:\.\d+)?\b", re.I)
ICAO = re.compile(r"\bDN[A-Z]{2}\b")
KEYWORDS = ["NIL", "Not Available", "not specified", "Jet A1", "Jet A-1",
            "CAT 9", "CAT 1", "CAT II", "CAT III", "NCAA", "NAMA", "NOTAM",
            "Kano FIR"]


def _has_num(reply: str, n: str) -> bool:
    return re.search(rf"(?<!\d){re.escape(n)}(?!\d)", reply) is not None


def _plate_leak(reply: str) -> bool:
    up = reply.upper()
    return any(m.upper() in up for m in PLATE_MARKERS)


def score(row: dict, r: dict) -> tuple[str, str]:
    gt, reply, tt = row["ground_truth"], r["reply"], row["test_type"]
    up = reply.upper()

    if tt == "out_of_scope":
        ok = r["path"] in ("out_of_scope", "abstain", "unresolved")
        return ("PASS" if ok else "FAIL", f"path={r['path']}")

    if tt == "multimodal":  # chart requests
        want = (ICAO.findall(gt) or [None])[0]
        got_chart = r["path"] == "chart" and len(r["charts"]) > 0
        icao_ok = want is None or r["icao"] == want or \
            any(c.icao_code == want for c in r["charts"])
        v = "PASS" if got_chart and icao_ok else ("PARTIAL" if got_chart else "FAIL")
        return (v, f"path={r['path']} charts={len(r['charts'])} icao={r['icao']} want={want}")

    if tt == "mapping":  # ICAO <-> city
        toks = ICAO.findall(gt) + re.findall(r"[A-Z][a-z]{2,}", gt)
        present = [t for t in toks if t.upper() in up]
        return ("PASS" if present else "FAIL", f"matched={present}")

    if tt == "structure":
        secs = SECTIONS.findall(gt)
        cited = " ".join((x.aip_section or "") for x in r["results"]).upper()
        sec_hit = [s for s in secs if s.upper() in up or s.upper() in cited]
        kw = [k for k in ("NCAA", "NAMA", "Kano FIR", "DNKK") if k.upper() in up]
        if sec_hit or kw:
            return ("PASS", f"sections={sec_hit} kw={kw}")
        return ("REVIEW" if r["path"] == "answer" else "FAIL",
                f"path={r['path']} (expected {secs})")

    if tt == "currency":
        if "03/2026" in reply or "AMDT 03" in up:
            return ("PASS", "AIRAC 03/2026 present")
        if "NOTAM" in up:
            return ("PASS", "NOTAM flagged")
        if "2026" in reply:
            return ("PASS", "2026 present")
        return ("FAIL", "no currency marker")

    if tt == "no_hallucination":
        low = gt.lower()
        # A faithful abstention IS the correct answer when the AIP doesn't state it.
        if r["path"] in ("not_in_aip", "abstain"):
            return ("PASS", f"faithful abstention (path={r['path']})")
        if any(s in low for s in ("invent", "no padding", "only what", "must not")):
            return ("REVIEW", "judgement: must-not-invent / no-padding")
        exp = [k for k in KEYWORDS if k.upper() in gt.upper()]
        present = [k for k in exp if k.upper() in up]
        if exp:
            return ("PASS" if present else "FAIL", f"expected={exp} present={present}")
        return ("REVIEW", "no clear marker to auto-check")

    # retrieval / reasoning / nl_robustness -> numeric + keyword recall
    if r["path"] == "not_in_aip":
        return ("FAIL", "abstained but a value was expected")
    gnums = NUM.findall(gt)
    matched = [n for n in gnums if _has_num(reply, n)]
    kw = [k for k in KEYWORDS if k.upper() in gt.upper() and k.upper() in up]
    if gnums:
        frac = len(matched) / len(gnums)
        v = "PASS" if frac == 1 else ("PARTIAL" if frac > 0 else "FAIL")
    elif kw:
        v = "PASS"
    else:
        v = "REVIEW" if r["path"] == "answer" else "FAIL"
    note = f"nums={len(matched)}/{len(gnums)} kw={kw}"
    if tt == "reasoning":
        note += " | synthesis NOT computed (verbatim) — facts only"
    return (v, note)


def main(path: str) -> int:
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))

    print("=" * 100)
    print(f"VANNIE EVALUATION — {len(rows)} cases from {path}")
    print("=" * 100)

    out_rows = []
    tally = {"PASS": 0, "PARTIAL": 0, "FAIL": 0, "REVIEW": 0}
    by_cat = {}
    leaks = []

    for row in rows:
        r = run_pipeline(row["question"])
        verdict, note = score(row, r)
        if _plate_leak(r["reply"]):
            note += "  ⚠PLATE-LEAK"
            leaks.append(row["id"])
        tally[verdict] = tally.get(verdict, 0) + 1
        by_cat.setdefault(row["category"], {"PASS": 0, "PARTIAL": 0, "FAIL": 0, "REVIEW": 0})
        by_cat[row["category"]][verdict] += 1
        print(f"[{verdict:7}] {row['id']:4} {row['category']:13} | "
              f"{row['question'][:46]:46} | {note}")
        out_rows.append({
            "id": row["id"], "category": row["category"], "test_type": row["test_type"],
            "question": row["question"], "ground_truth": row["ground_truth"],
            "verdict": verdict, "note": note, "icao": r["icao"], "path": r["path"],
            "sim": f"{r['sim']:.3f}", "charts": len(r["charts"]),
            "reply": r["reply"].replace("\n", " ⏎ ")[:600],
        })

    print("-" * 100)
    print("BY CATEGORY:")
    for cat, t in sorted(by_cat.items()):
        print(f"  {cat:14} P:{t['PASS']:2} part:{t['PARTIAL']:2} "
              f"F:{t['FAIL']:2} rev:{t['REVIEW']:2}")
    print("-" * 100)
    print(f"TOTAL  PASS {tally['PASS']}  PARTIAL {tally['PARTIAL']}  "
          f"FAIL {tally['FAIL']}  REVIEW {tally['REVIEW']}")
    if leaks:
        print(f"⚠ PLATE-TEXT LEAKS in: {', '.join(leaks)}  "
              f"(chart pages still in the text corpus — see DELETE/heuristic fix)")

    with open("vannie_eval_results.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)
    print("\nWrote vannie_eval_results.csv (review PARTIAL/FAIL/REVIEW rows).")
    return 1 if (tally["FAIL"] or leaks) else 0


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "vannie_test_set.csv")
