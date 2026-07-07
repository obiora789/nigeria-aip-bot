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
              similarity=None, charts=0, qid=None) -> None:
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
        "qid": qid,
    }
    try:
        supabase.table("vannie_query_log").insert(row).execute()
    except Exception:  # noqa: BLE001 — telemetry must never break the bot
        log.exception("query-log insert failed (path=%s)", path)


def record_feedback(qid: str, verdict: str) -> None:
    """Attach a pilot's 👍/👎 to the logged query. A 👎 flags it for review, so
    real wrong answers land in the triage queue."""
    if not qid or verdict not in ("up", "down"):
        return
    payload = {"feedback": verdict}
    if verdict == "down":
        payload["needs_review"] = True
        payload["reviewed"] = False
    try:
        supabase.table("vannie_query_log").update(payload).eq("qid", qid).execute()
    except Exception:  # noqa: BLE001
        log.exception("record_feedback failed (qid=%s)", qid)


def healthcheck() -> tuple[bool, list]:
    """Ping OpenAI + Supabase, log a clear line, and report each component to the
    alerter (which fires on healthy<->degraded transitions). Returns (ok, failures)
    so the caller can surface which credential is bad."""
    import alerting
    failures = []

    # Supabase: a trivial read that requires a valid key.
    try:
        supabase.table("aip_charts").select("icao_code").limit(1).execute()
        alerting.report("Supabase", True)
        log.info("healthcheck: Supabase OK")
    except Exception as e:  # noqa: BLE001
        failures.append("Supabase")
        alerting.report("Supabase", False, "check SUPABASE_KEY")
        log.error("healthcheck: Supabase FAILED — check SUPABASE_KEY (%s)", e)

    # OpenAI: a tiny embedding call that requires a valid key.
    try:
        from agent import get_embedding
        if get_embedding("healthcheck") is None:
            raise RuntimeError("embedding returned None")
        alerting.report("OpenAI", True)
        log.info("healthcheck: OpenAI OK")
    except Exception as e:  # noqa: BLE001
        failures.append("OpenAI")
        alerting.report("OpenAI", False, "check OPENAI_API_KEY")
        log.error("healthcheck: OpenAI FAILED — check OPENAI_API_KEY (%s)", e)

    ok = not failures
    log.info("healthcheck: %s", "ALL PASS" if ok else f"DEGRADED — {failures}")
    return ok, failures


# Back-compat alias: some callers expect a bool.
def startup_healthcheck() -> bool:
    return healthcheck()[0]


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


def prune_logs(before_days: int) -> int:
    """Delete query-log rows OLDER than before_days. Floored at 7 days so recent
    data and the audit trail can never be wiped from the web dashboard. Returns
    the number deleted."""
    before_days = max(7, int(before_days))
    cutoff = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(days=before_days)).isoformat()
    try:
        resp = (supabase.table("vannie_query_log").delete()
                .lt("created_at", cutoff).execute())
        n = len(resp.data or [])
        log.info("prune_logs: deleted %d rows older than %d days", n, before_days)
        return n
    except Exception:  # noqa: BLE001
        log.exception("prune_logs failed")
        return 0


def wipe_logs() -> int:
    """Delete ALL query-log rows. Destructive — CLI-only, never exposed on the web
    dashboard. Returns the number deleted."""
    try:
        resp = (supabase.table("vannie_query_log").delete()
                .gte("created_at", "2000-01-01T00:00:00Z").execute())
        n = len(resp.data or [])
        log.info("wipe_logs: deleted %d rows", n)
        return n
    except Exception:  # noqa: BLE001
        log.exception("wipe_logs failed")
        return 0


def export_csv(rows: list) -> str:
    """Serialize log rows to CSV for offline analysis / audit."""
    import csv
    import io
    cols = ["id", "created_at", "path", "intent", "icao", "similarity", "charts",
            "needs_review", "reviewed", "feedback", "query"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c, "") for c in cols])
    return buf.getvalue()


def _esc(s) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_dashboard(rows: list, days: int, token: str = "") -> str:
    """Self-contained HTML dashboard (inline CSS, no external deps, no third-party
    data egress). Read-only apart from an age-based prune; a full wipe is CLI-only."""
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
        card("👎 flagged", sum(1 for r in rows if r.get("feedback") == "down"),
             "wrong per pilots"),
        card("avg similarity", avg, "text answers"),
    ])

    # pilot 👎 — the strongest error signal: a human said "this was wrong"
    down = [r for r in rows if r.get("feedback") == "down"]
    down_rows = ""
    for r in down[:25]:
        ts = (r.get("created_at") or "")[:16].replace("T", " ")
        down_rows += (f'<tr><td class="id">#{r.get("id")}</td>'
                      f'<td>{_esc(r.get("path"))}</td>'
                      f'<td>{_esc(r.get("icao") or "—")}</td>'
                      f'<td>{_esc((r.get("query") or "")[:70])}</td>'
                      f'<td class="id">{ts}</td></tr>')
    if not down_rows:
        down_rows = '<tr><td colspan="5" class="empty">No 👎 in this window 🎉</td></tr>'

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

    # time-range toggle (carries the token) — daily / weekly / monthly / quarter
    def rng(label, d):
        cls = "on" if d == days else ""
        return f'<a class="rng {cls}" href="?token={_esc(token)}&amp;days={d}">{label}</a>'
    ranges = (rng("Today", 1) + rng("Week", 7) + rng("Month", 30) + rng("90 days", 90))

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
.hint{{font-size:12px;color:#8a96a3;margin-top:8px}}
.rng{{display:inline-block;padding:5px 12px;margin-right:6px;border-radius:8px;
background:#1a2029;border:1px solid #262e3a;color:#b8c2cc;text-decoration:none;font-size:13px}}
.rng.on{{background:#3b82f6;border-color:#3b82f6;color:#fff}}
.tools{{margin-top:28px;padding-top:16px;border-top:1px solid #232b36}}
.tools select,.tools button{{background:#1a2029;border:1px solid #262e3a;color:#e6e6e6;
border-radius:8px;padding:6px 10px;font-size:13px}}
.tools button{{cursor:pointer}}</style></head>
<body><div class="wrap">
<h1>Vannie — query dashboard</h1>
<div class="muted">last {days} days · generated live from vannie_query_log</div>
<div class="rangebar">{ranges}
<a class="rng" href="/dashboard/export.csv?token={_esc(token)}&amp;days={days}">⬇ Export CSV</a></div>
<div class="cards">{cards}</div>
<h2>👎 Flagged wrong by pilots</h2><table>
<tr><th class="id">id</th><th>path</th><th>icao</th><th>query</th><th class="id">when</th></tr>
{down_rows}</table>
<h2>Outcome mix</h2><table>{path_rows}</table>
<h2>Open review queue — repeated failures first</h2><table>
<tr><th class="num">n</th><th>path</th><th>icao</th><th class="id">id</th><th>query</th></tr>
{queue}</table>
<div class="hint">Mark handled items so they leave the queue:
<code>python triage.py --mark 123 456 --note "fixed enrichment"</code></div>
<h2>Top aerodromes</h2><table>{tops}</table>
<div class="tools">
  <form method="post" action="/dashboard/prune"
        onsubmit="return confirm('Delete log entries older than the selected age? This cannot be undone.');">
    <input type="hidden" name="token" value="{_esc(token)}">
    Clear entries older than
    <select name="before_days"><option value="30">30</option><option value="90" selected>90</option></select>
    days <button type="submit">Clear old logs</button>
  </form>
  <div class="hint">Recent entries (last 7 days) are always kept. To wipe everything,
  use the CLI: <code>python triage.py --wipe --yes</code></div>
</div>
</div></body></html>"""
