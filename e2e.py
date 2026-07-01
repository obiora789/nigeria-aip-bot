#!/usr/bin/env python3
"""
e2e.py — Vannie end-to-end integration runner.

Drives the REAL pipeline (OpenAI extraction + embeddings, Supabase RPCs, chart
catalogue) at the text level — the same decision flow as main.process(), minus
the Telegram sends — over a golden set, and auto-checks the safety invariants
we hardened: correct ICAO (no wrong-airport), abstention behaviour, verbatim
answers carrying a citation + AIRAC + disclaimer, chart requests returning the
plate (never plate text), and NO chart-plate text leaking into any answer.

Usage:
  python e2e.py                 # pre-flight + full golden set
  python e2e.py --preflight     # pre-flight checks only
  python e2e.py "your query"    # run a single query through the checked pipeline

Cost: ~15 gpt-4o-mini extractions + ~12 embeddings + searches. Cents, not dollars.
This hits LIVE OpenAI + Supabase. It does NOT message Telegram.
"""
import os
import re
import sys

import config
import resolver
import facts
import toc
import synthesize
from agent import extract_query_parameters, get_embedding
from database import get_charts, get_charts_smart, search_aip
from responder import (ambiguous, answer, chart_intro, chart_not_found, error,
                       grounded_reply, low_confidence, not_found, not_in_aip,
                       unresolved)

# Markers that must NEVER appear in a pilot-facing text answer — they indicate a
# chart PLATE chunk leaked into the text corpus (the bug the re-ingest fixed).
PLATE_MARKERS = [
    "SCALE 1:", "BEARINGS, TRACKS AND RADIALS", "INSTRUMENT APPROACH CHART",
    "BEARINGS TRACKS AND RADIALS",
]
DISCLAIMER_FRAGMENT = "Reference aid only"
CHART_HINT = re.compile(r"\b(ils|rnav|gnss|rnp|sid|star|iac|vac)\b", re.I)


def _wants_chart(ex) -> bool:
    """Mirror of main._wants_chart: explicit chart intent or a chart-procedure hint."""
    if ex.intent == "chart_retrieval":
        return True
    blob = f"{ex.procedure_type or ''} {ex.aerodrome_name or ''}"
    return bool(CHART_HINT.search(blob))


# ── Run one query through the (text-level) pipeline, mirroring main.process ────

def run_pipeline(q: str) -> dict:
    out = {"query": q, "path": None, "intent": None, "icao": None, "ref": None,
           "hint": None, "sim": 0.0, "reply": "", "charts": [], "results": []}

    ex = extract_query_parameters(q)
    if ex is None:
        out["path"], out["reply"] = "error", error()
        return out
    out["intent"] = ex.intent

    if ex.intent == "general_greeting":
        out["path"], out["reply"] = "greeting", config.GREETING
        return out
    if ex.intent == "out_of_scope":
        # A structure question ("which part covers X") can be mis-tagged
        # out_of_scope — answer it deterministically from the ToC first.
        if toc.is_structure_question(q):
            ans = toc.answer(q)
            if ans:
                out["path"], out["reply"] = "structure", ans
                return out
        out["path"], out["reply"] = "out_of_scope", config.OUT_OF_SCOPE
        return out

    # Cross-aerodrome enumeration (e.g. "which aerodromes use 5000 ft TA") — a
    # structured-facts lookup, not retrieval (top-k can't scan all 40 at once).
    if facts.is_ta_enumeration(q):
        ans = facts.answer_ta_enumeration(q)
        if ans:
            out["path"], out["reply"] = "facts", ans
            return out

    # Structure/meta questions are about the AIP's organisation, not data.
    if toc.is_structure_question(q):
        ans = toc.answer(q)
        if ans:
            out["path"], out["reply"] = "structure", ans
            return out

    res = resolver.resolve(ex)
    out["icao"], out["ref"], out["hint"] = res.icao, res.reference, res.aerodrome_hint
    if res.ambiguous:
        out["path"], out["reply"] = "ambiguous", ambiguous(res)
        return out
    if res.unresolved:
        out["path"], out["reply"] = "unresolved", unresolved(res)
        return out

    if ex.intent == "icao_lookup":
        full = resolver.aerodrome_full_name(res.icao) or res.label
        out["path"] = "mapping"
        out["reply"] = f"{res.icao} — {full}, Nigeria. Source: Nigeria AIP · {config.AIRAC_CYCLE}"
        return out

    # Chart requests short-circuit to plate-only (no KB text).
    if ex.intent == "chart_retrieval":
        ql = q.lower()
        chart_icao = res.icao
        if chart_icao is None and (res.reference == "DNKK" or "fir" in ql
                                   or "en-route" in ql or "enroute" in ql):
            chart_icao = "DNKK"
        charts = []
        if chart_icao:
            if chart_icao in ("GEN", "DNKK"):
                charts = get_charts(chart_icao, "", "")[: config.MAX_CHARTS]
            else:
                charts = get_charts_smart(
                    chart_icao, f"{ex.procedure_type or ''} {q}", ex.runway or "")[: config.MAX_CHARTS]
        out["charts"] = charts
        out["path"] = "chart" if charts else "chart_not_found"
        out["reply"] = chart_intro(res, ex) if charts else chart_not_found(res, ex)
        return out

    # Text path.
    search_text = resolver.build_search_text(ex, res, q)
    emb = get_embedding(search_text)
    if emb is None:
        out["path"], out["reply"] = "error", error()
        return out
    outcome = search_aip(emb, res, ex.procedure_type or "", ex.runway or "")
    out["sim"], out["results"] = outcome.max_similarity, outcome.results

    if outcome.abstained and outcome.reason == "low_confidence":
        out["path"], out["reply"] = "abstain", low_confidence(outcome)
    elif outcome.abstained:
        out["path"], out["reply"] = "abstain", not_found()
    else:
        status, ga = synthesize.synthesize_decision(q, outcome.results)
        if status == "grounded":
            out["path"], out["reply"] = "grounded", grounded_reply(ga, outcome, res)
        elif status == "not_in_aip":
            out["path"], out["reply"] = "not_in_aip", not_in_aip(res)
        else:
            out["path"], out["reply"] = "answer", answer(outcome, res, ex.runway)

    # Supplementary charts (SAR rides along with SAR text; chart-procedure hints).
    ql = q.lower()
    is_sar = re.search(r"\bsar\b|search and rescue|\brescue\b", ql) is not None
    chart_icao = res.icao or ("GEN" if is_sar else None)
    if chart_icao and (_wants_chart(ex) or is_sar):
        if chart_icao in ("GEN", "DNKK"):
            out["charts"] = get_charts(chart_icao, "", "")[: config.MAX_CHARTS]
        else:
            out["charts"] = get_charts_smart(
                chart_icao, f"{ex.procedure_type or ''} {q}", ex.runway or "")[: config.MAX_CHARTS]
    return out


# ── Invariant checks ─────────────────────────────────────────────────────────

def check(case: dict, r: dict) -> tuple[list, list]:
    fails, warns = [], []
    reply_up = r["reply"].upper()

    # Expectations from the golden case.
    if "path" in case and r["path"] != case["path"]:
        fails.append(f"path={r['path']} expected {case['path']}")
    if "icao" in case and r["icao"] != case["icao"]:
        fails.append(f"icao={r['icao']} expected {case['icao']}")
    if "ref" in case and r["ref"] != case["ref"]:
        fails.append(f"ref={r['ref']} expected {case['ref']}")
    if case.get("charts") and not r["charts"]:
        fails.append("expected chart(s), got none")
    for s in case.get("contains", []):
        if s.upper() not in reply_up:
            fails.append(f"missing text: {s!r}")
    if "min_sim" in case and r["path"] == "answer" and r["sim"] < case["min_sim"]:
        warns.append(f"sim {r['sim']:.3f} < {case['min_sim']}")

    # Universal safety invariants on any verbatim ANSWER.
    if r["path"] == "answer":
        if DISCLAIMER_FRAGMENT.upper() not in reply_up:
            fails.append("answer missing disclaimer")
        if config.AIRAC_CYCLE.upper() not in reply_up:
            fails.append("answer missing AIRAC stamp")
        if "% MATCH]" not in reply_up:
            fails.append("answer missing citation")
    # Plate text must never appear in any answer or chart caption.
    if r["path"] in ("answer", "chart"):
        for m in PLATE_MARKERS:
            if m.upper() in reply_up:
                fails.append(f"PLATE TEXT LEAK: {m!r}")
    return fails, warns


# ── Golden set ───────────────────────────────────────────────────────────────
# Expectations are conservative; min_sim is a WARNING, not a hard fail.

GOLDEN = [
    {"name": "greeting",        "q": "hi", "path": "greeting"},
    {"name": "out_of_scope",    "q": "what's the weather in London tomorrow?",
     "path": "out_of_scope"},
    {"name": "freq_lagos",      "q": "Lagos tower frequency",
     "path": "answer", "icao": "DNMM", "contains": ["MHz"]},
    {"name": "vor_ident_pot",   "q": "POT VOR frequency",
     "path": "answer", "icao": "DNPO", "contains": ["MHz"]},
    {"name": "airspace_ph",     "q": "What are the lateral limits of PH approach?",
     "path": "answer", "ref": "AIRSPACE", "contains": ["Port Harcourt"], "min_sim": 0.5},
    {"name": "sar_units",       "q": "search and rescue units in Nigeria",
     "path": "answer", "ref": "NATIONAL", "charts": True},
    {"name": "chart_ils_abc",   "q": "What is the ILS approach plate for abc?",
     "path": "chart", "icao": "DNAA", "charts": True, "contains": ["ILS", "Abuja"]},
    {"name": "runway_dist",     "q": "DNAA declared distances RWY 04",
     "path": "answer", "icao": "DNAA"},
    {"name": "unresolved_noad", "q": "show me the ILS approach plate",
     "path": "unresolved"},
    {"name": "oos_indicator",   "q": "frequencies for DNBI",
     "path": "unresolved", "contains": ["Bida"]},
    {"name": "abstain_nonsense","q": "what is the warp core containment limit?",
     # accept either an explicit out-of-scope classification or a low-confidence abstain
     "path": None},
    {"name": "regress_abuja",   "q": "Abuja tower frequency",
     "path": "answer", "icao": "DNAA", "contains": ["MHz"]},
]


def run_suite() -> int:
    print("=" * 78)
    print("VANNIE END-TO-END GOLDEN SET")
    print("=" * 78)
    passed = failed = warned = 0
    for case in GOLDEN:
        r = run_pipeline(case["q"])
        fails, warns = check(case, r)
        # Special-case the nonsense query: pass if it abstained OR was out_of_scope.
        if case["name"] == "abstain_nonsense":
            fails = [] if r["path"] in ("abstain", "out_of_scope") else \
                    [f"path={r['path']} (expected abstain/out_of_scope)"]
        tag = "PASS" if not fails else "FAIL"
        if fails:
            failed += 1
        else:
            passed += 1
        if warns:
            warned += 1
        line = (f"[{tag}] {case['name']:18} | intent={r['intent']} icao={r['icao']} "
                f"path={r['path']} sim={r['sim']:.3f} charts={len(r['charts'])}")
        print(line)
        for f in fails:
            print(f"        ✗ {f}")
        for w in warns:
            print(f"        ! {w}")
    print("-" * 78)
    print(f"PASSED {passed}  FAILED {failed}  (with warnings: {warned})")
    return 1 if failed else 0


# ── Pre-flight ───────────────────────────────────────────────────────────────

def preflight() -> int:
    print("=" * 78)
    print("PRE-FLIGHT")
    print("=" * 78)
    problems = 0

    def ok(label, cond, detail=""):
        nonlocal problems
        print(f"  [{'OK ' if cond else 'XX '}] {label}{(' — ' + detail) if detail else ''}")
        if not cond:
            problems += 1
        return cond

    # 1) env
    for v in ("OPENAI_API_KEY", "SUPABASE_URL", "SUPABASE_KEY", "TELEGRAM_BOT_TOKEN"):
        ok(f"env {v}", bool(os.getenv(v)))

    # 2) embedding dimension
    try:
        emb = get_embedding("integration test")
        ok("embedding model returns 1536-dim", emb is not None and len(emb) == 1536,
           f"got {0 if emb is None else len(emb)}")
    except Exception as e:  # noqa: BLE001
        ok("embedding model", False, str(e)[:80]); emb = None

    # 3) text RPC smoke + v2 RETURNS columns (aip_section / reference_tag present)
    try:
        from schemas import AIPQueryExtraction
        ex = extract_query_parameters("Lagos tower frequency")
        res = resolver.resolve(ex)
        st = resolver.build_search_text(ex, res, "Lagos tower frequency")
        e2 = get_embedding(st)
        outcome = search_aip(e2, res, ex.procedure_type or "", ex.runway or "")
        ok("match_aip_text_advanced returns rows", bool(outcome.results),
           f"{len(outcome.results)} rows, max_sim={outcome.max_similarity:.3f}")
        if outcome.results:
            top = outcome.results[0]
            ok("RPC v2 RETURNS aip_section", top.aip_section is not None,
               "apply sql/match_aip_text_advanced.sql if missing")
            ok("RPC v2 RETURNS reference_tag", top.reference_tag is not None,
               "apply sql/match_aip_text_advanced.sql if missing")
            # corpus cleanliness after re-ingest
            joined = " ".join(x.content.upper() for x in outcome.results)
            ok("no plate text in top results",
               not any(m.upper() in joined for m in PLATE_MARKERS),
               "re-ingest may be incomplete")
    except Exception as e:  # noqa: BLE001
        ok("text search RPC", False, str(e)[:120])

    # 4) chart catalogue + storage URLs
    try:
        charts = get_charts("DNMM", "", "")
        ok("get_aip_charts returns charts (DNMM)", bool(charts),
           f"{len(charts)} charts")
        if charts:
            ok("chart URL looks public", str(charts[0].url).startswith("http"),
               charts[0].url[:60])
        sar = get_charts("GEN", "", "")
        ok("SAR chart present (GEN)", bool(sar))
        dnkk = get_charts("DNKK", "", "")
        ok("en-route charts present (DNKK)", bool(dnkk), f"{len(dnkk)} plates")
    except Exception as e:  # noqa: BLE001
        ok("chart catalogue", False, str(e)[:120])

    # 5) AIRAC stamp configured
    ok("AIRAC cycle configured", bool(getattr(config, "AIRAC_CYCLE", "")),
       getattr(config, "AIRAC_CYCLE", ""))

    print("-" * 78)
    print("PRE-FLIGHT CLEAN" if not problems else f"PRE-FLIGHT ISSUES: {problems}")
    return 1 if problems else 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--preflight":
        sys.exit(preflight())
    if args:
        q = " ".join(args)
        r = run_pipeline(q)
        print(f"QUERY: {q}")
        print(f"  intent={r['intent']} icao={r['icao']} ref={r['ref']} "
              f"hint={r['hint']} path={r['path']} sim={r['sim']:.3f} "
              f"charts={len(r['charts'])}")
        fails, warns = check({}, r)
        for f in fails:
            print(f"  ✗ {f}")
        for w in warns:
            print(f"  ! {w}")
        print("\nREPLY:\n" + r["reply"])
        sys.exit(1 if fails else 0)
    code = preflight()
    print()
    code |= run_suite()
    sys.exit(code)
