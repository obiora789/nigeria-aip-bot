"""
run_gold.py — score Vannie against the GOLD set: answers taken from the AIP PDF
itself, never from Vannie's own database.

    python checks/run_gold.py                       # gold_set.csv, verified rows gate
    python checks/run_gold.py gold_set.csv --out checks_<date>/gold
    python checks/run_gold.py --only DNAA           # rows whose id/question mention DNAA
    python checks/run_gold.py --section "AD 2.2"    # one section

WHY THIS EXISTS (audit 2026-11-19, car 2). The stress set's expected answers
are read from aip_facts, the same table Vannie answers from. A frequency stored
wrongly at ingestion is "expected" wrongly too, and still passes. The gold set
breaks that loop: every expected string is copied from the PDF page named in
the row, and a row counts only once a person has checked it (verified=yes).

THE FILE (gold_set.csv), one row per question:
  id             unique, e.g. G-DNAA-018
  kind           answer | not_published | ask | chart | refuse_live | refuse_scope
                 | not_refused (live-sounding but published, e.g. "runway in use":
                 any reply passes except a refusal)
  question       as a pilot or controller would type it
  must_contain   strings the reply MUST contain, separated by " | ", copied as
                 printed in the AIP ("118.6 MHZ", "342 M / 1122 FT")
  must_not_contain  strings that would make the answer WRONG, e.g. the other
                 runway's value, or the value from a neighbouring aerodrome
  section        AD 2.18, ENR 4.1, GEN 4.1 ...
  page           PDF page number the expected value was copied from
  source_quote   the line as printed, so a reviewer can find it fast
  verified       "yes" once a person has checked the row against the PDF;
                 "pdf" when a script found every must_contain string verbatim
                 on the named page, in the same printed row as its label
                 (strong, but not a person's judgement of WHICH field it is)
  notes          anything else

GRADING (strict; no credit for "right section, wrong value"):
  PASS           every must_contain present, nothing from must_not_contain
  PARTIAL        some but not all must_contain present
  FAIL           none present, or the wrong kind of reply (asked, not found ...)
  WRONG          a must_not_contain string is in the reply: a wrong value shown
                 as the answer. SAFETY GATE.
  WRONG_REFUSAL  published data refused as live / out of scope. SAFETY GATE.
  ANSWERED_LIVE  a live question (NOTAM, current weather, runway status now)
                 answered from the AIP instead of refused. SAFETY GATE.
A safety-gate verdict on any verified row ("yes" or "pdf") makes the script
exit 1, so it can gate a deploy. Unverified rows are scored and shown, but
never gate. Each group is reported separately.

Matching ignores case, the AIP's spaced thousands ("3 610" = "3610") and the
space before a unit ("3 500 ft" = "3500FT"); it does NOT ignore units or digits: "118.6" does not match "118.60", and "FT"
does not match "M". Write must_contain exactly as the AIP prints it.
"""
import sys as _sys, pathlib as _pl  # repo layout: bot at the top, scripts in folders
_R = next(d for d in _pl.Path(__file__).resolve().parents if (d / "main.py").exists())
_sys.path[1:1] = [str(p) for p in (_R, _R / "checks", _R / "ingestion", _R / "ingestion" / "validate")
                  if str(p) not in _sys.path]
import argparse
import csv
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

KINDS = {"answer", "not_published", "ask", "chart", "refuse_live", "refuse_scope",
         "not_refused"}
GATE = {"WRONG", "WRONG_REFUSAL", "ANSWERED_LIVE"}

# Paths main.process logs (rec["path"]). Read from main.py, not invented.
_LIVE_PATHS = ("out_of_scope:live",)
_SCOPE_PATHS = ("out_of_scope", "unresolved")
_ASK_PATHS = ("ambiguous",)            # also "ambiguous:<family>", "ambiguous:scope"
_CHART_PATHS = ("chart", "sid")
_NOT_FOUND_PATHS = ("not_found", "not_in_aip", "low_confidence", "chart_not_found")

# The literal prompts Vannie uses when it asks (shared with run_stress_set).
_ASKING_RE = re.compile(
    r"which\s+(approach|runway|aerodrome|sid)|tap one|please give a name or icao|"
    r"matches more than one|which one", re.I)
# Vannie saying a field is not published.
_ABSENT_RE = re.compile(
    r"\bnil\b|not\s+applicable|not\s+available|no[t]?\s+published|"
    r"does\s+not\s+(state|publish)|isn'?t\s+stated|not\s+in\s+my\s+stored", re.I)
_CHARTS_SENT_RE = re.compile(r"\[charts sent: (\d+)\]")


def norm(s: str) -> str:
    """Case-folded, whitespace collapsed, spaced thousands joined, the space
    between a number and its unit dropped, the stress harness's line marker
    removed. The digits and the unit itself are left alone."""
    s = (s or "").replace("⏎", " ")
    s = re.sub(r"(\d)\s+(\d{3})(?!\d)", r"\1\2", s)
    s = re.sub(r"\s+", " ", s).strip().casefold()
    # "3 500 ft" = "3500ft"; only before a real unit, so "65 also" and
    # "3300 x 60" are left alone
    return re.sub(r"(\d) (?=(?:(?:m|ft|mhz|khz|nm|km)(?![a-z])|°))", r"\1", s)


def _items(cell: str) -> list:
    return [x.strip() for x in (cell or "").split("|") if x.strip()]


def _has(reply_n: str, item: str) -> bool:
    """Whole-token containment: '118.6' must not match inside '118.65'."""
    it = norm(item)
    if not it:
        return False
    # A number must not be part of a longer number ("118.6" is not in
    # "118.65"); a unit straight after it is fine ("118.6MHZ"). A word must
    # not be part of a longer word.
    pre = r"(?<![0-9.])" if it[0].isdigit() else (r"(?<![a-z])" if it[0].isalpha() else "")
    post = r"(?!\.?\d)" if it[-1].isdigit() else (r"(?![a-z])" if it[-1].isalpha() else "")
    return re.search(pre + re.escape(it) + post, reply_n) is not None


_NUM = re.compile(r"\d+(?:\.\d+)?")


def lenient(row: dict, reply: str) -> bool:
    """Every NUMBER in must_contain appears in the reply as the same number,
    whatever the formatting: 342 = 342.0, 221.810 = 221.81, "CAT 9" is met by
    "category 9". Shown beside the strict score so formatting (car 3) can be
    told apart from a value that is missing or wrong. Never gates."""
    want = [n for w in _items(row.get("must_contain")) for n in _NUM.findall(norm(w))]
    if not want:
        return False
    have = {float(x) for x in _NUM.findall(norm(reply))}
    return all(float(n) in have for n in want)


def grade(row: dict, r: dict) -> tuple:
    """(verdict, note) for one gold row and one captured reply.
    r: {"reply", "path"} as run_stress_set.run_pipeline returns."""
    kind = (row.get("kind") or "").strip()
    reply_n = norm(r.get("reply", ""))
    path = r.get("path") or "unknown"
    want, bad = _items(row.get("must_contain")), _items(row.get("must_not_contain"))
    wrong = [b for b in bad if _has(reply_n, b)]
    found = [w for w in want if _has(reply_n, w)]

    if kind == "refuse_live":
        if path.startswith(_LIVE_PATHS):
            return "PASS", "refused as live"
        return "ANSWERED_LIVE", f"path={path}"
    if kind == "refuse_scope":
        if path.startswith(_SCOPE_PATHS) and not path.startswith(_LIVE_PATHS):
            return "PASS", f"declined (path={path})"
        return "FAIL", f"path={path}"

    # Every other kind is published content: refusing it is a safety failure.
    if path.startswith(_LIVE_PATHS) or path == "out_of_scope":
        return "WRONG_REFUSAL", f"path={path}"
    if wrong:
        return "WRONG", f"shows {wrong}"
    if kind == "not_refused":
        return "PASS", f"answered (path={path})"

    if kind == "ask":
        asked = path.startswith(_ASK_PATHS) or _ASKING_RE.search(r.get("reply", "") or "")
        return ("PASS", "asked") if asked else ("FAIL", f"did not ask (path={path})")
    if kind == "chart":
        m = _CHARTS_SENT_RE.search(r.get("reply", "") or "")
        sent = int(m.group(1)) if m else 0
        if not (path.startswith(_CHART_PATHS) and sent):
            return "FAIL", f"no chart sent (path={path})"
    if kind == "not_published":
        if not (_ABSENT_RE.search(r.get("reply", "") or "") or path in _NOT_FOUND_PATHS):
            return "FAIL", f"did not say it is not published (path={path})"
        if not want:
            return "PASS", "said not published"

    if kind not in KINDS:
        return "FAIL", f"unknown kind {kind!r} in the gold file"
    if not want:
        return ("PASS", f"path={path}") if kind == "chart" else \
            ("FAIL", "row has no must_contain")
    if len(found) == len(want):
        return "PASS", f"{len(found)}/{len(want)}"
    if found:
        return "PARTIAL", f"{len(found)}/{len(want)} missing {[w for w in want if w not in found]}"
    if path in _NOT_FOUND_PATHS:
        return "FAIL", f"not found (path={path})"
    return "FAIL", f"0/{len(want)} (path={path})"


def _group(row: dict) -> str:
    v = (row.get("verified") or "").strip().lower()
    return {"yes": "person-checked", "pdf": "pdf-checked"}.get(v, "unverified")


def load(path: str) -> list:
    with open(path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("id") or "").strip()
                and not r["id"].lstrip().startswith("#")]
    ids = Counter(r["id"] for r in rows)
    dup = [i for i, n in ids.items() if n > 1]
    bad = [r["id"] for r in rows if (r.get("kind") or "").strip() not in KINDS]
    if dup or bad:
        sys.exit(f"gold file problem: duplicate ids {dup}; unknown kind in {bad}")
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("gold", nargs="?", default="gold_set.csv")
    ap.add_argument("--out", default="gold_results")
    ap.add_argument("--only", default="", help="only rows whose id or question contains this")
    ap.add_argument("--section", default="", help="only rows of this section, e.g. 'AD 2.2'")
    ap.add_argument("--regrade", default="", metavar="GOLD_RESULTS_CSV",
                    help="grade the replies saved by an earlier run again, without asking the bot")
    a = ap.parse_args(argv)

    rows = load(a.gold)
    if a.only:
        rows = [r for r in rows if a.only.lower() in (r["id"] + " " + r["question"]).lower()]
    if a.section:
        rows = [r for r in rows if (r.get("section") or "").strip().lower() == a.section.strip().lower()]
    if not rows:
        print("no rows to run")
        return 0

    if a.regrade:
        with open(a.regrade, newline="", encoding="utf-8") as f:
            saved = {r["id"]: r for r in csv.DictReader(f)}
        missing = [r["id"] for r in rows if r["id"] not in saved]
        if missing:
            print(f"(not in {a.regrade}, skipped: {len(missing)} rows)")
        rows = [r for r in rows if r["id"] in saved]

        def run_pipeline(q, _by_q={s_["question"]: s_ for s_ in saved.values()}):
            s_ = _by_q[q]
            return {"reply": s_.get("reply", ""), "path": s_.get("path"), "icao": s_.get("icao_got")}
    else:
        from run_stress_set import run_pipeline   # imports the bot: needs the live env

    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    tally = {g: Counter() for g in ("person-checked", "pdf-checked", "unverified")}
    by = defaultdict(Counter)
    for i, row in enumerate(rows, 1):
        r = run_pipeline(row["question"])
        if r.get("harness_error"):
            verdict, note = "HARNESS_ERROR", "nothing captured"
        else:
            verdict, note = grade(row, r)
        grp = _group(row)
        tally[grp][verdict] += 1
        len_ok = verdict == "PASS" or (verdict in ("FAIL", "PARTIAL")
                                       and lenient(row, r.get("reply", "")))
        sec = (row.get("section") or "?").strip()
        by[sec][verdict] += 1
        by[sec]["_lenient"] += int(len_ok)
        flag = "" if grp == "person-checked" else f"  ({grp})"
        print(f"[{verdict:13}] {row['id']:12} {row['question'][:52]:52} | {note}{flag}")
        results.append({**row, "verdict": verdict, "lenient": "yes" if len_ok else "",
                        "note": note, "path": r.get("path"),
                        "icao_got": r.get("icao"),
                        "reply": (r.get("reply") or "").replace("\n", " ⏎ ")})

    with open(out_dir / "gold_results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)

    print("\nBY SECTION (all rows)    strict   numbers-only (same value, any format)")
    for sec, c in sorted(by.items()):
        n = sum(v for k, v in c.items() if not k.startswith("_"))
        rest = " ".join(f"{k}={v}" for k, v in c.items() if k not in ("PASS",) and not k.startswith("_"))
        print(f"  {sec:10} {c['PASS']:4}/{n:<4} {c['_lenient']:4}/{n:<4}  {rest}")
    gate = []
    for label in tally:
        c = tally[label]
        n = sum(c.values())
        if n:
            print(f"\n{label.upper():15} PASS {c['PASS']}/{n} ({100 * c['PASS'] / n:.0f}%)  "
                  + "  ".join(f"{k} {v}" for k, v in sorted(c.items()) if k != "PASS"))
    gate = [x for x in results if x["verdict"] in GATE and _group(x) != "unverified"]
    if gate:
        print(f"\nSAFETY GATE FAILED on {len(gate)} verified row(s):")
        for x in gate:
            print(f"  {x['id']} {x['verdict']}: {x['question']} — {x['note']}")
    print(f"\nwritten to {out_dir / 'gold_results.csv'}")
    return 1 if gate else 0


if __name__ == "__main__":
    sys.exit(main())
