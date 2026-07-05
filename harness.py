"""
harness.py — run the full pipeline locally, WITHOUT Telegram.

This is your main dev loop: it does the real extraction, resolution, embedding,
search, and reply-building, then prints everything to the console so you can see
exactly what a pilot would receive. No Telegram, no webhook.

Usage:
    python harness.py "Lagos tower frequency"
    python harness.py                       # interactive loop

Needs OPENAI_API_KEY and SUPABASE_URL/SUPABASE_KEY in the environment.
(TELEGRAM_BOT_TOKEN must exist for imports, but is never called here.)
"""
import sys

import config
import resolver
import facts
import toc
from agent import extract_query_parameters, get_embedding
from database import get_charts, get_charts_smart, search_aip
import synthesize
from responder import (ambiguous, answer, chart_intro, chart_not_found, error,
                       grounded_reply, low_confidence, not_found, not_in_aip,
                       unresolved)


def run(text: str) -> None:
    print("=" * 72)
    print(f"QUERY: {text}")

    ex = extract_query_parameters(text)
    if ex is None:
        print("[extraction failed]\n", error())
        return
    print("EXTRACTION:", ex.model_dump())

    if ex.intent == "general_greeting":
        print("\nREPLY:\n", config.GREETING); return
    if toc.is_structure_question(text):
        ans = toc.answer(text)
        if ans:
            print("\nREPLY:\n", ans); return
    if ex.intent == "out_of_scope":
        print("\nREPLY:\n", config.OUT_OF_SCOPE); return

    res = resolver.resolve(ex)
    print("RESOLUTION:", res)
    if res.ambiguous:
        print("\nREPLY:\n", ambiguous(res)); return
    if res.unresolved:
        print("\nREPLY:\n", unresolved(res)); return

    if ex.intent == "icao_lookup":
        full = resolver.aerodrome_full_name(res.icao) or res.label
        print("\nREPLY:\n",
              f"{res.icao} — {full}, Nigeria. Source: Nigeria AIP · {config.AIRAC_CYCLE}")
        return

    # Chart requests are plate-only — no KB text shown (mirrors production).
    if ex.intent == "chart_retrieval":
        ql = text.lower()
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
                    chart_icao, f"{ex.procedure_type or ''} {text}", ex.runway or "")[: config.MAX_CHARTS]
        if charts:
            print("\nREPLY:\n", chart_intro(res, ex))
            print(f"\nCHARTS ({len(charts)}):")
            for c in charts:
                kind = "PDF" if c.is_pdf else "IMG"
                print(f"  [{kind}] {c.procedure_type} RWY {c.runway} -> {c.url}")
        else:
            print("\nREPLY:\n", chart_not_found(res, ex))
        return

    search_text = resolver.build_search_text(ex, res, text)
    if search_text != text:
        print("SEARCH TEXT:", search_text)
    emb = get_embedding(search_text)
    if emb is None:
        print("\nREPLY:\n", error()); return

    outcome = search_aip(emb, res, ex.procedure_type or "", ex.runway or "")
    print(f"SEARCH: abstained={outcome.abstained} reason='{outcome.reason}' "
          f"max_sim={outcome.max_similarity:.3f} part={outcome.used_part} "
          f"ref={outcome.used_reference} n={len(outcome.results)}")

    if outcome.abstained and outcome.reason == "low_confidence":
        print("\nREPLY:\n", low_confidence(outcome))
    elif outcome.abstained:
        print("\nREPLY:\n", not_found())
    else:
        status, ga = synthesize.synthesize_decision(text, outcome.results)
        print(f"SYNTHESIS: {status}")
        if status == "grounded":
            print("\nREPLY:\n", grounded_reply(ga, outcome, res))
        elif status == "not_in_aip":
            print("\nREPLY:\n", not_in_aip(res))
        else:
            print("\nREPLY:\n", answer(outcome, res, ex.runway, text))

    if res.icao:
        charts = get_charts(res.icao, ex.procedure_type or "", ex.runway or "")
        print(f"\nCHARTS ({len(charts)}):")
        for c in charts:
            kind = "PDF" if c.is_pdf else "IMG"
            print(f"  [{kind}] {c.procedure_type} RWY {c.runway} -> {c.url}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run(" ".join(sys.argv[1:]))
    else:
        print("Interactive mode — Ctrl-C to quit.")
        try:
            while True:
                q = input("\n> ").strip()
                if q:
                    run(q)
        except (KeyboardInterrupt, EOFError):
            print()
