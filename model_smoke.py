"""
model_smoke.py — one real call per model role, to prove a model works with
Vannie before any measurement is spent on it. About three calls; well under
one US cent.

    python model_smoke.py                                        # the configured models
    EXTRACTION_MODEL=gpt-6-luna SYNTHESIS_MODEL=gpt-6-luna python model_smoke.py
    EXTRACTION_MODEL=gpt-6-luna REASONING_EFFORT=low python model_smoke.py

Checks, through Vannie's own functions and prompts:
  extraction  agent.extract_query_parameters  -> a parsed AIPQueryExtraction
  judge       disambiguate.judge              -> 'Kano approach' is the approach family
  synthesis   synthesize.generate_grounded_answer over one made-up excerpt
              -> answerable, and the answer carries the excerpt's value

Each line shows the model OpenAI actually served, the time taken and the
tokens (reasoning tokens bill as output). Exit code 1 if any role fails —
then do not run the quick or full checks on that model.
"""
import logging
import sys
import threading
import time

logging.basicConfig(level=logging.WARNING)
for _n in ("httpx", "openai", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)

import agent         # noqa: E402  (run from the repo root)
import config        # noqa: E402
import disambiguate  # noqa: E402
import synthesize    # noqa: E402
from models import AIPResult  # noqa: E402

_lock = threading.Lock()
_last = {}
_last_out = None


def _wrap(client):
    comp = client.beta.chat.completions
    if getattr(comp.parse, "_smoke", False):
        return
    real = comp.parse

    def parse(*a, **kw):
        t0 = time.monotonic()
        resp = real(*a, **kw)
        u = getattr(resp, "usage", None)
        det = getattr(u, "completion_tokens_details", None)
        with _lock:
            _last.update(
                seconds=time.monotonic() - t0,
                served=getattr(resp, "model", "?"),
                prompt=getattr(u, "prompt_tokens", 0) or 0,
                completion=getattr(u, "completion_tokens", 0) or 0,
                reasoning=(getattr(det, "reasoning_tokens", 0) or 0) if det else 0,
                sent={k: v for k, v in kw.items() if k in ("temperature", "extra_body")},
            )
        return resp

    parse._smoke = True
    comp.parse = parse


def _role(name, model, role, fn, check):
    global _last_out
    _last.clear()
    _last_out = None
    try:
        out = fn()
        _last_out = out
        ok, why = check(out)
    except Exception as e:  # noqa: BLE001
        ok, why = False, f"{type(e).__name__}: {e}"
    effort = config.reasoning_effort_for(model, role)
    head = f"{'PASS' if ok else 'FAIL'}  {name:<10} {model}" + (f" [effort {effort}]" if effort else "")
    print(head)
    if _last:
        print(f"      served {_last['served']} · {_last['seconds']:.1f}s · "
              f"{_last['prompt']} prompt / {_last['completion']} output tokens"
              f" ({_last['reasoning']} reasoning) · sent {_last['sent']}")
    else:
        print("      no response — see the error logged above")
    if not ok:
        print(f"      {why}")
    elif getattr(_last_out, "answer", None):
        print(f"      answer: {_last_out.answer}")
    return ok


def main():
    _wrap(agent.client)
    _wrap(synthesize._client)

    # Feet AND metres: SYNTHESIS_SYSTEM rule 7 asks for both for an elevation,
    # and rule 5 says a partial answer is answerable=false. With feet only, a
    # careful model correctly declines (gpt-6-luna did, 2026-11-07).
    excerpt = AIPResult(
        content="DNXX AD 2.2 AERODROME GEOGRAPHICAL AND ADMINISTRATIVE DATA\n"
                "3 Elevation / Reference temperature: 376 M / 1234 FT / 31 DEG C",
        similarity=0.9, aip_section="AD 2.2", reference_tag="DNXX")

    results = [
        _role("extraction", config.EXTRACTION_MODEL, "extraction",
              lambda: agent.extract_query_parameters("What is the tower frequency at Kano?"),
              lambda ex: (ex is not None and bool(getattr(ex, "intent", None)),
                          f"returned {ex!r}")),
        _role("judge", config.EXTRACTION_MODEL, "extraction",
              lambda: disambiguate.judge("Kano approach"),
              lambda c: (c is not None and getattr(c, "family", None) == "approach",
                         f"returned {c!r}")),
        _role("synthesis", config.SYNTHESIS_MODEL, "synthesis",
              lambda: synthesize.generate_grounded_answer(
                  "What is the elevation of DNXX?", [excerpt]),
              lambda a: (a is not None and a.answerable and "1234" in (a.answer or "")
                         and "376" in (a.answer or ""),
                         f"returned {a!r}")),
    ]
    print()
    print("ALL PASS — safe to run: bash run_all_checks.sh --quick --stress"
          if all(results) else "FAILED — do not measure this model until fixed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
