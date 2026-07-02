"""
observability.py — turn "discovery by luck" into a triage queue.

Two jobs:
  1. log_query(...)  — one durable row per pilot query with its resolved path and
     outcome. Non-confident outcomes (not-found, abstain, chart-not-found, error)
     are flagged needs_review, so failures surface in a list you can triage
     weekly instead of ambushing you in production.
  2. startup_healthcheck() — ping OpenAI and Supabase once on boot and log a clear
     PASS/FAIL line, so the next bad credential announces itself immediately
     rather than hiding as silent "not found" until someone reads a traceback.

Both are best-effort: they never raise into the request path. Logging failures
are swallowed — telemetry must not break the bot.
"""
import collections
import datetime as dt
import hashlib
import logging
import re

import config
from database import supabase

log = logging.getLogger("vannie.obs")

# Outcomes that didn't confidently answer -> land in the review queue. Some are
# legitimate refusals (out_of_scope, not_in_aip); reviewing them is how you catch
# the FALSE refusals hiding among the correct ones.
REVIEW_PATHS = {
    "not_found", "low_confidence", "chart_not_found", "error",
    "not_in_aip", "out_of_scope", "unresolved", "ambiguous",
}


def _hash_chat(chat_id) -> str | None:
    if chat_id is None:
        return None
    return hashlib.sha256(f"vannie:{chat_id}".encode()).hexdigest()[:16]


def log_query(*, chat_id=None, query="", intent=None, icao=None, path="unknown",
              similarity=None, charts=0) -> None:
    """Insert one query-log row. Best-effort; never raises."""
    if not config.QUERY_LOG_ENABLED:
        return
    row = {
        "chat_hash": _hash_chat(chat_id),
        "query": (query or "")[:2000],
        "intent": intent,
        "icao": icao,
        "path": path,
        "similarity": round(float(similarity), 3) if similarity is not None else None,
        "charts": int(charts or 0),
        "needs_review": path in REVIEW_PATHS,
        "airac": config.AIRAC_CYCLE,
    }
    try:
        supabase.table("vannie_query_log").insert(row).execute()
    except Exception:  # noqa: BLE001 — telemetry must never break the bot
        log.exception("query-log insert failed (path=%s)", path)


def startup_healthcheck() -> bool:
    """Ping OpenAI + Supabase once and log a clear PASS/FAIL line. Returns True if
    both pass. Called on boot so credential problems are loud, not silent."""
    ok = True

    # Supabase: a trivial read that requires a valid key.
    try:
        supabase.table("aip_charts").select("icao_code").limit(1).execute()
        log.info("healthcheck: Supabase OK")
    except Exception as e:  # noqa: BLE001
        ok = False
        log.error("healthcheck: Supabase FAILED — check SUPABASE_KEY (%s)", e)

    # OpenAI: a tiny embedding call that requires a valid key.
    try:
        from agent import get_embedding
        if get_embedding("healthcheck") is None:
            raise RuntimeError("embedding returned None")
        log.info("healthcheck: OpenAI OK")
    except Exception as e:  # noqa: BLE001
        ok = False
        log.error("healthcheck: OpenAI FAILED — check OPENAI_API_KEY (%s)", e)

    log.info("healthcheck: %s", "ALL PASS" if ok else "DEGRADED — see errors above")
    return ok


# ── shared read/aggregate/mark layer (used by triage CLI and the dashboard) ───

def fetch_log(days: int = 7, icao: str | None = None,
              open_only: bool = False, limit: int = 5000) -> list:
    """Rows from the query log, newest first. open_only -> only unhandled review
    items (needs_review and not reviewed)."""
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    q = (supabase.table("vannie_query_log").select("*")
         .gte("created_at", cutoff).order("created_at", desc=True).limit(limit))
    if icao:
        q = q.eq("icao", icao.upper())
    if open_only:
        q = q.eq("needs_review", True).eq("reviewed", False)
    try:
        return q.execute().data or []
    except Exception:  # noqa: BLE001
        log.exception("query-log fetch failed")
        return []


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def summarize(rows: list) -> dict:
    """Aggregate rows into the numbers a dashboard/triage report needs."""
    total = len(rows)
    paths = collections.Counter(r.get("path") for r in rows)
    review = [r for r in rows if r.get("needs_review")]
    open_q = [r for r in review if not r.get("reviewed")]
    sims = [r["similarity"] for r in rows if r.get("similarity") is not None]
    icaos = collections.Counter(r.get("icao") for r in rows if r.get("icao"))
    clusters = collections.Counter(_norm(r["query"]) for r in open_q)
    return {
        "total": total,
        "paths": paths,
        "review": review,
        "open": open_q,
        "avg_sim": (sum(sims) / len(sims)) if sims else None,
        "icaos": icaos,
        "clusters": clusters,
    }


def mark_reviewed(ids: list, note: str | None = None) -> int:
    """Mark rows handled so they leave the open queue. Returns count updated."""
    ids = [int(i) for i in ids if str(i).strip().isdigit()]
    if not ids:
        return 0
    payload = {"reviewed": True,
               "reviewed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    if note:
        payload["reviewed_note"] = note
    try:
        supabase.table("vannie_query_log").update(payload).in_("id", ids).execute()
        return len(ids)
    except Exception:  # noqa: BLE001
        log.exception("mark_reviewed failed")
        return 0


def _esc(s) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_dashboard(rows: list, days: int) -> str:
    """Self-contained HTML dashboard (inline CSS, no external deps, no third-party
    data egress). Read-only; marking reviewed is done via the triage CLI."""
    s = summarize(rows)
    total = s["total"] or 1
    pct_review = 100 * len(s["review"]) // total
    avg = f"{s['avg_sim']:.2f}" if s["avg_sim"] is not None else "—"

    def card(label, value, sub=""):
        return (f'<div class="card"><div class="v">{value}</div>'
                f'<div class="l">{_esc(label)}</div>'
                f'<div class="s">{_esc(sub)}</div></div>')

    cards = "".join([
        card("queries", s["total"], f"last {days} days"),
        card("needs review", len(s["review"]), f"{pct_review}% of traffic"),
        card("open (unhandled)", len(s["open"]), "in the queue now"),
        card("avg similarity", avg, "text answers"),
    ])

    maxp = max(s["paths"].values()) if s["paths"] else 1
    path_rows = ""
    for path, n in s["paths"].most_common():
        flag = "warn" if path in REVIEW_PATHS else "ok"
        w = 100 * n // maxp
        path_rows += (f'<tr><td>{_esc(path)}</td><td class="num">{n}</td>'
                      f'<td class="bar"><span class="{flag}" style="width:{w}%"></span></td></tr>')

    queue = ""
    for qn, n in s["clusters"].most_common(20):
        ex = next(r for r in s["open"] if _norm(r["query"]) == qn)
        queue += (f'<tr><td class="num">×{n}</td><td>{_esc(ex.get("path"))}</td>'
                  f'<td>{_esc(ex.get("icao") or "—")}</td>'
                  f'<td class="id">#{ex.get("id")}</td>'
                  f'<td>{_esc(ex.get("query"))[:80]}</td></tr>')
    if not queue:
        queue = '<tr><td colspan="5" class="empty">Queue clear 🎉</td></tr>'

    tops = "".join(f'<tr><td>{_esc(ic)}</td><td class="num">{n}</td></tr>'
                   for ic, n in s["icaos"].most_common(10)) or \
        '<tr><td colspan="2" class="empty">—</td></tr>'

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vannie · query dashboard</title><style>
*{{box-sizing:border-box}}body{{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
margin:0;background:#0f1419;color:#e6e6e6}}.wrap{{max-width:1000px;margin:0 auto;padding:24px}}
h1{{font-size:18px;margin:0 0 4px}}.muted{{color:#8a96a3;font-size:13px;margin-bottom:20px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:24px}}
.card{{background:#1a2029;border:1px solid #262e3a;border-radius:10px;padding:16px}}
.card .v{{font-size:28px;font-weight:600}}.card .l{{font-size:13px;margin-top:2px}}
.card .s{{font-size:11px;color:#8a96a3}}h2{{font-size:14px;margin:24px 0 8px;color:#b8c2cc}}
table{{width:100%;border-collapse:collapse;background:#1a2029;border-radius:10px;overflow:hidden}}
td,th{{padding:8px 12px;border-bottom:1px solid #232b36;text-align:left;font-size:13px}}
.num{{text-align:right;font-variant-numeric:tabular-nums;width:60px}}.id{{color:#8a96a3;width:60px}}
.bar{{width:40%}}.bar span{{display:block;height:10px;border-radius:5px}}
.bar .ok{{background:#3b82f6}}.bar .warn{{background:#e0a13c}}
.empty{{text-align:center;color:#8a96a3;padding:16px}}
.hint{{font-size:12px;color:#8a96a3;margin-top:8px}}</style></head>
<body><div class="wrap">
<h1>Vannie — query dashboard</h1>
<div class="muted">last {days} days · generated live from vannie_query_log</div>
<div class="cards">{cards}</div>
<h2>Outcome mix</h2><table>{path_rows}</table>
<h2>Open review queue — repeated failures first</h2><table>
<tr><th class="num">n</th><th>path</th><th>icao</th><th class="id">id</th><th>query</th></tr>
{queue}</table>
<div class="hint">Mark handled items so they leave the queue:
<code>python triage.py --mark 123 456 --note "fixed enrichment"</code></div>
<h2>Top aerodromes</h2><table>{tops}</table>
</div></body></html>"""
