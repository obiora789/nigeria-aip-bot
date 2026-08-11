#!/usr/bin/env python3
"""
run_stress_set.py — score Vannie against the AIP-grounded stress set.

Reads stress_set_clean.csv (build_stress_set.py -> fix_stress_set.py) and drives
main.process() itself, with the Telegram sends captured in memory. There is
deliberately NO copy of the decision flow here. Hits live OpenAI + Supabase.

WHAT MAKES THIS DIFFERENT FROM eval_set.py
------------------------------------------
eval_set.py scores 63 hand-written cases whose expected answers were recorded by
looking at what the bot did. That cannot catch a bug where the code and the
expectation share a wrong belief -- confirmed live: main._AERODROME_DATA_RE
claimed "transition altitude" for AD 2.2 and the test suite ASSERTED it must, so
93/93 passed while the bot answered with the aerodrome elevation.

Here every expectation comes from the AIP itself, via the validated extractors.
`subsection` is where the document puts the answer; `expected_value` is what the
document says. Neither was ever obtained by asking Vannie.

THREE SCORES, DELIBERATELY NOT AVERAGED
---------------------------------------
  ROUTING  did the reply cite the section the AIP actually keeps this in?
           A miss is a classifier / router problem.
  VALUE    did the reply contain the published value?
           A miss is a retrieval / scoping problem.
  CLARIFY  for questions with several correct answers ("gombe obstacle",
           "Lagos TORA"), did Vannie ASK instead of answering?
           A confident single answer here is a FAILURE, not a pass. Scoring
           these as answerable would reward exactly the misattribution this
           project exists to prevent.

Averaging the three hides which one you have, and they have different fixes.

MATCH MODES (set by fix_stress_set.py)
--------------------------------------
  exact    value is short -> require it in the reply, whitespace-normalised
  tokens   value is a long paragraph (AD 2.22 procedure text is ~68% space
           padding from the layout-preserving renderer, so verbatim
           containment fails on whitespace alone) -> require its distinctive
           numbers/idents
  absent   the AIP publishes NIL / Not available -> require Vannie to say so
           and, critically, NOT to state a number

USAGE
    python run_stress_set.py stress_set_clean.csv --limit 300
    python run_stress_set.py stress_set_clean.csv --subsection "AD 2.17"
    python run_stress_set.py stress_set_clean.csv --corruption typo

COST WARNING
    One gpt-4o-mini extraction + one embedding PER ROW. The full 15,111-row set
    is a real spend and a long run. --limit samples evenly across subsections so
    a few hundred rows still tell you where you stand. Start there.
"""
import argparse
import csv
import random
import re
import sys
from collections import Counter, defaultdict

import asyncio
import itertools

try:
    import main as vannie_main
    import observability
except ImportError as e:                             # noqa: BLE001
    print(f"Cannot import main.py: {e}")
    print("Run this from the repo root with the venv active.")
    sys.exit(2)

# ---------------------------------------------------------------------------
# CAPTURE MODE — drive the REAL main.process(), with the Telegram sends and the
# query log redirected into memory.
#
# WHY NOT e2e.run_pipeline(), WHICH eval_set.py USES
# --------------------------------------------------
# Because it is no longer a mirror of production, and it drifted silently:
#     main.py handles 11 statuses (approach_procedure, subsection_verbatim,
#       subsection, rwy_data, declared_distance, comms, navaid, lighting_data,
#       rwy_char, grounded, not_in_aip)
#     e2e.run_pipeline handles 2 (grounded, not_in_aip) -- the other nine fall
#       into its else-branch and become a generic verbatim answer
# and e2e calls synthesize_decision(q, results) WITHOUT `ex`, so the LLM
# subsection classifier never runs, while main.py passes it at lines 656/696.
#
# Scoring against that mirror would have produced a wall of routing failures
# caused by the harness, not the bot -- and none of the clarify flow, the
# AD 2.22/2.17 guards or the structured lookups would have been exercised at
# all. A second copy of the decision flow is a thing that WILL drift; this
# runner therefore has no copy.
#
# Nothing in main.py changes. send_message/send_charts are module-level imports
# and log_query is called in process()'s finally block, so both are patchable
# from outside.
# ---------------------------------------------------------------------------

_chat_ids = itertools.count(900000000)


def run_pipeline(q: str) -> dict:
    """Run one question through the real request path and capture the reply."""
    sent, log = [], {}

    async def _send_message(chat_id, text_, reply_markup=None, **kw):
        # Clarification prompts come through here WITHOUT the feedback keyboard
        # (see main.send_info's docstring), so the presence of reply_markup is
        # not a reliable signal -- capture the text and let the scorer judge.
        sent.append(text_ or "")

    async def _send_charts(chat_id, charts, **kw):
        sent.append(f"[charts sent: {len(charts or [])}]")
        return None

    def _log_query(**kw):
        log.update(kw)

    orig = (vannie_main.send_message, vannie_main.send_charts, observability.log_query)
    vannie_main.send_message, vannie_main.send_charts = _send_message, _send_charts
    observability.log_query = _log_query
    try:
        # A FRESH chat_id per question. main.process() carries short-term
        # conversation context and chart-pending slots per chat, so reusing one
        # id would let question N-1 slot-fill question N and quietly invalidate
        # the run.
        asyncio.run(vannie_main.process(next(_chat_ids), q))
    except Exception as e:                           # noqa: BLE001
        sent.append(f"<process error: {type(e).__name__}: {e}>")
    finally:
        vannie_main.send_message, vannie_main.send_charts, observability.log_query = orig

    # A row that captured NOTHING is a harness failure, not a bot failure.
    # main.process() always replies -- even an error path sends error(). Zero
    # captured messages therefore means the monkeypatch missed a send, which
    # happens silently if someone rebinds the import (`from telegram import
    # send_message as _sm`) or inlines a send. Without this flag those rows
    # would score as FAIL and send you hunting a routing bug that is really a
    # broken harness -- exactly the failure mode e2e.py already demonstrated.
    captured_nothing = not sent
    if captured_nothing and not log:
        sent.append("<HARNESS: no message captured and no query logged>")

    return {"query": q, "reply": "\n".join(sent),
            "path": log.get("path", "unknown"), "icao": log.get("icao"),
            "intent": log.get("intent"), "sim": log.get("similarity") or 0.0,
            "charts": [], "messages": len(sent),
            "harness_error": captured_nothing}


# A reply that asks rather than answers. These are the literal prompts
# _run_chart_decision and the resolver emit, not invented phrasings.
_ASKING_RE = re.compile(
    r"which\s+(approach|runway|aerodrome)|tap one|please give a name or icao|"
    r"matches more than one|which one", re.I)

# Vannie saying a field is not published. Must be distinguished from Vannie
# failing to find it, so abstention phrasing counts too.
_ABSENT_RE = re.compile(
    r"\bnil\b|not\s+applicable|not\s+available|no[t]?\s+published|"
    r"does\s+not\s+(state|publish)|couldn'?t\s+find|don'?t\s+have", re.I)

_NUM_RE = re.compile(r"\d[\d,.]*")

# Vannie declining to single out one value from a multi-entity AIP table. This
# is the navaid/comms guard working exactly as designed -- "this aerodrome
# publishes several navaids in one AIP table, so I won't single out one value".
# Measured: 23 such replies scored 9 FAIL + 9 VALUE_MISS, i.e. correct
# safety behaviour counted as failure, which flatters nothing and hides real
# defects. Scored as its own verdict so it is visible without being punished.
_REFUSAL_RE = re.compile(
    r"won'?t single out|read the exact figure|publishes several", re.I)


def _despace(s: str) -> str:
    """The AIP writes thousands with a space ('3 610'); the layout renderer adds
    many more. Collapse both so matching compares values, not whitespace."""
    s = re.sub(r"(\d)\s+(\d{3})(?!\d)", r"\1\2", s or "")
    return re.sub(r"\s+", " ", s).strip()


def _norm_section(s: str) -> str:
    m = re.search(r"(\d+\.\d+)", s or "")
    return f"AD {m.group(1)}" if m else (s or "").strip()


# Paths that answer correctly WITHOUT citing a section, because there is no
# section to cite. Measured: AD 2.1 scored 11 ROUTE_MISS out of 13, every one
# of them the mapping path answering correctly -- "city at Zaria" -> "DNZA —
# Zaria, Nigeria. Source: Nigeria AIP · AIRAC AMDT 03/2026". The reply is
# right; a location-indicator lookup simply has no AD 2.x subsection. Marking
# those as routing failures buried real failures under harness noise.
_SECTIONLESS_PATHS = ("mapping", "greeting", "structure", "facts")


def score_routing(row, r):
    """Did the reply cite the section the AIP keeps this field in?

    Checked against the reply text and the pipeline's own path/section fields,
    because different paths cite differently (a structured reply names the
    section in its header; the facts path names it in the citation)."""
    if str(r.get("path") or "").split(":")[0] in _SECTIONLESS_PATHS:
        return True
    want = _norm_section(row["subsection"])
    hay = " ".join(str(r.get(k) or "") for k in ("reply", "path", "section", "ref"))
    hay_n = re.sub(r"\s+", " ", hay)
    if re.search(rf"\b{re.escape(want)}\b", hay_n, re.I):
        return True
    # "AD 2.2" must not be satisfied by "AD 2.22" — check the number exactly.
    num = want.split()[-1]
    return bool(re.search(rf"(?<!\d){re.escape(num)}(?!\d)", hay_n))


def score_value(row, r):
    """Did the reply contain the published value, per match_mode?"""
    reply = _despace(r.get("reply") or "")
    mode = (row.get("match_mode") or "exact").lower()
    want = _despace(row["expected_value"])

    if mode == "absent":
        # Correct = says it isn't published. Wrong = states a number instead,
        # which is the invent-a-value failure.
        if _NUM_RE.search(reply) and not _ABSENT_RE.search(reply):
            return False
        return bool(_ABSENT_RE.search(reply))

    if mode == "tokens":
        # Use the RAW value, not the despaced one. _despace() joins a digit to a
        # following 3-digit group so the AIP's "3 610" matches "3610" -- correct
        # for a single value, destructive for a token LIST, where it welded
        # "2800 045 190 610" into "2800045 190610" and matched nothing. Caught
        # by the scorer self-test before this cost a single API call.
        toks = [t for t in (row["expected_value"] or "").split() if t]
        if not toks:
            return False
        hit = sum(1 for t in toks if t.lower() in reply.lower())
        return hit >= max(1, len(toks) // 2)      # majority of distinctive tokens

    return want.lower() in reply.lower()


# A reply that LISTS several labelled entries rather than asserting one value.
# facts_reply() groups facts by entity, the structured runway path prints each
# end as "[05] ... [23] ...", and the field-table renderer bullets every field.
_ENUM_BULLET_RE = re.compile(r"(?:^|\n)\s*[•▸]", re.M)
_ENUM_LABEL_RE = re.compile(r"(?:^|\n)\s*[A-Za-z][^:\n]{2,40}:", re.M)
_ENUM_ENTITY_RE = re.compile(r"\[\s*\d{2}[LRC]?\s*\]")


def _enumerates(reply: str) -> bool:
    """True if the reply shows SEVERAL labelled entries."""
    b = reply or ""
    return max(len(_ENUM_BULLET_RE.findall(b)),
               len(_ENUM_LABEL_RE.findall(b)),
               len(_ENUM_ENTITY_RE.findall(b))) >= 2


def score_clarify(row, r):
    """For a question with several correct answers, did Vannie avoid asserting
    just one of them?

    THIS TEST WAS ORIGINALLY WRONG, and the error was mine. It demanded a
    clarifying QUESTION, on the assumption that asking is the only safe
    response to an ambiguous query. Measured against real replies, 43 of 64
    so-called failures were Vannie ENUMERATING every entity, each labelled --

        Yola — AD 2.4
        Cargo-handling facilities: NAHCO and SAHCOL
        Fuelling facilities and capacity: Bowser
        Hangar space for visiting aircraft: NIL
        ...

    That is not misattribution; it is a better answer than a question, because
    the pilot gets everything in one message and each value stays welded to its
    own label. responder.facts_reply() groups by entity for exactly this
    reason. Scoring it as a failure would have pushed the bot toward asking
    more questions -- degrading a working design to satisfy a bad metric.

    So three responses PASS, and only one FAILS:
      PASS  asks which entity                    (clarify flow)
      PASS  lists them all, labelled             (enumeration)
      PASS  honestly declines to answer          (abstention)
      FAIL  asserts ONE value, unqualified       (the real misattribution)
    """
    reply = r.get("reply") or ""
    if _ASKING_RE.search(reply):
        return True
    if _enumerates(reply):
        return True
    # An honest abstention. A confidence figure in the refusal ("best was 35%")
    # is not an asserted AIP value, so the numeric check must not veto it --
    # that alone wrongly failed a reply that said "I won't guess on
    # aeronautical data".
    return bool(_ABSENT_RE.search(reply))


def sample(rows, limit, seed=0):
    """Sample evenly across subsections, so a small run still covers all 23
    rather than over-representing AD 2.22 (which is 21% of the set)."""
    if not limit or limit >= len(rows):
        return rows
    random.seed(seed)
    by = defaultdict(list)
    for r in rows:
        by[r["subsection"]].append(r)
    per = max(1, limit // max(1, len(by)))
    out = []
    for k in sorted(by):
        out.extend(random.sample(by[k], min(per, len(by[k]))))
    random.shuffle(out)
    return out[:limit]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("infile")
    ap.add_argument("--out", default="stress_results.csv")
    ap.add_argument("--limit", type=int, default=0, help="0 = all (expensive)")
    ap.add_argument("--subsection", help="e.g. 'AD 2.17'")
    ap.add_argument("--corruption", help="e.g. typo")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()

    rows = list(csv.DictReader(open(a.infile)))
    if a.subsection:
        rows = [r for r in rows if _norm_section(r["subsection"]) == _norm_section(a.subsection)]
    if a.corruption:
        rows = [r for r in rows if r["corruption"] == a.corruption]
    rows = sample(rows, a.limit, a.seed)
    if not rows:
        print("No rows match those filters.")
        return 1
    print(f"running {len(rows)} cases "
          f"(~{len(rows)} extractions + {len(rows)} embeddings)\n")

    out, tally = [], Counter()
    by_sub = defaultdict(Counter)
    by_corr = defaultdict(Counter)

    for i, row in enumerate(rows, 1):
        try:
            r = run_pipeline(row["question"])
        except Exception as e:                       # noqa: BLE001
            r = {"reply": f"<pipeline error: {type(e).__name__}: {e}>",
                 "path": "error", "icao": "", "sim": 0.0, "charts": []}

        if r.get("harness_error"):
            tally["HARNESS_ERROR"] += 1
            out.append({**row, "verdict": "HARNESS_ERROR", "routing_ok": False,
                        "value_ok": False, "path": r.get("path"),
                        "icao_got": r.get("icao"), "sim": "0.000",
                        "reply": r.get("reply", "")[:500]})
            continue

        clarify_case = row.get("expected_behaviour") == "clarify"
        routing = score_routing(row, r)
        if not clarify_case and _REFUSAL_RE.search(r.get("reply") or ""):
            tally["GUARD_REFUSAL"] += 1
            by_sub[row["subsection"]]["GUARD_REFUSAL"] += 1
            by_corr[row["corruption"]]["GUARD_REFUSAL"] += 1
            out.append({**row, "verdict": "GUARD_REFUSAL", "routing_ok": routing,
                        "value_ok": None, "path": r.get("path"),
                        "icao_got": r.get("icao"),
                        "sim": f"{float(r.get('sim') or 0):.3f}",
                        "reply": (r.get("reply") or "").replace("\n", " ⏎ ")[:500]})
            continue
        if clarify_case:
            asked = score_clarify(row, r)
            verdict = "CLARIFY_OK" if asked else "CLARIFY_MISS"
            value = None
        else:
            value = score_value(row, r)
            verdict = ("PASS" if (routing and value)
                       else "VALUE_MISS" if routing
                       else "ROUTE_MISS" if value
                       else "FAIL")

        tally[verdict] += 1
        by_sub[row["subsection"]][verdict] += 1
        by_corr[row["corruption"]][verdict] += 1
        out.append({**row, "verdict": verdict,
                    "routing_ok": routing, "value_ok": value,
                    "path": r.get("path"), "icao_got": r.get("icao"),
                    "sim": f"{float(r.get('sim') or 0):.3f}",
                    "reply": (r.get("reply") or "").replace("\n", " ⏎ ")[:500]})
        if i % 25 == 0 or i == len(rows):
            print(f"  {i}/{len(rows)}  {dict(tally)}")

    with open(a.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)

    if tally["HARNESS_ERROR"]:
        print(f"\n*** {tally['HARNESS_ERROR']} HARNESS ERRORS — capture missed a send.")
        print("    Check that main.py still does `from telegram import send_message,")
        print("    send_charts` at module level and that log_query runs in finally.")
        print("    Do NOT read the scores below until this is zero.\n")

    ans = sum(v for k, v in tally.items()
              if not k.startswith("CLARIFY")
              and k not in ("HARNESS_ERROR", "GUARD_REFUSAL"))
    clr = sum(v for k, v in tally.items() if k.startswith("CLARIFY"))
    print("\n" + "=" * 74)
    print(f"{'ANSWERABLE':<14}{ans:>6}")
    if ans:
        print(f"  routing correct   {sum(1 for r in out if r['routing_ok']):>6}"
              f"  ({100*sum(1 for r in out if r['routing_ok'])//len(out)}% of all rows)")
        print(f"  PASS              {tally['PASS']:>6}  ({100*tally['PASS']//ans}%)")
        print(f"  VALUE_MISS        {tally['VALUE_MISS']:>6}  right section, wrong/absent value")
        print(f"  ROUTE_MISS        {tally['ROUTE_MISS']:>6}  right value, wrong section cited")
        print(f"  FAIL              {tally['FAIL']:>6}  neither")
    if tally["GUARD_REFUSAL"]:
        print(f"\n{'GUARD REFUSAL':<14}{tally['GUARD_REFUSAL']:>6}  "
              f"declined to single out a value from a multi-entity table")
        print("               (correct safety behaviour — not counted as a failure)")

    print(f"\n{'MUST CLARIFY':<14}{clr:>6}")
    if clr:
        print(f"  asked (correct)   {tally['CLARIFY_OK']:>6}  ({100*tally['CLARIFY_OK']//clr}%)")
        print(f"  ANSWERED ANYWAY   {tally['CLARIFY_MISS']:>6}  <-- misattribution risk")

    print("\nworst subsections (by non-PASS rate):")
    scored = [(s, c) for s, c in by_sub.items() if sum(c.values()) >= 3]
    for s, c in sorted(scored, key=lambda kv: -(1 - kv[1]["PASS"] / sum(kv[1].values())))[:8]:
        tot = sum(c.values())
        print(f"  {s:<9} {tot:>4} cases  PASS {c['PASS']:>3}  "
              f"{dict((k, v) for k, v in c.items() if k != 'PASS')}")

    print("\nby corruption (this is the regex-fragility signal):")
    for k in sorted(by_corr):
        c = by_corr[k]
        tot = sum(c.values())
        ok = c["PASS"] + c["CLARIFY_OK"]
        print(f"  {k:<14} {ok:>4}/{tot:<4} ({100*ok//tot if tot else 0}%)")

    print(f"\nwritten to {a.out}")
    print("Compare 'clean' against the corrupted rows: a large gap is a regex")
    print("failing on surface form, and names the layer to replace next.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
