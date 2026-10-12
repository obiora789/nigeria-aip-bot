#!/usr/bin/env python3
"""
measure_intent.py — measure the extraction prompt, and only the extraction
prompt, before and after changing it.

WHY THIS AND NOT run_stress_set.py
----------------------------------
The out_of_scope failures live entirely in agent.extract_query_parameters().
Vannie never searches, so retrieval, routing and rendering are irrelevant to
them. Running the full pipeline to measure a classifier costs an embedding and
several DB round-trips per question and buries the signal in unrelated
failures. This calls the extractor and nothing else: one cheap LLM call per
question, and the only thing measured is the thing being changed.

MEASURE BOTH DIRECTIONS — THIS IS THE POINT
-------------------------------------------
A prompt change that stops false refusals is trivially easy to get wrong: tell
the model "don't refuse" and false refusals go to zero while Vannie starts
answering questions about London weather and fuel prices. That is a far worse
bot, and a one-sided metric would score it as an improvement.

So three populations, scored separately and never averaged:

  IN SCOPE      every question in the stress set. These are generated FROM
                aip_facts, so by construction the AIP publishes the answer.
                Refusing one is a FALSE REFUSAL.
  BOUNDARY      published data that SOUNDS live or priced: a frequency or TA
                asked "now"/"current"/"today", the runway in use (AD 2.22.2),
                tower hours, landing-forecast type, the VFR clearance rule,
                GEN 4 charges. The AIP holds information of lasting
                character; a time word does not make a published fact live.
                These sit right next to a refusal rule, which is exactly where
                a model over-refuses, and the stress set holds almost none of
                them (zero charges questions). Refusing one is a BOUNDARY
                REFUSAL, reported on its own line.
  OUT OF SCOPE  the control set below: live state, observations, clearances,
                unpublished prices, foreign airports. Answering one is a
                FALSE ACCEPT.

A change is only an improvement if false refusals fall AND false accepts do
not rise. Report all three, always.

USAGE
    python measure_intent.py stress_set_clean.csv --limit 300 --tag before
    # ...edit agent._SYSTEM...
    python measure_intent.py stress_set_clean.csv --limit 300 --tag after
    python measure_intent.py --compare before.json after.json

A JSON written by an older copy of this script has no boundary figure; the
comparison shows it as "n/a" rather than inventing a zero. Re-run both tags
with this copy to compare like with like — the control set changed too.
"""
import argparse
import csv
import json
import random
import sys
from collections import Counter, defaultdict

try:
    from agent import extract_query_parameters
    import resolver
except ImportError as e:                             # noqa: BLE001
    print(f"Cannot import agent.py: {e}. Run from the repo root with the venv active.")
    sys.exit(2)

# Genuinely out-of-scope questions, drawn from the categories config.OUT_OF_SCOPE
# names: live/real-time information, commercial data the AIP does not publish,
# and airports outside Nigeria. If a prompt change makes any of these answerable,
# it has traded a real safety property for a metric.
#
# "what is the landing fee at Kano" was REMOVED: GEN 4.1 publishes landing fees
# (one national FAAN schedule), so refusing it was the bug, not the control.
# It moved to BOUNDARY_IN_SCOPE below.
#
# Every control here is a state that changes from day to day and comes from a
# NOTAM, ATIS, a weather report or a controller. Several carry no time word at
# all ("usable", "working", "serviceable"), because the live gate rests on the
# model's judgement of the ANSWER, not on the wording.
#
# REMOVED as wrong controls — they are published, lasting facts:
#   "which runway is in use at Lagos today", "what runway are they using at
#   Lagos", "what is the runway in use for Kano"   -> AD 2.22.2 statement
# They moved to BOUNDARY_IN_SCOPE.
OUT_OF_SCOPE_CONTROLS = [
    "what's the weather in Lagos right now",
    "current METAR for Kano",
    "are there any active NOTAMs for Abuja",
    "can I get a clearance to land at Port Harcourt",
    "what slots are available at Abuja tomorrow",
    "how much does Jet A-1 cost at Lagos",
    "what is the ILS frequency at Heathrow",
    "runway length at JFK",
    "weather in London tomorrow",
    "is Accra airport open",
    "what is the wind at Abuja",
    "book me a flight to Lagos",
    "what time does my flight leave",
    # live state with no live keyword
    "Is runway 22 open right now?",
    "can I use 18R at Lagos tonight",
    "is the ILS at Abuja working",
    "any restrictions on runway 04 at Kano this afternoon",
    "is Port Harcourt airport closed",
    "what's the QNH at Lagos",
    "is the Abuja VOR serviceable today",
    "is taxiway A at Kano usable at the moment",
    # Present state of an ATS unit — a closure would be NOTAMed. Its HOURS are
    # published and answered ("Lagos tower hours today" is in the boundary set).
    "is Lagos tower open now",
]

# Published data that sits next to a refusal rule. Every one of these is in the
# AIP; refusing it is a boundary refusal. Paired, where possible, with a live
# control above that differs only in asking about a moment.
BOUNDARY_IN_SCOPE = [
    # GEN 4 charges (published; FAAN aerodrome charges and NAMA ANS charges)
    "what is the landing fee at Kano",
    "landing charges for international flights",
    "what is the passenger service charge",
    "terminal navigation charge for domestic flights",
    "what are the en-route charges",
    "parking charges at Lagos",
    # standing rules next to their live counterparts
    "what are the operating hours of Lagos tower",
    "landing forecast at Calabar",
    "TAF validity at Lagos",
    # AD 2.22 publishes the VFR clearance rule; "clearance to land" is ATC.
    # Measured: refused once the live gate went in — the cost this tracks.
    "what are the ATC clearance requirements for VFR flights at Lagos",
    # Lasting facts asked with a live-sounding word. Each must be ANSWERED:
    # the word does not change the published value.
    "which runway is in use at Lagos today",
    "what runway are they using at Lagos",
    "what is the runway in use for Kano",
    "what is Lagos tower frequency now",
    "current ATIS frequency at Kano",
    "current transition altitude at Abuja",
    "Lagos tower hours today",
    "what is the current landing fee at Lagos",
    "latest RFFS category for Port Harcourt",
    "which AIRAC amendment is the AIP currently on",
]


def classify(q):
    """-> (intent, icao_or_name_found). None intent means extraction failed."""
    try:
        ex = extract_query_parameters(q)
    except Exception as e:                           # noqa: BLE001
        return (f"<error:{type(e).__name__}>", None)
    if ex is None:
        return (None, None)
    return (ex.intent, ex.icao_code or ex.aerodrome_name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("infile", nargs="?", help="stress_set_clean.csv")
    ap.add_argument("--limit", type=int, default=200, help="in-scope questions to sample")
    ap.add_argument("--tag", default="run", help="writes <tag>.json")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"))
    ap.add_argument("--focus", metavar="RUN.json",
                    help="re-test ONLY the false refusals from a previous run")
    a = ap.parse_args()

    if a.compare:
        b = json.load(open(a.compare[0]))
        c = json.load(open(a.compare[1]))
        print(f"{'metric':28}{'before':>10}{'after':>10}{'delta':>10}")
        print("-" * 58)
        for k in ("false_refusal_pct", "boundary_refusal_pct", "false_accept_pct"):
            if k not in b or k not in c:
                bv = f"{b[k]:>9.1f}%" if k in b else f"{'n/a':>10}"
                cv = f"{c[k]:>9.1f}%" if k in c else f"{'n/a':>10}"
                print(f"{k:28}{bv}{cv}{'':>10}  (older run lacks this figure)")
                continue
            d = c[k] - b[k]
            arrow = "better" if (d < 0) else ("worse" if d > 0 else "same")
            print(f"{k:28}{b[k]:>9.1f}%{c[k]:>9.1f}%{d:>+9.1f}  {arrow}")
        if b.get("n_controls") != c.get("n_controls"):
            print(f"\nNOTE: the control sets differ ({b.get('n_controls', '?')} vs "
                  f"{c.get('n_controls', '?')} questions). Re-run both tags with "
                  f"the same copy of this script before reading the deltas.")
        print("\nA change is an improvement ONLY if false_refusal falls and")
        print("false_accept does not rise. Either alone is not enough.")
        return 0

    if a.focus:
        # FOCUSED MODE — the answer to "8.1% vs 8.6%, is that real?"
        #
        # It is not. At n=198 with p~=0.08 the standard error is +-1.9pp, so a
        # 0.5pp move is one case and cannot be distinguished from noise. Broad
        # sampling is the wrong instrument for a change that affects ~16 of 198
        # questions: 90% of the calls land on questions that already pass and
        # carry no information about the edit.
        #
        # This re-tests ONLY the questions that failed before. If a prompt
        # change fixes 12 of 16, that is 75% -> visible immediately, at a
        # twelfth of the cost. Keep using the broad run for false ACCEPTS,
        # which need the wide net; use this for iterating on refusals.
        prev = json.load(open(a.focus))
        qs = prev.get("false_refusals", [])
        if not qs:
            print(f"{a.focus} recorded no false refusals — nothing to focus on.")
            return 0
        print(f"FOCUSED: re-testing {len(qs)} questions that were refused in "
              f"{prev.get('tag', a.focus)!r}\n")
        fixed, still = [], []
        for q in qs:
            intent, _ = classify(q)
            (still if intent == "out_of_scope" else fixed).append((q, intent))
        print(f"  FIXED       {len(fixed):>3}/{len(qs)}  "
              f"({100*len(fixed)//len(qs)}%)")
        print(f"  STILL REFUSED {len(still):>3}/{len(qs)}")
        if fixed:
            print("\n  now accepted:")
            for q, i in fixed[:12]:
                print(f"    {q[:58]!r} -> {i}")
        if still:
            print("\n  still refused:")
            for q, _ in still[:12]:
                print(f"    {q[:58]!r}")
        print("\nNOTE: this measures refusals only. Re-run the broad mode to")
        print("confirm false ACCEPTS have not risen — a prompt that stops")
        print("refusing everything scores 100% here and is a worse bot.")
        return 0

    if not a.infile:
        ap.error("give stress_set_clean.csv, or use --compare or --focus")

    rows = list(csv.DictReader(open(a.infile)))
    random.seed(a.seed)
    # Sample evenly across subsections so one large section cannot dominate.
    by = defaultdict(list)
    for r in rows:
        by[r["subsection"]].append(r)
    per = max(1, a.limit // max(1, len(by)))
    sample = []
    for k in sorted(by):
        sample.extend(random.sample(by[k], min(per, len(by[k]))))
    sample = sample[:a.limit]

    print(f"IN SCOPE   : {len(sample)} questions from the stress set")
    print(f"BOUNDARY   : {len(BOUNDARY_IN_SCOPE)} published-but-live-sounding questions")
    print(f"OUT OF SCOPE: {len(OUT_OF_SCOPE_CONTROLS)} controls\n")

    false_refusals, by_sub, intents = [], Counter(), Counter()
    for i, r in enumerate(sample, 1):
        intent, _ = classify(r["question"])
        intents[intent] += 1
        if intent == "out_of_scope":
            false_refusals.append(r)
            by_sub[r["subsection"]] += 1
        if i % 25 == 0:
            print(f"  in-scope {i}/{len(sample)}  refused so far: {len(false_refusals)}")

    boundary_refusals = []
    for q in BOUNDARY_IN_SCOPE:
        intent, _ = classify(q)
        if intent == "out_of_scope":
            boundary_refusals.append(q)

    false_accepts = []
    for q in OUT_OF_SCOPE_CONTROLS:
        intent, _ = classify(q)
        if intent != "out_of_scope":
            false_accepts.append((q, intent))

    fr = 100.0 * len(false_refusals) / max(1, len(sample))
    br = 100.0 * len(boundary_refusals) / max(1, len(BOUNDARY_IN_SCOPE))
    fa = 100.0 * len(false_accepts) / max(1, len(OUT_OF_SCOPE_CONTROLS))

    print("\n" + "=" * 62)
    print(f"FALSE REFUSALS  {len(false_refusals):>4}/{len(sample):<5} {fr:5.1f}%"
          "   published data refused as out_of_scope")
    print(f"BOUNDARY REFUSALS {len(boundary_refusals):>2}/{len(BOUNDARY_IN_SCOPE):<5} {br:5.1f}%"
          "   live-sounding published data refused")
    print(f"FALSE ACCEPTS   {len(false_accepts):>4}/{len(OUT_OF_SCOPE_CONTROLS):<5} {fa:5.1f}%"
          "   out-of-scope questions NOT refused")

    if by_sub:
        print("\nfalse refusals by subsection:")
        for s, n in by_sub.most_common():
            print(f"   {s:<9} {n}")
        print("\nexamples:")
        for r in false_refusals[:8]:
            print(f"   [{r['subsection']}] {r['question'][:56]!r}")
            print(f"        AIP publishes: {r['label']} = {r['expected_value'][:44]!r}")

    if boundary_refusals:
        print("\nBOUNDARY REFUSALS — published, but refused:")
        for q in boundary_refusals:
            print(f"   {q[:56]!r}")

    if false_accepts:
        print("\n*** FALSE ACCEPTS — these must stay refused:")
        for q, intent in false_accepts:
            print(f"   {q[:56]!r} -> {intent}")

    print(f"\nintent distribution: {dict(intents.most_common(8))}")
    out = {"tag": a.tag, "n_in_scope": len(sample),
           "n_boundary": len(BOUNDARY_IN_SCOPE),
           "n_controls": len(OUT_OF_SCOPE_CONTROLS),
           "false_refusal_pct": fr, "boundary_refusal_pct": br,
           "false_accept_pct": fa,
           "false_refusals": [r["question"] for r in false_refusals],
           "boundary_refusals": boundary_refusals,
           "false_accepts": [q for q, _ in false_accepts]}
    json.dump(out, open(f"{a.tag}.json", "w"), indent=1)
    print(f"\nwritten to {a.tag}.json — rerun with a different --tag, then --compare")
    return 0


if __name__ == "__main__":
    sys.exit(main())
