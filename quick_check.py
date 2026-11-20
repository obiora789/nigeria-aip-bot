"""
quick_check.py — cheap check of the double-meaning judge (disambiguate.py).

Sends each question ONLY to the judge (one small model call each), never
through extraction, retrieval or synthesis. Use it while tuning
JUDGE_SYSTEM; run the full `bash run_all_checks.sh` once before pushing.

Design since 2026-10-26 (option B): for six known bare questions Vannie
answers the usual reading and puts one "Did you mean ...?" button under the
answer. Everything else is answered as typed, with no extra button.

    python quick_check.py                 # 184 targeted questions, 1 pass
    python quick_check.py --stress        # + the 294 stress questions (out-of-sample over-asking)
    python quick_check.py --passes 2      # repeat each question (consistency)
    python quick_check.py --compare checks_quick_20261027_0100/quick_check.csv

What it measures
  DOUBLE     the six bare questions: the judge must name the RIGHT family
             (so the right default is answered and the right button offered).
  SINGLE     questions with one meaning: family must be "none" — answered as
             typed, no extra button. Includes every form that went wrong on
             22 and 26 Oct and the 5 stress questions that asked.
  STRESS     (with --stress) how often ordinary pilot questions get treated
             as a double meaning.
  BUTTON     for each detected double meaning, whether the AIP has the
             alternative at that aerodrome (database read, no model call).

Results go to checks_quick_<date>_<time>/ (quick_check.txt + quick_check.csv).
Token use and approximate cost are measured from the real API responses.
"""
import argparse
import csv
import datetime as _dt
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(level=logging.WARNING)
for _name in ("httpx", "openai", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

import disambiguate  # noqa: E402  (needs the repo on the path: run from the repo root)

# ---------------------------------------------------------------- questions
# Aerodrome names as the full checks use them. Picked to include every
# aerodrome with several errors on 22 Oct (DNSO, DNSK, DNOG, DNGO, DNAI,
# DNZA, DNKT, DNDS) and to avoid the places used as examples inside
# JUDGE_SYSTEM (Yola, Enugu, Benin, Ilorin, Jos), so the test isn't
# simply repeating the prompt back.
AERODROMES = {
    "DNSO": "Sokoto", "DNSK": "Soku", "DNOG": "Ogun", "DNGO": "Gombe",
    "DNAI": "Uyo", "DNZA": "Zaria", "DNKT": "Katsina", "DNAA": "Abuja",
    "DNCA": "Calabar", "DNDS": "Dutse",
}
SMALL = ["DNAA", "DNGO", "DNSO"]
# The other heliports (DNSK is already above): the judge should still name the
# family; the BUTTON column should say "off (heliport ...)".
HELIPORTS = {"DNGB": "Gbaran Ubie", "DNPS": "PHSIA", "DNWI": "Warri Industrial"}   # for templates that already passed 40/40

MUST_ASK = {   # case -> template (the expected family is FAMILY_OF[case])
    "approach-bare": "{X} approach",
    "approach-what": "What is {X} approach",
    "approach-show": "Show me {X} approach",
    "minima":        "minima for {X}",
    "clearance":     "clearance at {X}",
    "lighting":      "lighting at {X}",
    "taxiing":       "taxiing at {X}",
    "charges":       "charges at {X}",
}
FAMILY_OF = {
    "approach-bare": "approach", "approach-what": "approach", "approach-show": "approach",
    "minima": "minima", "clearance": "clearance", "lighting": "lighting",
    "taxiing": "taxiing", "charges": "charges",
}
MUST_CLEAR = {  # the 62 needless asks on 22 Oct came from these five
    "approach-freq":   "{X} approach frequency",
    "approach-freq2":  "frequency of {X} Approach",
    "approach-ils":    "What is the ILS approach in {X}",
    "approach-in":     "What is the approach in {X}",
    "approach-plate":  "approach plate for {X}",
    # failed 80/80 in the full run of 26 Oct
    "approach-vor":    "{X} VOR approach",
    "approach-rnav":   "RNAV approach for {X}",
    "ils-minima":      "ILS minima for {X}",
}
MUST_CLEAR_SMALL = {  # regression guard, 3 aerodromes each
    "approach-minima": "approach minima for {X}",
    "takeoff-minima":  "take-off minima for {X}",
    "vfr-clearance":   "VFR clearance at {X}",
    "rwy-lighting":    "runway lighting at {X}",
    "landing-fee":     "landing fee at {X}",
    "ta":              "transition altitude at {X}",
}
STRESS_FALSE_ASKS = [  # (icao, question) asked on 22 Oct, should be answered
    ("DNKT", "ats units at katsina"),
    ("DNSO", "what is the TWR frequency at Sokoto"),
    ("DNAK", "what is the ats units for Akure?"),
    ("DNBY", "Amassoma ats units"),
    ("DNGO", "gombe ats unit call sign/language(s)"),
    ("DNKT", "obstacle at katsina rwy 05/apch rwy 23/tkof | nitel mast ele"),
]


def targeted():
    rows = []
    for icao, name in AERODROMES.items():
        for case, t in MUST_ASK.items():
            rows.append(("ASK", case, icao, t.format(X=name)))
        for case, t in MUST_CLEAR.items():
            rows.append(("CLEAR", case, icao, t.format(X=name)))
    for icao in SMALL:
        for case, t in MUST_CLEAR_SMALL.items():
            rows.append(("CLEAR", case, icao, t.format(X=AERODROMES[icao])))
    for icao, name in HELIPORTS.items():
        for case in ("approach-bare", "approach-what"):
            rows.append(("ASK", case, icao, MUST_ASK[case].format(X=name)))
    for icao, q in STRESS_FALSE_ASKS:
        rows.append(("CLEAR", "stress-false-ask", icao, q))
    return rows


def stress_rows(path):
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            q = (r.get("question") or "").strip()
            if not q:
                continue
            exp = (r.get("expected_behaviour") or "answer").strip()
            # 'clarify' rows: asking is acceptable, so only 'answer' rows count
            out.append(("STRESS-" + exp.upper(), r.get("subsection", ""), r.get("icao", ""), q))
    return out


# ------------------------------------------------------------ token meter
class Meter:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls = self.prompt = self.cached = self.completion = self.reasoning = 0
        self.ok = False
        self.served = set()     # the model snapshot(s) OpenAI actually answered with

    def add(self, usage):
        if usage is None:
            return
        with self.lock:
            self.calls += 1
            self.prompt += getattr(usage, "prompt_tokens", 0) or 0
            self.completion += getattr(usage, "completion_tokens", 0) or 0
            det = getattr(usage, "prompt_tokens_details", None)
            self.cached += (getattr(det, "cached_tokens", 0) or 0) if det else 0
            cdet = getattr(usage, "completion_tokens_details", None)
            self.reasoning += (getattr(cdet, "reasoning_tokens", 0) or 0) if cdet else 0


METER = Meter()


def install_meter():
    """Wrap the client call the judge uses so every response's usage is counted.
    If the judge uses a different client, cost is reported as 'not measured'."""
    try:
        import agent
        comp = agent.client.beta.chat.completions
        real = comp.parse

        def parse(*a, **kw):
            resp = real(*a, **kw)
            METER.add(getattr(resp, "usage", None))
            served = getattr(resp, "model", None)
            if served:
                with METER.lock:
                    METER.served.add(str(served))
            return resp

        comp.parse = parse
        METER.ok = True
    except Exception as e:  # never let metering break the check
        print(f"(token meter not installed: {e})")


# USD per 1M tokens: (input, cached input, output). Reasoning tokens bill as
# output. Check current prices before relying on these.
PRICES = {
    "gpt-4o-mini": (0.15, 0.075, 0.60),
    "gpt-6-luna": (0.10, 0.01, 0.50),
}


def cost_line():
    if not METER.calls:
        return "tokens: not measured"
    model = ""
    try:
        import config
        model = str(getattr(config, "EXTRACTION_MODEL", ""))
        effort = config.reasoning_effort_for(model, "extraction") \
            if hasattr(config, "reasoning_effort_for") else None
    except Exception:
        effort = None
    s = (f"tokens: {METER.calls} calls, {METER.prompt:,} prompt "
         f"({METER.cached:,} cached), {METER.completion:,} completion "
         f"({METER.reasoning:,} reasoning)")
    price = next((v for k, v in PRICES.items() if model.startswith(k)), None)
    if price:
        inp, cached, out = price
        usd = ((METER.prompt - METER.cached) * inp + METER.cached * cached
               + METER.completion * out) / 1e6
        s += f"  ~= ${usd:.3f} at list prices"
    elif model:
        s += f"  (model {model}: work out cost from its price)"
    # Proof of the pin: the snapshot OpenAI actually answered with.
    s += (f"\nmodel configured: {model or '?'}" + (f" [effort {effort}]" if effort else "")
          + f"; served: {', '.join(sorted(METER.served)) or '?'}")
    return s


# --------------------------------------------------------------- judging
def _label(o):
    if isinstance(o, dict):
        return str(o.get("label") or o.get("question") or o)
    return str(getattr(o, "label", None) or getattr(o, "question", None) or o)


def _button(family, icao):
    """Would the "Did you mean" button be shown at this aerodrome?"""
    try:
        if hasattr(disambiguate, "applies_here") and not disambiguate.applies_here(family, icao):
            return "off (heliport: answered as typed)"
        return "yes" if disambiguate.alternative_exists(family, icao) else "no (not in AIP)"
    except Exception as e:                       # noqa: BLE001
        return f"? ({type(e).__name__})"


def run_one(q, icao=""):
    """-> (result, why, labels, button). result is a family name, 'none' or ERROR."""
    try:
        chk = disambiguate.judge(q)
        if chk is None:
            return "ERROR", "judge returned None (API failure)", [], ""
        why = "topic={!r} family={} place={!r}".format(
            getattr(chk, "topic", "?"), getattr(chk, "family", "?"),
            getattr(chk, "place", "?"))
        got = disambiguate.decide(chk)
        if not got:
            return "none", why, [], ""
        family, place = got
        labels = [o["label"] for o in disambiguate.readings(family, place)]
        return family, why, labels, _button(family, icao or None)
    except Exception as e:
        return "ERROR", f"{type(e).__name__}: {e}", [], ""


def grade(expect, case, result):
    if result == "ERROR":
        return "ERROR"
    if expect == "ASK":
        return "OK" if result == FAMILY_OF.get(case) else "WRONG"
    if expect == "CLEAR":
        return "OK" if result == "none" else "WRONG"
    if expect == "STRESS-ANSWER":
        return "OK" if result == "none" else "REVIEW"
    return "OK"  # STRESS-CLARIFY: either is acceptable


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stress", nargs="?", const="auto", default=None,
                    help="also judge the stress questions (default file: quick_stress_294.csv)")
    ap.add_argument("--passes", type=int, default=1)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--compare", default=None, help="an earlier quick_check.csv")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    rows = targeted()
    if a.stress:
        path = a.stress
        if path == "auto":
            here = os.path.dirname(os.path.abspath(__file__))
            path = next((p for p in (os.path.join(here, "quick_stress_294.csv"),
                                     "quick_stress_294.csv") if os.path.exists(p)), None)
            if not path:
                sys.exit("quick_stress_294.csv not found; pass --stress <csv>")
        rows += stress_rows(path)

    jobs = [(p, r) for p in range(1, a.passes + 1) for r in rows]
    print(f"Judging {len(jobs)} questions ({len(rows)} x {a.passes} pass(es)) ...")
    install_meter()

    done = [0]
    lock = threading.Lock()

    def work(job):
        p, (expect, case, icao, q) = job
        res, why, labels, button = run_one(q, icao)
        with lock:
            done[0] += 1
            if done[0] % 25 == 0 or done[0] == len(jobs):
                print(f"  {done[0]}/{len(jobs)}")
        return {"pass": p, "expect": expect, "case": case, "icao": icao, "question": q,
                "result": res, "verdict": grade(expect, case, res),
                "offered": " | ".join(labels), "button": button, "why": why}

    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        out = list(ex.map(work, jobs))

    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M")
    odir = a.out or f"checks_quick_{stamp}"
    os.makedirs(odir, exist_ok=True)
    with open(os.path.join(odir, "quick_check.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)

    lines = report(out, a.passes, a.compare)
    lines.append("")
    lines.append(cost_line())
    text = "\n".join(lines)
    print("\n" + text)
    with open(os.path.join(odir, "quick_check.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(f"\nwritten to {odir}/")


def heliport_report():
    """Which of the published aerodromes the heliport rule picks (database
    reads only, no model calls). Compare with ad216_extractor.py, which names
    DNGB, DNPS, DNSK and DNWI as the dedicated heliports."""
    if not hasattr(disambiguate, "is_heliport"):
        return []
    try:
        import resolver
        icaos = sorted(resolver.AERODROMES)
    except Exception as e:                       # noqa: BLE001
        return ["", f"HELIPORTS: could not list aerodromes ({type(e).__name__})"]
    found = [i for i in icaos if disambiguate.is_heliport(i)]
    expected = {"DNGB", "DNPS", "DNSK", "DNWI"}
    L = ["", f"HELIPORTS by the stored-AIP rule (no AD 2.7, 2.15, 2.8 text): "
             f"{', '.join(found) or 'none'}  ({len(found)}/{len(icaos)})"]
    if set(found) != expected:
        L.append(f"  differs from ad216_extractor.py's list {sorted(expected)}: "
                 f"extra {sorted(set(found) - expected)}, missing {sorted(expected - set(found))}")
    return L


def report(out, passes, compare):
    L = []
    by_case = {}
    for r in out:
        if r["expect"] in ("ASK", "CLEAR"):
            by_case.setdefault((r["expect"], r["case"]), []).append(r)
    ask_ok = sum(r["verdict"] == "OK" for r in out if r["expect"] == "ASK")
    ask_n = sum(r["expect"] == "ASK" for r in out)
    clr_ok = sum(r["verdict"] == "OK" for r in out if r["expect"] == "CLEAR")
    clr_n = sum(r["expect"] == "CLEAR" for r in out)
    L.append(f"DOUBLE {ask_ok}/{ask_n}   (bare double-meaning question -> right family: "
             f"usual answer + one 'Did you mean' button)")
    L.append(f"SINGLE {clr_ok}/{clr_n}   (one meaning -> answered as typed, no extra button)")
    L.append("")
    L.append("want   case               correct")
    for (exp, case), rs in by_case.items():
        ok = sum(r["verdict"] == "OK" for r in rs)
        flag = "" if ok == len(rs) else "     <-- check"
        L.append(f"{('DOUBLE' if exp == 'ASK' else 'SINGLE'):<6} {case:<18} {ok:>3}/{len(rs):<3}{flag}")

    st = [r for r in out if r["expect"] == "STRESS-ANSWER"]
    if st:
        L.append("")
        asked = [r for r in st if r["result"] not in ("none", "ERROR")]
        L.append(f"STRESS (answer rows): treated as a double meaning on {len(asked)}/{len(st)} "
                 f"({100 * len(asked) / len(st):.1f}%). Each should be a bare question "
                 f"of one of the six kinds.")
        for r in asked:
            L.append(f"   [{r['case']}] '{r['question']}': {r['offered']}")

    errs = [r for r in out if r["verdict"] == "ERROR"]
    wrong = [r for r in out if r["verdict"] == "WRONG"]
    if wrong:
        L.append("")
        L.append(f"WRONG ({len(wrong)}):")
        for r in wrong:
            if r["expect"] == "CLEAR":
                what = f"treated as double meaning ({r['result']})"
            elif r["result"] == "none":
                what = f"not recognised (wanted {FAMILY_OF.get(r['case'])})"
            else:
                what = f"wrong family {r['result']} (wanted {FAMILY_OF.get(r['case'])})"
            L.append(f"  [{r['icao']}] '{r['question']}' — {what}")
            if r["offered"]:
                L.append(f"        offered: {r['offered']}")
            if r["why"]:
                L.append(f"        judge: {r['why']}")
    if errs:
        L.append("")
        L.append(f"ERRORS ({len(errs)}) — not counted as right or wrong:")
        for r in errs[:10]:
            L.append(f"  '{r['question']}': {r['why']}")

    nob = [r for r in out if r["expect"] == "ASK" and r["verdict"] == "OK"
           and not str(r.get("button", "")).startswith("yes")]
    if nob:
        L.append("")
        L.append(f"BUTTON LEFT OFF ({len(nob)}) — heliport, the AIP has no alternative here, "
                 f"or the database could not be read. Check each:")
        for r in nob:
            L.append(f"  [{r['icao']}] '{r['question']}': {r['button']}")

    L.extend(heliport_report())

    if passes > 1:
        seen = {}
        for r in out:
            seen.setdefault(r["question"], set()).add(r["result"])
        flaky = [q for q, s in seen.items() if len(s - {"ERROR"}) > 1]
        L.append("")
        L.append(f"INCONSISTENT across passes: {len(flaky)}")
        for q in flaky[:20]:
            L.append(f"  '{q}'")

    if compare and os.path.exists(compare):
        prev = {}
        with open(compare, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("pass", "1") == "1":
                    prev[r["question"]] = r
        now = {r["question"]: r for r in out if r["pass"] == 1}
        better = [q for q in now if q in prev and prev[q]["verdict"] != "OK" and now[q]["verdict"] == "OK"]
        worse = [q for q in now if q in prev and prev[q]["verdict"] == "OK" and now[q]["verdict"] != "OK"]
        L.append("")
        L.append(f"vs {compare}: {len(better)} fixed, {len(worse)} broken")
        for q in worse:
            L.append(f"  BROKEN '{q}' ({prev[q]['result']} -> {now[q]['result']})")
        for q in better:
            L.append(f"  fixed  '{q}' ({prev[q]['result']} -> {now[q]['result']})")
    return L


if __name__ == "__main__":
    main()
