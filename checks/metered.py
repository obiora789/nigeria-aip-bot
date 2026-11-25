"""
metered.py — run one check script and count every OpenAI call it makes.

    python checks/metered.py <step-name> checks/check_subsection.py stress_questions.csv ...
    python checks/metered.py --report checks_<date>/cost.jsonl

Why (2026-11-25): a full run costs about $5 and nobody knows which step spends
it. Before cutting anything (fewer repeats, caching), measure. This wraps the
two OpenAI clients the bot uses (agent.client, synthesize._client) for the
length of one script, counts tokens per kind of call (extraction, judge,
synthesis, embedding), including the prompt tokens OpenAI served from its
cache, and appends one JSON line per step to $VANNIE_METER_OUT. --report
prints the table with a list-price estimate.

Nothing in the bot changes: the wrapping exists only inside this process.
"""
import atexit
import json
import os
import runpy
import sys
import threading
from pathlib import Path

_R = next(d for d in Path(__file__).resolve().parents if (d / "main.py").exists())
sys.path[1:1] = [str(p) for p in (_R, _R / "checks", _R / "ingestion") if str(p) not in sys.path]

# USD per 1M tokens: (input, cached input, output). Check current prices
# before relying on these; reasoning tokens bill as output.
PRICES = {
    "gpt-4o-mini": (0.15, 0.075, 0.60),
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "text-embedding-3-small": (0.02, 0.02, 0.0),
}

_lock = threading.Lock()
_usage = {}          # kind -> {"calls", "prompt", "cached", "completion", "model"}


def _kind(kw, fallback):
    fmt = kw.get("response_format")
    name = getattr(fmt, "__name__", "") or ""
    if name == "AIPQueryExtraction":
        return "extraction"
    if "synth" in name.lower() or "answer" in name.lower():
        return "synthesis"
    return name.lower() or fallback


def _add(kind, resp, model):
    u = getattr(resp, "usage", None)
    if u is None:
        return
    det = getattr(u, "prompt_tokens_details", None)
    with _lock:
        row = _usage.setdefault(kind, {"calls": 0, "prompt": 0, "cached": 0,
                                       "completion": 0, "model": model})
        row["calls"] += 1
        row["prompt"] += getattr(u, "prompt_tokens", 0) or 0
        row["cached"] += (getattr(det, "cached_tokens", 0) or 0) if det else 0
        row["completion"] += getattr(u, "completion_tokens", 0) or 0


def _wrap(obj, attr, fallback):
    real = getattr(obj, attr, None)
    if real is None or getattr(real, "_metered", False):
        return

    def call(*a, **kw):
        resp = real(*a, **kw)
        _add(_kind(kw, fallback), resp, str(kw.get("model", "")))
        return resp

    call._metered = True
    setattr(obj, attr, call)


def install():
    clients = []
    try:
        import agent
        clients.append(agent.client)
    except Exception as e:  # noqa: BLE001 — metering must never stop a check
        print(f"(meter: agent client not wrapped: {e})", file=sys.stderr)
    try:
        import synthesize
        clients.append(synthesize._client)
    except Exception as e:  # noqa: BLE001
        print(f"(meter: synthesis client not wrapped: {e})", file=sys.stderr)
    for c in clients:
        _wrap(c.beta.chat.completions, "parse", "judge")
        _wrap(c.chat.completions, "parse", "judge")
        _wrap(c.chat.completions, "create", "chat")
        _wrap(c.embeddings, "create", "embedding")


def _usd(row):
    price = next((v for k, v in PRICES.items() if row["model"].startswith(k)), None)
    if not price:
        return None
    inp, cached, out = price
    return ((row["prompt"] - row["cached"]) * inp + row["cached"] * cached
            + row["completion"] * out) / 1e6


def _save(step):
    out = os.getenv("VANNIE_METER_OUT")
    line = json.dumps({"step": step, "usage": _usage})
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    else:
        print(line, file=sys.stderr)


def report(path):
    total, rows = 0.0, []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        for kind, row in sorted(rec["usage"].items()):
            usd = _usd(row)
            total += usd or 0
            rows.append((rec["step"], kind, row, usd))
    print(f"{'step':<26}{'kind':<12}{'calls':>7}{'prompt':>12}{'cached':>12}"
          f"{'output':>10}{'USD':>9}")
    for step, kind, row, usd in rows:
        print(f"{step:<26}{kind:<12}{row['calls']:>7}{row['prompt']:>12,}"
              f"{row['cached']:>12,}{row['completion']:>10,}"
              f"{(f'{usd:.3f}' if usd is not None else '?'):>9}")
    print(f"{'total (list prices)':<79}{total:>9.3f}")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--report":
        report(sys.argv[2])
        sys.exit(0)
    if len(sys.argv) < 3:
        sys.exit("usage: metered.py <step-name> <script.py> [args...]")
    step, script = sys.argv[1], sys.argv[2]
    install()
    atexit.register(_save, step)
    sys.argv = [script] + sys.argv[3:]
    runpy.run_path(script, run_name="__main__")
