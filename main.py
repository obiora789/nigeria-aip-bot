"""
main.py — Vannie · Nigeria AIP Reference Assistant (Telegram webhook).

Flow per message:
  verify secret -> dedup -> ACK 200 fast -> (background) extract -> resolve
  -> embed -> search w/ fallback -> gate on max similarity -> extractive reply
  with citation + AIRAC + disclaimer -> deterministic charts.

The heavy work runs in a background task so we acknowledge Telegram within
milliseconds; otherwise Telegram retries the update and we'd pay twice.
"""
import asyncio
import contextvars
import copy
import hmac
import logging
import re
import uuid
from types import SimpleNamespace

from fastapi import Cookie, BackgroundTasks, FastAPI, Form, Header, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

import cache
import config
import resolver
from agent import asks_live, extract_query_parameters, get_embedding
from database import (DataUnavailable, get_aerodrome_data, get_charts, get_charts_smart,
                      search_facts_scoped,
                      get_declared_distances, get_lighting_data,
                      get_runway_physical_data, get_section_text,
                      get_structured, get_subsection_text, search_aip, search_facts)
from models import AIPResult, Resolution, SearchOutcome
import synthesize
import subsection_router
import facts
import memory
import clarify
import observability
import procedures
import sids
from clarify import _rwy_eq
import toc
import scope_refusal
import charges
import disambiguate
from responder import (ambiguous, answer, chart_intro, chart_not_found,
                       comms_reply, data_unavailable, declared_distance_reply, error,
                       grounded_reply,
                       facts_reply, info_block_reply, lighting_data_reply, low_confidence,
                       navaid_reply, not_found, not_in_aip, runway_data_reply,
                       runway_end_reply, ad22_reply, ad26_reply,
                       overview_reply, rwy_char_reply, sid_charts_intro, sid_reply,
                       subsection_reply,
                       unresolved)
from telegram import (answer_callback, clarify_runway_kb, clarify_scope_kb,
                      clarify_type_kb, overview_kb, sid_name_kb, sid_runway_kb,
                      feedback_kb, verify_secret)
from telegram import send_charts as _tg_send_charts
from telegram import send_message as _tg_send_message

logging.basicConfig(level=logging.INFO)
# THE BOT TOKEN WAS IN THE LOGS (2026-11-25). httpx logs every request at INFO
# with its full URL, and Telegram's URL contains the token
# (api.telegram.org/bot<TOKEN>/sendMessage), so Render kept a copy of it on
# every reply. Warnings and errors from httpx still show; Vannie's own log
# lines (vannie.*) are unchanged.
logging.getLogger("httpx").setLevel(logging.WARNING)

# ── audit trail: every reply process() sends is logged with its question ──────
# (2026-10-27). process() opens a buffer; these wrappers copy each outgoing
# message into it; the finally block logs the joined text as the row's reply.
# A context variable keeps concurrent chats apart. run_stress_set.py replaces
# these two names to capture replies, exactly as it did before.
_REPLIES: contextvars.ContextVar = contextvars.ContextVar("vannie_replies", default=None)


def _capture(text) -> None:
    buf = _REPLIES.get()
    if buf is not None:
        buf.append(text or "")


async def send_message(chat_id, text, reply_markup=None):
    _capture(text)
    await _tg_send_message(chat_id, text, reply_markup=reply_markup)


async def send_charts(chat_id, charts, requested_runway=None):
    if charts:
        _capture(f"[{len(charts)} chart(s) sent]")
    await _tg_send_charts(chat_id, charts, requested_runway=requested_runway)
log = logging.getLogger("vannie.main")

app = FastAPI(title="Vannie — Nigeria AIP Reference Assistant")


@app.on_event("startup")
async def _warm() -> None:
    try:
        resolver.load_index()
    except Exception:  # noqa: BLE001
        log.exception("index warmup failed; will lazy-load on first request")
    # Deep check on boot -> loud PASS/FAIL, and an alert if a credential is bad.
    try:
        ok, fails = await asyncio.to_thread(observability.healthcheck)
        if not ok:
            log.error("startup DEGRADED: %s", fails)
    except Exception:  # noqa: BLE001
        log.exception("startup healthcheck errored")
    try:
        redis_ok = await cache.ping()
        log.info("cache backend: %s", "Redis" if redis_ok else "in-memory (no REDIS_URL or unreachable)")
    except Exception:  # noqa: BLE001
        log.exception("cache ping errored")
    # Periodic background monitor so degradation alerts even without a restart.
    if config.DEEP_CHECK_INTERVAL_SEC > 0:
        asyncio.create_task(_health_monitor())
    # Audit items (2026-10-27): say loudly what is misconfigured.
    if not config.TELEGRAM_WEBHOOK_SECRET:
        if config.ALLOW_INSECURE_WEBHOOK:
            log.warning("TELEGRAM_WEBHOOK_SECRET unset and ALLOW_INSECURE_WEBHOOK=1 "
                        "— webhook OPEN (development only)")
        else:
            log.error("TELEGRAM_WEBHOOK_SECRET is not set — every Telegram update "
                      "will be REFUSED (403). Set it and re-register the webhook.")
    for w in config.model_shutdown_warnings():
        log.warning("MODEL: %s", w)
    if not config.PRIVACY_CONTACT:
        log.warning("PRIVACY_CONTACT is not set — /privacy shows no contact")
    log.info("models: %s | prompts %s | app %s", observability._models(),
             observability.prompt_version(), config.APP_VERSION or "unknown")
    if config.RETENTION_INTERVAL_SEC > 0:
        asyncio.create_task(_retention_loop())


async def _retention_loop() -> None:
    """Retention (audit item, 2026-10-27): once at startup, then every
    RETENTION_INTERVAL_SEC, delete query-log rows older than LOG_RETENTION_DAYS
    and expired conversation context."""
    while True:
        try:
            out = await asyncio.to_thread(observability.run_retention)
            log.info("retention: %s", out)
        except Exception:  # noqa: BLE001
            log.exception("retention pass errored")
        await asyncio.sleep(config.RETENTION_INTERVAL_SEC)


async def _health_monitor() -> None:
    """Re-run the deep check on an interval; alerting.report() fires on
    healthy<->degraded transitions (throttled, with recovery notices)."""
    while True:
        await asyncio.sleep(config.DEEP_CHECK_INTERVAL_SEC)
        try:
            await asyncio.to_thread(observability.healthcheck)
        except Exception:  # noqa: BLE001
            log.exception("periodic healthcheck errored")



# --- dashboard auth ---------------------------------------------------------
# The token was previously read from the QUERY STRING and echoed back into
# redirect URLs and every rendered link. URLs leak: browser history, proxy and
# CDN access logs, and the Referer header sent to any external resource the
# page loads. Anyone holding that token reads every pilot query in the log.
#
# It now travels in an HttpOnly cookie set by a POST login, so it is never in a
# URL and JavaScript cannot read it. The query parameter is still ACCEPTED for
# one purpose only — the initial login link — and when used it is immediately
# exchanged for a cookie via a redirect that carries no token.
#
# Comparison is constant-time: `!=` on a secret leaks length and prefix
# information through timing, which is exactly the primitive an attacker needs
# to recover a token byte by byte.
_DASH_COOKIE = "vannie_dash"


def _dash_ok(cookie_token: str | None, query_token: str = "") -> bool:
    """True if either credential matches. Constant-time; empty config = closed."""
    expected = config.DASHBOARD_TOKEN or ""
    if not expected:
        return False
    for candidate in (cookie_token or "", query_token or ""):
        if candidate and hmac.compare_digest(candidate, expected):
            return True
    return False


def _dash_denied() -> Response:
    """404, not 401 — do not confirm the endpoint exists to an unauthenticated
    caller."""
    return Response("not found", status_code=404)


def _set_dash_cookie(resp):
    """HttpOnly + SameSite=Strict blocks JS access and cross-site submission;
    Secure keeps it off plaintext HTTP. 12h expiry bounds a leaked cookie."""
    resp.set_cookie(_DASH_COOKIE, config.DASHBOARD_TOKEN, max_age=12 * 3600,
                    httponly=True, secure=True, samesite="strict", path="/")
    return resp


@app.get("/health")
def health() -> dict:
    """Cheap liveness — no external calls, safe for frequent Render pings."""
    return {"status": "ok", "airac": config.AIRAC_CYCLE}


@app.get("/health/deep")
def health_deep(token: str = "", vannie_dash: str | None = Cookie(default=None)):
    """Deep check (OpenAI + Supabase). Token-gated because it makes API calls."""
    if not _dash_ok(vannie_dash, token):
        return _dash_denied()
    ok, fails = observability.healthcheck()
    return {"status": "ok" if ok else "degraded", "failed": fails,
            "airac": config.AIRAC_CYCLE}


@app.get("/dashboard")
def dashboard(token: str = "", days: int = 30,
              vannie_dash: str | None = Cookie(default=None)):
    """Read-only observability dashboard. Token-gated; disabled unless
    DASHBOARD_TOKEN is set. Renders live from the query log — no third-party
    egress, mutations only via the triage CLI."""
    if not _dash_ok(vannie_dash, token):
        return _dash_denied()
    days = max(1, min(int(days), 90))
    # Arriving with ?token=... is the LOGIN path: set the cookie and bounce to
    # a clean URL, so the secret leaves the address bar (and history) at once.
    if token and not vannie_dash:
        return _set_dash_cookie(
            RedirectResponse(f"/dashboard?days={days}", status_code=303))
    rows = observability.fetch_log(days=days)
    # No token passed to the renderer: links are relative and carry the cookie.
    return HTMLResponse(observability.render_dashboard(rows, days))


@app.post("/dashboard/prune")
def dashboard_prune(token: str = Form(""), before_days: int = Form(90),
                    vannie_dash: str | None = Cookie(default=None)):
    """Age-based prune from the dashboard. Token-gated; floored at 7 days in
    prune_logs so recent data can't be wiped. A full wipe is CLI-only.

    SameSite=Strict on the cookie is what stops a cross-site POST here: this
    endpoint mutates the log, so a forged submission from another origin would
    otherwise be able to prune it."""
    if not _dash_ok(vannie_dash, token):
        return _dash_denied()
    observability.prune_logs(before_days)
    return RedirectResponse("/dashboard?days=30", status_code=303)


@app.get("/dashboard/export.csv")
def dashboard_export(token: str = "", days: int = 30,
                     vannie_dash: str | None = Cookie(default=None)):
    """Download the query log for the window as CSV (offline analysis / audit)."""
    if not _dash_ok(vannie_dash, token):
        return _dash_denied()
    days = max(1, min(int(days), 90))
    rows = observability.fetch_log(days=days)
    csv_text = observability.export_csv(rows)
    return Response(csv_text, media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="vannie_log_{days}d.csv"'})


@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks,
                  x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    if not verify_secret(x_telegram_bot_api_secret_token):
        return Response(status_code=403)

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return {"status": "ignored"}

    if await cache.already_seen(payload.get("update_id")):
        return {"status": "duplicate"}

    # Button taps (👍/👎) arrive as callback_query, not message.
    cb = payload.get("callback_query")
    if cb:
        background.add_task(handle_feedback, cb)
        return {"status": "accepted"}

    msg = payload.get("message") or payload.get("edited_message") or {}
    text = msg.get("text")
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not text or chat_id is None:
        return {"status": "ignored"}

    if await cache.throttled(chat_id):
        return {"status": "throttled"}

    background.add_task(process, chat_id, text)
    return {"status": "accepted"}


async def handle_feedback(cb: dict) -> None:
    """Route a button tap: 👍/👎 feedback, or a chart-clarification answer. Never raises."""
    try:
        data = cb.get("data") or ""
        cid = cb.get("id")
        # The tapper's own id is the reliable reply target in a 1:1 chat;
        # callback_query.message can be absent or not the human's chat.
        chat_id = (((cb.get("message") or {}).get("chat") or {}).get("id")
                   or (cb.get("from") or {}).get("id"))

        if data.startswith("fb:"):
            _, verdict, qid = data.split(":", 2)
            await asyncio.to_thread(observability.record_feedback, qid, verdict)
            msg = "Thanks — logged." if verdict == "up" else "Thanks — flagged for review."
            if cid:
                await answer_callback(cid, msg)
            return

        if data.startswith("scope:") and chat_id is not None:
            parts = data.split(":")
            if len(parts) < 4:
                if cid:
                    await answer_callback(cid)
                return
            kind, sid, qid = parts[1], parts[2], parts[3]
            if cid:
                await answer_callback(cid, sid)     # stop the spinner at once
            try:
                ctx = await asyncio.to_thread(memory.load, chat_id)
                pending = ctx.get("pending") or {}
                # qid guard: a stale button must not act on a replaced request.
                if pending.get("kind") != "scope_clar" or pending.get("qid") != qid:
                    await send_message(chat_id,
                                       "That request expired — please ask again.")
                    return
                # Re-run the ORIGINAL question with the scope now pinned. The
                # chosen scope is carried in the callback data, so nothing is
                # re-resolved and the ambiguity cannot recur.
                original = pending.get("query") or sid
                if kind == "AD":
                    # SUBSTITUTE THE IDENT FOR THE AERODROME NAME.
                    #
                    # The pilot has just said they meant the aerodrome, so the
                    # ident is no longer what they are asking about — and it is
                    # actively harmful to leave in: "What is the frequency of
                    # ABC?" embeds poorly against Abuja's AD 2.18 comms text,
                    # because ABC is a NAVAID ident that appears nowhere in it.
                    #
                    # Measured: the re-run scored 39% for ABC, 33% for SAB and
                    # 32% for MIU — all just under SIMILARITY_THRESHOLD (0.40),
                    # so the search abstained and the tap produced "I couldn't
                    # find a confident match". The abstention was correct; the
                    # query it was given was not.
                    _name = resolver.aerodrome_full_name(sid) or sid
                    _ident = (pending.get("ident") or "").strip()
                    if _ident:
                        original = re.sub(rf"\b{re.escape(_ident)}\b", _name,
                                          original, flags=re.I)
                await process(chat_id, original, force_scope=(kind, sid))
            except Exception:  # noqa: BLE001 — surface, never vanish
                log.exception("scope clarification callback failed")
                await send_message(
                    chat_id,
                    "Sorry — I couldn't finish that. Please ask again, naming "
                    "the aerodrome or the navaid.")
            return

        if data.startswith("alt:") and chat_id is not None:
            # "Did you mean ...?" under a default answer (disambiguate.py).
            # The callback carries the family and the place, so there is no
            # stored state to expire or be overwritten.
            if cid:
                await answer_callback(cid)          # stop the spinner at once
            try:
                got = disambiguate.from_callback(data)
                if not got:
                    await send_message(chat_id, "That button is no longer valid — please ask again.")
                    return
                family, question = got
                await process(chat_id, question, from_choice=True,
                              log_as=f"[alt:{family}] {question}")
            except Exception:  # noqa: BLE001 — surface, never vanish
                log.exception("alternative-reading callback failed")
                await send_message(chat_id, "Sorry — I couldn't finish that. "
                                            "Please ask again more specifically.")
            return

        if data.startswith("amb:") and chat_id is not None:
            # Ask-first buttons from messages sent before 2026-10-26.
            parts = data.split(":")
            if len(parts) < 3:
                if cid:
                    await answer_callback(cid)
                return
            idx, qid = parts[1], parts[2]
            if cid:
                await answer_callback(cid)          # stop the spinner at once
            try:
                ctx = await asyncio.to_thread(memory.load, chat_id)
                question = disambiguate.chosen_question(ctx.get("pending") or {},
                                                        idx, qid)
                if not question:
                    # qid guard: a stale button must not act on a replaced request.
                    await send_message(chat_id, "That request expired — please ask again.")
                    return
                await process(chat_id, question, from_choice=True)
            except Exception:  # noqa: BLE001 — surface, never vanish
                log.exception("ambiguity choice callback failed")
                await send_message(chat_id, "Sorry — I couldn't finish that. "
                                            "Please ask again more specifically.")
            return

        if data.startswith("ov:") and chat_id is not None:
            # A topic under the aerodrome summary card. The callback carries the
            # topic and the ICAO, so there is no stored state to expire.
            if cid:
                await answer_callback(cid)
            try:
                parts = data.split(":")
                topic, icao = (parts[1], parts[2]) if len(parts) == 3 else ("", "")
                if topic not in OVERVIEW_TOPICS or icao not in resolver.VALID_ICAO:
                    await send_message(chat_id, "That button is no longer valid — please ask again.")
                    return
                question = OVERVIEW_TOPICS[topic].format(icao=icao)
                await process(chat_id, question, from_choice=True,
                              log_as=f"[overview:{topic}] {question}")
            except Exception:  # noqa: BLE001 — surface, never vanish
                log.exception("overview topic callback failed")
                await send_message(chat_id, "Sorry — I couldn't finish that. Please ask again.")
            return

        if data.startswith("sid:") and chat_id is not None:
            parts = data.split(":")
            if len(parts) < 4:
                if cid:
                    await answer_callback(cid)
                return
            dim, val, qid = parts[1], parts[2], parts[3]
            if cid:
                await answer_callback(cid, val)
            try:
                ctx = await asyncio.to_thread(memory.load, chat_id)
                pending = ctx.get("pending") or {}
                if pending.get("kind") != "sid_clar" or pending.get("qid") != qid:
                    await send_message(chat_id, "That SID request expired — please ask again.")
                    return
                runway = val if dim == "rwy" else (pending.get("runway") or "")
                name = val if dim == "name" else (pending.get("name") or "")
                res = SimpleNamespace(icao=pending["icao"],
                                      label=pending.get("label") or pending["icao"])
                new_qid = uuid.uuid4().hex[:12]
                await asyncio.to_thread(
                    observability.log_query, chat_id=chat_id,
                    query=f"[sid {dim}={val}] {runway} {name}".strip(),
                    intent="chart_retrieval", icao=pending["icao"], path="sid",
                    qid=new_qid)
                kb = feedback_kb(new_qid)

                async def send_info(text_: str) -> None:
                    await send_message(chat_id, text_, reply_markup=kb)

                await _run_sid_decision(chat_id, res, pending["icao"], runway, name,
                                        send_info)
            except Exception:  # noqa: BLE001 — surface, never vanish
                log.exception("SID choice callback failed")
                await send_message(chat_id, "Sorry — I couldn't finish that SID request. "
                                            "Please ask again (e.g. \"SID Lagos RWY 18R\").")
            return

        if data.startswith("clar:") and chat_id is not None:
            parts = data.split(":")
            if len(parts) < 4:
                if cid:
                    await answer_callback(cid)
                return
            dim, val, qid = parts[1], parts[2], parts[3]
            if cid:
                await answer_callback(cid, val)      # stop the spinner immediately
            try:
                ctx = await asyncio.to_thread(memory.load, chat_id)
                pending = ctx.get("pending") or {}
                # qid guard: a stale button (pending replaced/expired) must not act.
                if pending.get("kind") != "chart_clar" or pending.get("qid") != qid:
                    await send_message(chat_id,
                                       "That chart request expired — please ask again.")
                    return
                ptype = pending.get("type") or ""
                runway = pending.get("runway") or ""
                if dim == "type":
                    ptype = val
                elif dim == "rwy":
                    runway = val
                res = SimpleNamespace(icao=pending["icao"],
                                      label=pending.get("label") or pending["icao"])
                new_qid = uuid.uuid4().hex[:12]
                await asyncio.to_thread(
                    observability.log_query, chat_id=chat_id,
                    query=f"[clarify {dim}={val}] {ptype} {runway}".strip(),
                    intent="chart_retrieval", icao=pending["icao"], path="chart",
                    qid=new_qid)
                kb = feedback_kb(new_qid)

                async def send_info(text_: str) -> None:
                    await send_message(chat_id, text_, reply_markup=kb)

                await _run_chart_decision(chat_id, res, pending["icao"], ptype,
                                          runway, send_info)
            except Exception:  # noqa: BLE001 — surface, never vanish
                log.exception("chart clarification callback failed")
                await send_message(
                    chat_id,
                    "Sorry — I couldn't finish that chart request. Please ask again "
                    "(e.g. \"VOR approach plate for Lagos RWY 18L\").")
            return
    except Exception:  # noqa: BLE001
        log.exception("handle_feedback failed")


# Procedure types that imply a published chart (not a frequency/service).
# Deliberately excludes tower/atis/ground/approach-control — those are frequencies,
# so a question like "Lagos tower frequency" must NOT pull a chart.
_CHART_HINTS = ("ils", "rnav", "gnss", "rnp", "sid", "star", "iac", "vac")


def _wants_chart(ex) -> bool:
    """Charts are fetched only when the pilot actually asked for one."""
    if ex.intent == "chart_retrieval":
        return True
    pt = (ex.procedure_type or "").lower()
    return any(h in pt for h in _CHART_HINTS)


_APPROACH_RE = re.compile(r"\b(ils|vor|rnav|gnss|rnp|ndb|loc)\b", re.I)


def _is_approach(ex, text) -> bool:
    """True for an instrument APPROACH (which has holding/letdown/missed-approach
    procedures) — not aerodrome/parking/obstacle/SID/STAR charts."""
    t = f"{ex.procedure_type or ''} {text}".lower()
    if any(w in t for w in ("sid", "star", "departure", "arrival", "parking",
                            "docking", "obstacle", "terrain", "aerodrome chart")):
        return _APPROACH_RE.search(t) is not None and "approach" in t
    return ("approach" in t or _APPROACH_RE.search(t) is not None
            or any(w in t for w in ("holding", "letdown", "let-down",
                                    "missed approach")))


_PLATE_POINTER = (
    "The approach is depicted on the plate below — the descent (letdown) profile, "
    "the missed-approach note, and the hold on the plan view. Read the procedures "
    "directly from the chart.")


async def _send_approach_procedures(chat_id, ex, res, send_info):
    """For an instrument approach: show the AD 2.22 Holding/Letdown/Missed-Approach
    VERBATIM, scoped to the exact requested approach — but ONLY when it parses
    cleanly and unambiguously. Otherwise defer to the plate. Never a partial or
    spliced procedure. send_info is passed in (it's a per-request closure)."""
    result = None
    if config.PROCEDURES_TEXT_ENABLED and ex.runway and res.icao:
        full = await asyncio.to_thread(get_section_text, res.icao, "AD 2.22")
        if full:
            result = procedures.extract(full, ex.runway, ex.procedure_type or "")
    if result:
        await send_info(procedures.format_message(res.label, result))
    else:
        await send_info(_PLATE_POINTER)


async def _run_chart_decision(chat_id, res, chart_icao, ptype, runway, send_info):
    """Fetch the aerodrome's charts, decide, and either ASK a clarifying question
    (storing pending + tappable options built from the real catalogue) or SEND the
    plate(s). Reused by the chart branch and the clarification-tap handler. Fails
    safe: ambiguous-but-unanswerable shows all matches; nothing found -> not_found."""
    shim = SimpleNamespace(procedure_type=ptype or None, runway=runway or None)
    all_charts = await asyncio.to_thread(get_charts_smart, chart_icao, "", "")
    d = clarify.decide(all_charts, ptype or "", runway or "")

    if d.action == "ask_type":
        qid = uuid.uuid4().hex[:12]
        await asyncio.to_thread(memory.save_chart_pending, chat_id, chart_icao,
                                res.label, ptype, runway, qid)
        await send_message(chat_id, f"Which approach for {res.label}? Tap one:",
                           reply_markup=clarify_type_kb(d.options, qid))
        return "ask_type"
    if d.action == "ask_runway":
        qid = uuid.uuid4().hex[:12]
        await asyncio.to_thread(memory.save_chart_pending, chat_id, chart_icao,
                                res.label, d.type, runway, qid)
        await send_message(chat_id,
                           f"{d.type} approach for {res.label} — which runway? Tap one:",
                           reply_markup=clarify_runway_kb(d.options, qid))
        return "ask_runway"
    if d.action == "not_found":
        await send_info(chart_not_found(res, shim))
        return "not_found"
    # send: intro -> procedures (verbatim/pointer) -> the narrowed plate(s)
    #
    # The shim must describe what was DECIDED, not what the pilot happened to
    # type. When decide() resolves to exactly ONE plate without needing to ask,
    # the runway and approach type are fully determined by that plate — but
    # `runway`/`ptype` still hold the original, often EMPTY, request. Passing
    # those through handed procedures.extract() an empty req_rwy, which returns
    # None on its first line, so the reply silently degraded to the plate
    # pointer plus the chart, with no Holding/Letdown/Missed text at all.
    #
    # That is precisely why procedures appeared after a TAP (the callback
    # supplies the runway) but never on a direct send: same code path, one
    # missing value. Confirmed against clarify.decide() — e.g. an aerodrome
    # publishing a single ILS approach for RWY 05 returns action="send" with
    # that chart, while the shim carried runway=None.
    if len(d.charts) == 1:
        _only = d.charts[0]
        ptype = ptype or clarify.approach_label(
            getattr(_only, "procedure_type", "") or "") or ""
        runway = runway or (getattr(_only, "runway", "") or "")
        shim = SimpleNamespace(procedure_type=ptype or None, runway=runway or None)
    await send_info(chart_intro(res, shim))
    await _send_approach_procedures(chat_id, shim, res, send_info)
    await send_charts(chat_id, d.charts, requested_runway=runway or None)
    return "send"


async def _run_sid_decision(chat_id, res, icao, runway, name, send_info) -> bool:
    """SIDs work like approaches (Obiora, 2026-11-21): ask the runway, then the
    SID, then send that one SID — its published description when it proves it
    belongs to that SID (sids.description), and the aerodrome's SID chart
    pages. Options come from the aerodrome's own AD 2.22 headings, never from
    the pilot's wording. Returns False when the aerodrome publishes no SID
    headings, so the caller keeps its existing behaviour instead of claiming
    there is no SID."""
    text = await asyncio.to_thread(get_section_text, icao, "AD 2.22")
    d = sids.decide(sids.catalogue(text), runway or "", name or "")
    if d.action == "none":
        return False
    if d.action in ("ask_runway", "no_runway"):
        qid = uuid.uuid4().hex[:12]
        await asyncio.to_thread(memory.save_sid_pending, chat_id, icao, res.label,
                                "", name or "", qid, d.options)
        lead = (f"I can't find a SID for RWY {d.runway} at {res.label} in my copy "
                f"of AD 2.22. " if d.action == "no_runway" else "")
        await send_message(chat_id, f"{lead}SIDs from {res.label} — which runway? Tap one:",
                           reply_markup=sid_runway_kb(d.options, qid))
        return True
    if d.action == "ask_sid":
        qid = uuid.uuid4().hex[:12]
        await asyncio.to_thread(memory.save_sid_pending, chat_id, icao, res.label,
                                d.runway, "", qid, d.options)
        await send_message(chat_id,
                           f"SIDs from {res.label} RWY {d.runway} — which one? Tap one:",
                           reply_markup=sid_name_kb(d.options, qid))
        return True
    sid = d.sids[0]
    every = await asyncio.to_thread(get_charts_smart, icao, "", "")
    charts = [c for c in every if "sid" in (c.procedure_type or "").lower()]
    await send_info(sid_reply(res, sid, sids.description(text, sid), bool(charts)))
    if charts:
        # SID pages carry their runway once labelled correctly
        # (16_fix_chart_labels.sql); send that runway's page when there is
        # one, otherwise every SID page with a note saying so.
        same = [c for c in charts if c.runway and _rwy_eq(sid.runway, c.runway)]
        await send_info(sid_charts_intro(res, sid.runway if same else None))
        await send_charts(chat_id, (same or charts)[: config.MAX_CHARTS])
    return True


# Topic buttons under the summary card: each runs a fixed question through the
# normal pipeline, so every topic keeps its existing safe handler. The ICAO is
# used rather than a name so the question resolves to exactly this aerodrome.
OVERVIEW_TOPICS = {
    "rwy": "Runway physical characteristics at {icao}",
    "freq": "ATS communication frequencies at {icao}",
    "app": "Approach plate for {icao}",
    "sid": "SID {icao}",
    "adc": "Aerodrome chart for {icao}",
    "met": "Meteorological information provided at {icao}",
    "hrs": "Operational hours at {icao}",
}


async def _send_overview(chat_id, res, qid) -> None:
    """The same summary card for every aerodrome (Obiora, 2026-11-21), then
    topic buttons. Only topics this aerodrome actually has are offered:
    approaches only where an approach chart is stored and it is not a
    heliport, SIDs only where a SID chart is stored."""
    ad22, rwys, comms, charts = await asyncio.gather(
        asyncio.to_thread(get_structured, res.icao, "2.2"),
        asyncio.to_thread(get_runway_physical_data, res.icao),
        asyncio.to_thread(get_structured, res.icao, "2.18"),
        asyncio.to_thread(get_charts_smart, res.icao, "", ""))
    kinds = " | ".join((c.procedure_type or "").lower() for c in charts)
    heliport = await asyncio.to_thread(disambiguate.is_heliport, res.icao)
    topics = ["rwy", "freq"]
    if "approach" in kinds and not heliport:
        topics.append("app")
    if "sid" in kinds:
        topics.append("sid")
    if "aerodrome" in kinds:
        topics.append("adc")
    topics += ["met", "hrs"]
    kb = overview_kb(res.icao, topics)
    kb["inline_keyboard"] += feedback_kb(qid)["inline_keyboard"]
    await send_message(chat_id, overview_reply(res, ad22, rwys, comms), reply_markup=kb)


_AVIATION_INTENTS = {"chart_retrieval", "procedure_lookup", "frequency_retrieval",
                     "runway_data", "aerodrome_fact", "airspace_lookup"}


# A message that is ONLY a place name, once the aerodrome itself is removed.
# "Lagos", "DNMM", "lagos please", "it's Kano" -> nothing of substance is left.
_FILLER_ONLY_RE = re.compile(
    r"^(it'?s|its|the|a|at|in|for|to|is|use|try|do|please|pls|thanks?|ok|okay|"
    r"yes|yeah|airport|aerodrome|dn[a-z]{2})$", re.I)


def _bare_aerodrome(ex, raw: str = "") -> bool:
    """True when the message is essentially just naming a place ('Lagos',
    'DNMM') — the safe signal that it answers an earlier 'which aerodrome?'.

    STRUCTURAL, not intent-based. The previous test required
    intent == "icao_lookup", which is a judgement the extraction LLM has to
    make about a single word with no context. It frequently returns
    aerodrome_fact instead (and backstop #7 rewrites general_greeting to
    aerodrome_fact too), so the test failed and the pending request was
    silently dropped.

    Confirmed live: "Show me ILS approach for RWY 18L" -> "Which aerodrome?"
    -> "Lagos" -> the bot answered with Lagos's AD 2.1 city and aerodrome
    NAME. The pilot's intent, procedure type and runway were all stored
    correctly in `pending` and never merged, because of the intent check
    alone.

    What makes it safe is the same thing the old docstring wanted: the message
    must carry NO field of its own. "elevation of Abuja" still fails this test
    and is treated as a new query, because "elevation" survives the filter."""
    if ex.procedure_type or ex.runway:
        return False
    if ex.intent == "icao_lookup":
        return True
    # Strip the aerodrome the extractor found, then see if anything meaningful
    # remains. Nothing left -> the message was only a place name.
    residue = raw or ""
    for token in (ex.aerodrome_name or "", ex.icao_code or ""):
        if token:
            residue = re.sub(re.escape(token), " ", residue, flags=re.I)
    words = [w for w in re.findall(r"[A-Za-z']{2,}", residue)
             if not _FILLER_ONLY_RE.match(w)]
    return not words


def _aviation_intent(ex) -> bool:
    return ex.intent in _AVIATION_INTENTS


def _names_a_place(ex) -> bool:
    """True if the message references a specific aerodrome — by name or code —
    even one we can't resolve. When a place IS named but unresolved (e.g.
    'Jalingo'), a follow-up carry must NOT fire: we refuse for the named place
    rather than silently answering for the last aerodrome."""
    return bool(getattr(ex, "aerodrome_name", None) or getattr(ex, "icao_code", None))


# A bare approach type or runway typed instead of tapping a clarify button.
_BARE_CLAR_RE = re.compile(r"^(ILS|VOR|RNAV|GNSS|RNP|NDB|\d{2}[LRC]?)$", re.I)

# AD 2.2 aerodrome geographic/admin fields — routed to a fetch-by-section then
# synthesize, because the general vector search under-retrieves the secondary
# paired values (e.g. reference temperature sits behind elevation in one field).
# Fields that genuinely live in AD 2.2 (aerodrome geographical/administrative
# data), answerable from the structured get_aerodrome_data() record.
#
# "transition altitude|level" was REMOVED from this list. Both are AD 2.17
# fields, verified directly against the AIP:
#   DNAA — "5 Transition altitude 5 000 ft/1 500 m AMSL" ... "7 Remarks
#          Transition level: FL 65"
#   DNMM — "5 Transition altitude 3 500 ft/1 067 m AMSL" ... "7 Remarks
#          Transition level: FL 50"
# both sitting in the ATS airspace table alongside "ATS unit call sign",
# "CTR/TMA" and "Hours of applicability". AD 2.2 has never held either value,
# so this branch could only ever fall through to whatever AD 2.2 DID have --
# which is why "what is the transition level for Abuja" answered "DNAA
# elevation 342.0m/1122.0ft" at a confident 100% match.
#
# subsection_router.detect_subsection() already returns "AD 2.17" for all of
# "transition level/altitude" phrasings, so removing them here is sufficient:
# the query now flows to the AD 2.17 path that owns the data.
_AERODROME_DATA_RE = re.compile(
    r"\b(reference temp\w*|ref\.?\s?temp\w*|magnetic variation|mag\.?\s?var\w*|"
    r"annual change|aerodrome reference point|\barp\b|geoid|"
    r"aerodrome elevation|elevation of the aerodrome)\b",
    re.I)


async def _admin_health_report() -> str:
    """Operator health snapshot: proves the full webhook->process->reply path AND
    that OpenAI + Supabase are reachable."""
    ok, fails = await asyncio.to_thread(observability.healthcheck)
    redis_ok = await cache.ping()
    return "\n".join([
        f"Vannie status — {'ALL OK' if ok else 'DEGRADED'}",
        f"• OpenAI: {'OK' if 'OpenAI' not in fails else 'FAIL'}",
        f"• Supabase: {'OK' if 'Supabase' not in fails else 'FAIL'}",
        f"• Cache: {'Redis' if redis_ok else 'in-memory'}",
        f"• AIRAC: {config.AIRAC_CYCLE}",
        f"• Models: {observability._models()}",
        f"• Prompts: {observability.prompt_version()} · App: {config.APP_VERSION or 'unknown'}",
        f"• Audit columns: {'OK' if observability._audit_columns_ok else 'MISSING — run sql/14_audit_trail.sql'}",
    ] + [f"⚠ {w}" for w in config.model_shutdown_warnings()])


async def _admin_stats_report() -> str:
    """Operator pulse from the query log: volume and the review backlog."""
    rows = await asyncio.to_thread(observability.fetch_log, 1)   # last 24h
    s = observability.summarize(rows)
    top = ", ".join(f"{ic}({n})" for ic, n in s["icaos"].most_common(3)) or "—"
    pct = (100 * len(s["review"]) // s["total"]) if s["total"] else 0
    return "\n".join([
        "Vannie — last 24h",
        f"• queries: {s['total']}",
        f"• needs review: {len(s['review'])} ({pct}%)",
        f"• open (unhandled): {len(s['open'])}",
        f"• top aerodromes: {top}",
    ])


def _airspace_redirect(ex, res, query: str):
    """An airspace question that is really about one aerodrome's OWN airspace
    ("vertical limits of the Kano control zone") is answered from that
    aerodrome's AD 2.17: returns the aerodrome's Resolution, else None.

    Its own function (2026-11-19) so check_enr_reachability.py can apply the
    same step the live bot applies after resolve() — the audit could not tell
    whether it also captures a genuine ENR airspace ("KANO EAST SECTOR
    vertical limits"), and only measurement can.
    """
    if not (res.is_national and res.reference == "AIRSPACE"):
        return None
    # A NAMED ENR AIRSPACE KEEPS ITS OWN RECORD (measured 2026-11-20).
    # check_enr_reachability.py --fields: 54 of 84 questions about a TMA or
    # sector ("LAGOS TMA vertical limits", "lateral limits of KANO EAST
    # SECTOR") were resolved to the ENR 2.1 entity and then sent here to the
    # aerodrome's AD 2.17 because the name contains "Lagos"/"Kano". The
    # redirect exists for airspace with no record of its own (a CTR), so it
    # only applies when resolve() found no specific entity.
    if res.scope_id:
        return None
    _sub = synthesize._normalise_subsection(getattr(ex, "ad2_subsection", None))
    _kw = subsection_router.detect_subsection(query)
    if not (_sub or _kw):
        return None
    # Prefer what the LLM extracted, but fall back to scanning the
    # RAW QUERY for any known aerodrome name or alias. Depending on
    # ex.aerodrome_name alone is fragile: for "lagos control zone"
    # the extractor can read the whole phrase as an airspace NAME
    # and leave aerodrome_name null, which silently disabled this
    # redirect and sent the query to ENR 1.1 at 63%.
    _hint = res.aerodrome_hint or ex.aerodrome_name
    _scan_icao = None
    if not _hint:
        # match_name() returns ICAO CODES, so feed it back as
        # icao_code — assigning it to aerodrome_name would fail,
        # since 'DNMM' is not an alias of itself.
        _scan = resolver.match_name(query)
        if len(_scan) == 1:
            _scan_icao = next(iter(_scan))
    if not (_hint or _scan_icao):
        return None
    _ex2 = ex.model_copy() if hasattr(ex, "model_copy") else copy.copy(ex)
    _ex2.intent = "aerodrome_fact"
    if _scan_icao:
        _ex2.icao_code = _scan_icao
        _ex2.aerodrome_name = None
    else:
        _ex2.aerodrome_name = _hint
    _redirect = resolver.resolve(_ex2)
    if not _redirect.icao:
        return None
    log.info("airspace query is about %s's own %s -> %s",
             _hint or _scan_icao, _sub or _kw, _redirect.icao)
    return _redirect


async def process(chat_id: int, text: str, force_scope=None,
                  from_choice: bool = False, alt_button=None,
                  log_as=None) -> None:
    """`force_scope` is (scope_kind, scope_id) chosen from a clarification
    button. It PINS the resolution, so the same ambiguity cannot recur on the
    re-run and the pilot is never asked the same question twice."""
    """All heavy lifting; runs after the 200 ack. Never raises to the caller."""
    rec = {"intent": None, "icao": None, "path": "unknown",
           "similarity": None, "charts": 0, "qid": uuid.uuid4().hex[:12]}
    _replies: list = []
    _replies_token = _REPLIES.set(_replies)   # audit trail: what this question was told
    kb = feedback_kb(rec["qid"])   # 👍/👎 buttons
    if alt_button:                 # "Did you mean ...?" (disambiguate.py), own row
        kb["inline_keyboard"].append([alt_button])

    async def send_info(text_: str) -> None:
        """Any INFORMATIONAL reply (answer, refusal, abstention, mapping, facts,
        structure, chart result). Always carries the 👍/👎 feedback keyboard.
        Non-informational messages (greeting, help, system error, clarification
        prompts, context prefix) use plain send_message and get no buttons."""
        await send_message(chat_id, text_, reply_markup=kb)

    try:
        # Commands: answered deterministically, no LLM call.
        cmd = text.strip().lower().split("@")[0]
        if cmd in ("/start", "/help"):
            rec["path"] = "help"
            await send_message(chat_id, config.HELP)
            return
        # /reset is for PILOTS, not operators — deliberately ungated. Vannie
        # carries an aerodrome between turns so "and the TORA?" works, but that
        # same memory can pin the wrong thing: a VOR ident answered as its
        # aerodrome once, and every later question then inherited it
        # ("Using your last aerodrome, Abuja"). A pilot needs a way out that
        # does not depend on knowing why.
        # Privacy (audit item, 2026-10-27). Neither request is itself logged.
        if cmd == "/privacy":
            rec["path"], rec["skip_log"] = "privacy", True
            await send_message(chat_id, config.privacy_notice())
            return
        if cmd in ("/deletemydata", "/delete_my_data"):
            rec["path"], rec["skip_log"] = "erasure", True
            ctx_ok = await asyncio.to_thread(memory.clear, chat_id)
            try:
                n = await asyncio.to_thread(observability.delete_chat_logs, chat_id)
            except Exception:  # noqa: BLE001 — surface, never claim success
                log.exception("erasure failed")
                await send_message(chat_id, "I couldn't delete your records just now. "
                                            "Please try again shortly.")
                return
            if n is None:
                body = ("Done. Your logged questions are not linked to your Telegram "
                        "ID, so there was nothing to delete against it.")
            else:
                body = f"Done. I deleted {n} logged question(s) linked to your Telegram ID."
            if not ctx_ok:
                body += " (I couldn't clear the aerodrome I was remembering; it expires on its own.)"
            await send_message(chat_id, body)
            return
        if cmd in ("/reset", "/clear", "/forget"):
            rec["path"] = "reset"
            ok = await asyncio.to_thread(memory.clear, chat_id)
            await send_message(
                chat_id,
                "Cleared. I've forgotten the aerodrome and any question I was "
                "waiting on — ask me anything fresh."
                if ok else
                "I couldn't clear that just now. Try again shortly; until then, "
                "name the aerodrome explicitly in your question.")
            return

        # Operator-only diagnostics. Non-admins are ignored silently — the
        # commands don't exist for them (no info leak, no OpenAI/Supabase cost).
        if cmd in ("/health", "/stats"):
            if not (config.ADMIN_CHAT_ID and str(chat_id) == str(config.ADMIN_CHAT_ID)):
                rec["path"] = "ignored"
                return
            rec["path"] = "admin"
            report = (await _admin_health_report() if cmd == "/health"
                      else await _admin_stats_report())
            await send_message(chat_id, report)
            return

        # Typed answer to a pending SID choice: a runway, or one of the SIDs
        # that were offered (compared with the offered buttons, nothing else).
        p_s = {}
        if len(text.split()) <= 3:          # a reply to buttons is a few words at most
            ctx_s = await asyncio.to_thread(memory.load, chat_id)
            p_s = ctx_s.get("pending") or {}
        if p_s.get("kind") == "sid_clar":
            typed = re.sub(r"\s+", "", text).upper()
            offered = {re.sub(r"\s+", "", o).upper().replace("RWY", ""): o
                       for o in (p_s.get("options") or [])}
            pick = offered.get(typed.replace("RWY", "").replace("RUNWAY", ""))
            if pick:
                is_rwy = not p_s.get("runway")   # runway buttons are offered first
                res_shim = SimpleNamespace(icao=p_s["icao"],
                                           label=p_s.get("label") or p_s["icao"])
                rec["path"], rec["icao"] = "sid", p_s["icao"]
                await _run_sid_decision(chat_id, res_shim, p_s["icao"],
                                        pick if is_rwy else p_s.get("runway") or "",
                                        "" if is_rwy else pick, send_info)
                return

        # Free-text answer to a pending chart clarification ("VOR", "18L") — treat
        # like a button tap so pilots who type instead of tapping still get through.
        if _BARE_CLAR_RE.match(text.strip()):
            ctx0 = await asyncio.to_thread(memory.load, chat_id)
            p = ctx0.get("pending") or {}
            if p.get("kind") == "chart_clar":
                tok = text.strip().upper()
                ptype = p.get("type") or ""
                runway = p.get("runway") or ""
                if clarify.norm_type(tok):
                    ptype = tok
                else:
                    runway = tok
                res_shim = SimpleNamespace(icao=p["icao"],
                                           label=p.get("label") or p["icao"])
                rec["path"], rec["icao"] = "chart", p["icao"]
                await _run_chart_decision(chat_id, res_shim, p["icao"], ptype,
                                          runway, send_info)
                return

        # 1) extract (sync SDK -> threadpool). The double-meaning check is its
        #    own short model call (disambiguate.py) and runs at the same time,
        #    so it adds no wait. Skipped for a rewritten question (default
        #    reading or a "Did you mean" tap): that wording has one meaning.
        _amb = (None if from_choice else
                asyncio.create_task(asyncio.to_thread(disambiguate.pick, text)))
        ex = await asyncio.to_thread(extract_query_parameters, text)
        if ex is None:
            rec["path"] = "error"
            await send_message(chat_id, error())
            return
        rec["intent"] = ex.intent

        if ex.intent == "general_greeting":
            rec["path"] = "greeting"
            await send_message(chat_id, config.GREETING)
            return

        # LIVE QUESTIONS ARE REFUSED BEFORE ANYTHING ELSE CAN ANSWER THEM.
        # The TA enumeration and the ToC below run ahead of the out_of_scope
        # check on purpose — a structure question mis-tagged out_of_scope
        # should still get its ToC answer. But that ordering also let a live
        # question reach them first. A question whose answer needs a live
        # source (NOTAM, ATIS, METAR, ATC) is refused here, whatever else it
        # resembles. This reads the model's judgement of the ANSWER, never the
        # wording: "which aerodromes use 5000 ft TA today" is published AD 2.17
        # data and goes straight past this gate. agent._backstop has already
        # forced the intent; this keeps the order of checks honest.
        if asks_live(ex):
            rec["path"] = "out_of_scope:live"
            await send_info(scope_refusal.out_of_scope(ex))
            return

        # Cross-aerodrome enumeration ("which aerodromes use 5000 ft TA") —
        # structured-facts lookup, not retrieval.
        if facts.is_ta_enumeration(text):
            ans = facts.answer_ta_enumeration(text)
            if ans:
                rec["path"] = "facts"
                await send_info(ans)
                return

        # Structure/meta questions ("which part of the AIP covers X") are about
        # the document's organisation — answer from the ToC, never retrieval.
        if toc.is_structure_question(text):
            ans = toc.answer(text)
            if ans:
                rec["path"] = "structure"
                await send_info(ans)
                return

        if ex.intent == "out_of_scope":
            rec["path"] = "out_of_scope"
            await send_info(scope_refusal.out_of_scope(ex))
            return

        # TWO REAL READINGS -> ANSWER THE USUAL ONE, OFFER THE OTHER.
        # disambiguate.pick (started alongside extraction) says whether this is
        # one of six known bare questions ("Sokoto approach", "minima for X",
        # "clearance at X", "lighting at X", "taxiing at X", "charges at X").
        # If so, the DEFAULT reading's rewritten question is answered through
        # this same pipeline at once, with one "Did you mean ...?" button under
        # the answer for the other reading — the pilot never waits on a choice
        # (Obiora, 2026-10-26, option B). The button is left off when the AIP
        # has nothing for the alternative at this aerodrome.
        # Placed after the live and out-of-scope refusals (never a choice) and
        # before every answering route, including charges.
        # At a heliport "Soku approach" is answered as typed (Obiora,
        # 2026-10-27): disambiguate.applies_here.
        if _amb is not None:
            _pick = await _amb
            icao = None
            if _pick:
                family, place = _pick
                if ex.icao_code in resolver.VALID_ICAO:
                    icao = ex.icao_code
                else:
                    _hits = resolver.match_name(ex.aerodrome_name or place) or set()
                    icao = next(iter(_hits)) if len(_hits) == 1 else None
                if not await asyncio.to_thread(disambiguate.applies_here, family, icao):
                    _pick = None
            if _pick:
                has_alt = await asyncio.to_thread(disambiguate.alternative_exists,
                                                  family, icao)
                rec["path"] = f"ambiguous:{family}"
                rec["skip_log"] = True        # the default answer logs this question
                await process(chat_id, disambiguate.default_question(family, place),
                              from_choice=True,
                              alt_button=(disambiguate.alt_button(family, place, icao)
                                          if has_alt else None),
                              log_as=f"[default:{family}] {text}")
                return

        # PUBLISHED CHARGES (GEN 4) — fetched BY SECTION and shown VERBATIM.
        # A fee schedule is never left to vector search and synthesis: GEN 4.1
        # puts international and domestic rates, and day and night landing
        # rates, side by side, which is the same juxtaposition hazard as
        # AD 2.12 and AD 2.17. The pilot reads the AIP's own lines, with the
        # amendment they are valid for and who to ask for current rates.
        _g4 = charges.section_for(getattr(ex, "gen4_charge", None))
        if _g4:
            rec["path"] = f"charges:{_g4}"
            g4_text = await asyncio.to_thread(get_subsection_text, "NATIONAL", _g4)
            await send_info(charges.charges_reply(
                _g4, g4_text, text,
                aerodrome_named=bool(ex.icao_code or ex.aerodrome_name)))
            return

        # 2) deterministic resolution
        ctx = await asyncio.to_thread(memory.load, chat_id)
        res = await asyncio.to_thread(resolver.resolve, ex)
        rec["icao"] = res.icao

        # --- conversation context: fill a GAP only, always surfaced -----------
        ctx_note = None
        follow_query = text
        pending = ctx.get("pending")
        if pending and _bare_aerodrome(ex, text) and res.icao:
            # A bare "Lagos" answering an earlier "which aerodrome?" — merge the
            # remembered request onto this aerodrome and re-run it.
            ex.intent = pending.get("intent") or ex.intent
            ex.procedure_type = pending.get("procedure_type")
            ex.runway = pending.get("runway")
            ex.icao_code, ex.aerodrome_name = res.icao, None
            res = await asyncio.to_thread(resolver.resolve, ex)
            rec["icao"] = res.icao
            follow_query = pending.get("raw") or text
            ctx_note = f"Continuing your earlier request — {res.label}:"
        elif (res.unresolved and ctx.get("last_icao") and _aviation_intent(ex)
              and not _names_a_place(ex)):
            # A follow-up with NO aerodrome reference ("what about the ILS?", "can
            # you list them?") — carry the last aerodrome and fold in the last
            # query. Surfaced, never silent. If the message NAMED a place we
            # couldn't resolve (e.g. "Jalingo"), we do NOT reach here — it falls
            # through to the honest "not a published aerodrome" refusal below,
            # instead of borrowing the last aerodrome.
            ex.icao_code = ctx["last_icao"]
            res = await asyncio.to_thread(resolver.resolve, ex)
            rec["icao"] = res.icao
            if not res.unresolved:
                follow_query = f"{ctx.get('last_query') or ''} {text}".strip()
                ctx_note = f"Using your last aerodrome, {res.label}:"

        # An aerodrome's OWN airspace — CTR/TMA lateral and vertical limits,
        # classification, ATS call sign, transition altitude — is published in
        # ITS AD 2.17, NOT in ENR. resolver.resolve() cannot make this call:
        # it never sees the query text, so it can't tell "Maiduguri CTR
        # lateral limits" (AD 2.17) from "airways through the Lagos TMA"
        # (genuinely ENR), and so sends every airspace_lookup national.
        #
        # Here the text and the classifier ARE available, so narrow it:
        # redirect only when the query is identifiably about one aerodrome's
        # own AD 2.x content. Everything else stays national, leaving real
        # en-route queries untouched.
        #
        # Confirmed live failures this fixes: "what is the lateral limit for
        # lagos ctr" returned ENR 3.1 at 59% and "what is the lateral limit
        # for maiduguri ctr" returned ENR 3.1 at 54% — while DNMM's and
        # DNMA's own AD 2.17 held the exact answers.
        _redirect = await asyncio.to_thread(_airspace_redirect, ex, res,
                                            follow_query or text)
        if _redirect is not None:
            res = _redirect
            rec["icao"] = res.icao
        if force_scope:
            _k, _sid = force_scope
            if _k == "AD":
                res = Resolution(icao=_sid,
                                 label=resolver.aerodrome_full_name(_sid) or _sid,
                                 part="AD", reference=_sid,
                                 scope_kind="AD", scope_id=_sid)
            else:
                res = Resolution(label=f"{_sid} (navaid)", part="ENR",
                                 reference="AIRSPACE", is_national=True,
                                 scope_kind=_k, scope_id=_sid)

        if res.ambiguous:
            rec["path"] = "ambiguous"
            # A SCOPE ambiguity gets buttons, not prose. "ABC" is both Abuja and
            # the ABC VOR/DME, and the prose form ("reply with the ICAO code,
            # or say 'navaid'") had nowhere to land: the slot-fill path only
            # understands aerodrome names, so a typed "navaid" would not have
            # routed anywhere. The button carries the scope explicitly.
            if getattr(res, "ambiguous_kind", "aerodrome") == "scope":
                _ident = re.sub(r"\s+", "", (ex.aerodrome_name or "")).upper()
                _icao = next((ic for ic, idt in resolver.VOR_IDENTS.items()
                              if idt == _ident), None)
                if _icao:
                    rec["path"] = "ambiguous:scope"
                    await asyncio.to_thread(
                        memory.save_scope_pending, chat_id, _icao, _ident,
                        text, rec["qid"])
                    await send_message(
                        chat_id,
                        f"'{_ident}' is both an aerodrome and a published "
                        f"navaid. Which did you mean?",
                        reply_markup=clarify_scope_kb(_icao, _ident, rec["qid"]))
                    return
            await send_message(chat_id, ambiguous(res))
            return
        if res.unresolved:
            rec["path"] = "unresolved"
            # Remember this request so the next bare aerodrome name completes it.
            await asyncio.to_thread(memory.save_pending, chat_id, ex, text,
                                    ctx.get("last_icao"))
            await send_message(chat_id, unresolved(res))
            return

        # Resolved: remember the aerodrome + query for follow-ups, clear pending.
        if res.icao:
            await asyncio.to_thread(memory.save_last, chat_id, res.icao, text)
        # Surface any carried context BEFORE the answer (guardrail: never silent).
        if ctx_note:
            await send_message(chat_id, ctx_note)

        # A BARE AERODROME NAME gets the summary card — the model's decision
        # (schemas.aerodrome_overview), honoured only when it asked for nothing
        # else, and never when this message continued an earlier request.
        if (getattr(ex, "aerodrome_overview", False) and res.icao and ctx_note is None
                and (res.scope_kind or "AD") == "AD"
                and not (ex.procedure_type or ex.runway
                         or getattr(ex, "ad2_subsection", None)
                         or getattr(ex, "approach_request", None)
                         or getattr(ex, "sid_request", False))):
            rec["path"] = "overview"
            await _send_overview(chat_id, res, rec["qid"])
            return

        # ICAO <-> name mapping: answer deterministically from the static table.
        # No retrieval, no LLM — the safest possible path.
        # ONLY for aerodromes. This branch answers from resolver.AERODROMES,
        # which contains the 40 published aerodromes and nothing else, so for
        # an ENR entity aerodrome_full_name() returns None and the reply reads
        #     "None — DND45 (prohibited/restricted/danger area), Nigeria."
        # Confirmed live. Worse than the cosmetic defect: it RETURNS, so the
        # facts lookup below never runs and a fully indexed danger area — its
        # vertical limits, its coordinates, its activity — is never consulted.
        #
        # A non-aerodrome scope falls through to the normal retrieval path,
        # which is where its facts actually live.
        if ex.intent == "icao_lookup" and (res.scope_kind or "AD") == "AD" and res.icao:
            rec["path"] = "mapping"
            full = resolver.aerodrome_full_name(res.icao) or res.label
            await send_info(
                f"{res.icao} — {full}, Nigeria.\nSource: Nigeria AIP · {config.AIRAC_CYCLE}")
            return

        # CHART REQUESTS short-circuit here. The deliverable is the plate image;
        # the text layer of chart pages is flattened diagram annotations (scale
        # bars, bearing ticks, loose numbers) and must NEVER be shown to a pilot.
        if ex.intent == "chart_retrieval":
            ql = text.lower()
            chart_icao = res.icao
            # A SID question (the model's sid_request) goes to the SID choice:
            # runway, then SID, as an approach goes to the approach choice.
            if (getattr(ex, "sid_request", False) and res.icao
                    and (res.scope_kind or "AD") == "AD"):
                rec["path"] = "sid"
                if await _run_sid_decision(chat_id, res, res.icao, ex.runway or "",
                                           getattr(ex, "sid_name", None) or "",
                                           send_info):
                    return
            # AD 2.22 content that is NOT an instrument approach (take-off
            # minima, PBN coding tables, VFR rules within the TMA) has no
            # corresponding AD 2.24 plate — showing an arbitrary approach chart
            # beside such an answer would imply a connection that doesn't
            # exist. Clearing chart_icao makes the existing `if chart_icao:`
            # check below skip the chart flow entirely, so the query falls
            # through to the text path, which routes to AD 2.22
            # deterministically and answers from that section alone.
            if (subsection_router.detect_subsection(text) == "AD 2.22"
                    and not subsection_router.is_approach_query(text)):
                log.info("AD 2.22 non-approach query — text only, no chart")
                chart_icao = None
            elif chart_icao is None and (res.reference == "DNKK" or "fir" in ql
                                         or "en-route" in ql or "enroute" in ql):
                chart_icao = "DNKK"
            charts = []
            if chart_icao:
                # Instrument-approach requests go through clarification: ask
                # ILS/VOR/RNAV and runway when ambiguous, send directly when not.
                if chart_icao not in ("GEN", "DNKK") and _is_approach(ex, text):
                    rec["path"] = "chart"
                    await _run_chart_decision(chat_id, res, chart_icao,
                                              ex.procedure_type or "", ex.runway or "",
                                              send_info)
                    return
                if chart_icao in ("GEN", "DNKK"):
                    charts = await asyncio.to_thread(get_charts, chart_icao, "", "")
                else:
                    term = f"{ex.procedure_type or ''} {text}"
                    charts = await asyncio.to_thread(
                        get_charts_smart, chart_icao, term, ex.runway or "")
                charts = charts[: config.MAX_CHARTS]
            rec["charts"] = len(charts)
            if charts:
                rec["path"] = "chart"
                await send_info(chart_intro(res, ex))
                await send_charts(chat_id, charts, requested_runway=ex.runway)
            else:
                rec["path"] = "chart_not_found"
                await send_info(chart_not_found(res, ex))
            return

        # 2.5) Aerodrome geographic/admin data (AD 2.2): reference temperature,
        #      magnetic variation, ARP, transition altitude/level, geoid,
        #      aerodrome elevation. The general vector search under-retrieves the
        #      SECONDARY paired values (Kano ref temp 33.1C was falsely abstained
        #      even though it's published), so fetch AD 2.2 BY SECTION and
        #      synthesize over that guaranteed-correct chunk. These fields are
        #      unit-disambiguated (m vs C vs FL), so synthesis is safe once the
        #      right chunk is in hand.
        if _AERODROME_DATA_RE.search(follow_query) and res.icao:
            ad_text = await asyncio.to_thread(get_aerodrome_data, res.icao)
            # A THIN AD 2.2 chunk must not answer. Measured on the real bot:
            # 8 of 13 AD 2.2 stress cases returned the aerodrome ELEVATION
            # regardless of what was asked -- magnetic variation, reference
            # temperature, ARP longitude, ARP site description all got
            # "DNKN elevation 476.0m/1562.0ft". The routing was right; the
            # stored chunk simply holds one field.
            #
            # aip_knowledge_base (what this reads) is far thinner than
            # aip_structured/aip_facts (what the 23 validated extractors
            # built) -- DNAA's whole AD 2.8 chunk is the string
            # "DNAA AD 2.8 aprons/taxiways". Answering from a one-line chunk
            # produces a confident reply about the wrong field, which is the
            # misattribution class this project exists to prevent.
            #
            # The test is structural, not a field list: a genuine AD 2.2
            # record carries several fields and runs to hundreds of
            # characters. Falling through is SAFE and already the established
            # behaviour when the chunk is missing entirely -- the normal
            # search path (and the facts path, when enabled) then gets a turn.
            if ad_text and len(ad_text.strip()) < 120 and ad_text.count("\n") < 2:
                log.info("AD 2.2 chunk too thin for %s (%d chars) — falling through",
                         res.icao, len(ad_text.strip()))
                ad_text = ""
            if ad_text:
                ad_res = AIPResult(content=ad_text, similarity=1.0,
                                   aip_section="AD 2.2", reference_tag=res.icao)
                ad_out = SearchOutcome(results=[ad_res], max_similarity=1.0,
                                       abstained=False, used_reference=res.icao)
                status, ga = await asyncio.to_thread(
                    synthesize.synthesize_decision, follow_query, [ad_res], ex)
                if status == "grounded":
                    rec["path"] = "aerodrome_data"
                    await send_info(grounded_reply(ga, ad_out, res))
                    return
                if status == "not_in_aip":
                    rec["path"] = "not_in_aip"
                    await send_info(not_in_aip(res))
                    return
                # any other status -> show the AD 2.2 chunk focused (safe, sourced)
                rec["path"] = "aerodrome_data"
                await send_info(answer(ad_out, res, ex.runway, follow_query))
                return
            # no AD 2.2 chunk stored -> fall through to the normal search path

        # 3) embed an enriched query: expands the aerodrome name (PH -> Port
        #    Harcourt) and, for airspace, prepends AIP airspace terminology.
        #    On a follow-up, follow_query folds in the prior topic.
        search_text = resolver.build_search_text(ex, res, follow_query)
        embedding = await asyncio.to_thread(get_embedding, search_text)
        if embedding is None:
            rec["path"] = "error"
            await send_message(chat_id, error())
            return

        # 3b) NON-AERODROME SCOPES ARE SERVED FROM aip_facts, FIRST AND ONLY.
        #
        # search_aip() below queries aip_knowledge_base, which holds AD 2.x and
        # ENR PROSE — it has no per-entity content for a danger area, waypoint
        # or airway. So for an ENR scope it finds nothing, abstains, and sends
        # "I couldn't find that" from inside a branch whose `else` contains the
        # facts lookup. The entity is fully indexed and never consulted.
        #
        # Order matters, and it is the reverse of the aerodrome case: for an
        # aerodrome the knowledge base is a reasonable first stop and facts are
        # a refinement; for an ENR entity the facts index is the ONLY source.
        if (res.scope_kind or "AD") != "AD" and res.scope_id:
            _facts = await asyncio.to_thread(
                search_facts_scoped, embedding, res.scope_kind, res.scope_id,
                "", config.FACTS_MAX)
            if _facts:
                rec["path"] = f"facts:{res.scope_kind}"
                await send_info(facts_reply(res, _facts, follow_query))
                return
            # Indexed entity with no matching fact: abstain rather than fall
            # through to a knowledge-base search that cannot know about it.
            rec["path"] = "not_found"
            await send_info(not_found())
            return

        # 4) search with fallback + max-similarity gate
        outcome = await asyncio.to_thread(
            search_aip, embedding, res, ex.procedure_type or "", ex.runway or ""
        )
        rec["similarity"] = outcome.max_similarity

        if outcome.abstained and outcome.reason == "low_confidence":
            rec["path"] = "low_confidence"
            await send_info(low_confidence(outcome))
            # still offer charts below if we have an ICAO
        elif outcome.abstained:
            rec["path"] = "not_found"
            await send_info(not_found())
        else:
            status, ga = await asyncio.to_thread(
                synthesize.synthesize_decision, follow_query, outcome.results, ex)

            # FIELD-LEVEL FACTS. Tried once the safety guards and structured
            # handlers have declined — i.e. exactly where the old code fell
            # through to synthesis over whole retrieved chunks, which is the
            # population that produced "lateral limit for lagos ctr" -> ENR
            # 3.1 @ 59%.
            #
            # A fact is a single field embedded as a sentence close to how a
            # pilot asks for it, so retrieval lands on the answer rather than
            # on a 79,871-character subsection average. Shown VERBATIM, never
            # synthesized, so this path cannot hallucinate.
            #
            # Below FACTS_MIN_SIM the retriever has no real opinion, so we
            # leave the existing behaviour untouched rather than answer from
            # a weak match.
            # Statuses whose fallback is a whole-SECTION dump from
            # aip_knowledge_base, and the subsection aip_facts holds the same
            # data under. Measured over 286 stress cases:
            #
            #     path                 store               pass
            #     facts                aip_facts            74%
            #     subsection_verbatim  aip_knowledge_base   54%
            #     comms                aip_knowledge_base   50%
            #     navaid               aip_knowledge_base  (refusals)
            #
            # 126 of 286 rows sat on paths that could never reach aip_facts,
            # because the gate below listed only four statuses. aip_facts holds
            # all 23 subsections for all 36 aerodromes, so those paths were
            # falling back to the thin store while the rich one went unqueried.
            #
            # This is SAFER than the section dump it replaces, not merely more
            # accurate. The comms and navaid guards exist because AD 2.18 and
            # AD 2.19 stack several services/navaids into one block with
            # misaligned fields, so a synthesised "the tower frequency" could
            # return another service's value. In aip_facts each service and each
            # navaid is already its own row (entity = "TWR"/"ATIS"/"ACC",
            # "DVOR/DME"/"GP"/"DME"), and facts_reply() groups by entity — so
            # two entities' values cannot merge onto one line. The guards' own
            # reasoning was written before aip_facts existed.
            #
            # rwy_data (81%) and declared_distance (80%) are deliberately NOT
            # here: they already read per-entity records from aip_structured
            # and outperform the facts path. Do not "fix" what is winning.
            _FACTS_SUBSECTION = {"comms": "2.18", "navaid": "2.19"}
            _FACTS_STATUSES = ("subsection", "subsection_verbatim", "comms",
                               "navaid", "grounded", "not_in_aip", "fallback")
            # A scope may be an aerodrome OR an ENR entity (danger area,
            # waypoint, ATS route). res.scope_id covers both: it is the ICAO
            # for aerodromes and the entity id otherwise, so this condition
            # widens without changing aerodrome behaviour.
            if config.FACTS_ENABLED and (res.scope_id or res.icao) \
                    and status in _FACTS_STATUSES:
                if status in ("subsection", "subsection_verbatim"):
                    _sub = ga or ""
                else:
                    _sub = _FACTS_SUBSECTION.get(status, "")
                _sub_num = (_sub or "").replace("AD ", "").strip()
                if (res.scope_kind or "AD") != "AD":
                    # Non-aerodrome scope: retrieval is confined to that ONE
                    # entity at the database boundary, exactly as it is for an
                    # aerodrome. DND45's facts can never be returned for DND46.
                    _facts = await asyncio.to_thread(
                        search_facts_scoped, embedding, res.scope_kind,
                        res.scope_id, _sub_num, config.FACTS_MAX)
                else:
                    _facts = await asyncio.to_thread(
                        search_facts, embedding, res.icao, _sub_num,
                        config.FACTS_MAX)
                _top = _facts[0]["similarity"] if _facts else 0.0
                if _facts and _top >= config.FACTS_MIN_SIM:
                    rec["path"] = f"facts:{_sub_num or 'any'}"
                    rec["similarity"] = _top
                    log.info("facts path: %s top=%.2f n=%d", res.icao, _top, len(_facts))
                    await send_info(facts_reply(res, _facts, follow_query))
                    return
            if status == "approach_procedure":
                # Defense-in-depth: synthesis refused to write approach procedures.
                # Route to the safe approach-chart flow (clarification + plate);
                # NEVER dump the AD 2.22 chunk verbatim.
                if res.icao:
                    rec["path"] = "chart"
                    await _run_chart_decision(chat_id, res, res.icao,
                                              ex.procedure_type or "", ex.runway or "",
                                              send_info)
                else:
                    rec["path"] = "not_found"
                    await send_info(not_found())
                return
            if status == "declared_distance":
                # Answer from STRUCTURED per-runway data (validated at ingestion,
                # never misattributed). If this aerodrome wasn't parsed cleanly,
                # there's no structured row -> refuse to source (AD 2.13 verbatim).
                rec["path"] = "declared_distance"
                recs = (await asyncio.to_thread(get_declared_distances, res.icao)
                        if res.icao else [])
                if recs:
                    await send_info(declared_distance_reply(res, recs, ex.runway,
                                                             follow_query))
                else:
                    note = ("I don't have structured declared-distance data for this "
                            "aerodrome, so I won't single out a value — read the exact "
                            "figure from the AD 2.13 source below:")
                    await send_info(f"{note}\n\n"
                                    f"{answer(outcome, res, ex.runway, follow_query)}")
                return
            if status == "navaid":
                # Several navaids are published together for one aerodrome; the
                # block can't be split into per-navaid values safely, so we never
                # single one out. Fetch AD 2.19 BY NAME (the vector search can rank
                # the wrong section — it surfaced AD 2.12 for this query) and show
                # it focused, so the pilot reads the right navaid's figure.
                rec["path"] = "navaid"
                note = ("This aerodrome publishes several navaids in one AIP table, "
                        "so I won't single out one value — read the exact figure for "
                        "the navaid you need from the AD 2.19 source below:")
                nav_text = ""
                if res.icao:
                    nav_text = await asyncio.to_thread(get_section_text, res.icao,
                                                       "AD 2.19")
                if nav_text:
                    body = navaid_reply(res, nav_text, follow_query)
                else:
                    body = answer(outcome, res, ex.runway, follow_query)
                await send_info(f"{note}\n\n{body}")
                return
            if status == "comms":
                # Tower/Ground/Approach/ATIS frequencies share one AD 2.18 block;
                # fetch it BY NAME and show focused so the pilot reads the exact
                # frequency for the service they need — never a synthesized value
                # that could be another service's frequency.
                rec["path"] = "comms"
                note = ("This aerodrome lists several ATS frequencies together, so I "
                        "won't single one out — read the exact frequency for the "
                        "service you need from the AD 2.18 source below:")
                ctext = ""
                if res.icao:
                    ctext = await asyncio.to_thread(get_section_text, res.icao,
                                                    "AD 2.18")
                if ctext:
                    body = comms_reply(res, ctext, follow_query)
                else:
                    body = answer(outcome, res, ex.runway, follow_query)
                await send_info(f"{note}\n\n{body}")
                return
            if status == "rwy_char":
                # Asymmetric AD 2.12 field (bearing / threshold elevation /
                # threshold coordinates) differs per runway end; fetch AD 2.12 BY
                # NAME and show focused so the pilot reads the value for the exact
                # end. Symmetric fields (length/width/PCN) never reach here.
                rec["path"] = "rwy_char"
                note = ("This value differs per runway end, so I won't single one "
                        "out — read the exact figure for the runway end you need "
                        "from the AD 2.12 source below:")
                # Structured per-end record first (gold run 2026-11-25: the
                # AD 2.12 chunk holds no threshold value at all).
                rrecs = (await asyncio.to_thread(get_runway_physical_data, res.icao)
                         if res.icao else [])
                if rrecs:
                    await send_info(f"{note}\n\n"
                                    f"{runway_end_reply(res, rrecs, ex.runway, follow_query)}")
                    return
                rtext = ""
                if res.icao:
                    rtext = await asyncio.to_thread(get_section_text, res.icao,
                                                    "AD 2.12")
                if rtext:
                    body = rwy_char_reply(res, rtext, follow_query)
                else:
                    body = answer(outcome, res, ex.runway, follow_query)
                await send_info(f"{note}\n\n{body}")
                return
            if status == "rwy_data":
                # General runway-overview query ("Abuja runway", "runways at
                # Kano") with no specific field asked. AD 2.12 is now fully
                # structured (Layer 2 / aip_structured) and validated at
                # ingestion, so this is an exact key lookup, not a similarity
                # search — closing the exact gap the project's original
                # misattribution incident was about: a vague runway query
                # previously fell through to low-confidence vector search and
                # could surface an unrelated table entirely.
                rec["path"] = "rwy_data"
                recs = (await asyncio.to_thread(get_runway_physical_data, res.icao)
                        if res.icao else [])
                if recs:
                    await send_info(runway_data_reply(res, recs, ex.runway,
                                                       follow_query))
                else:
                    # No aip_structured row for this aerodrome (rare — validated
                    # 36/36 in production) — fall back to the existing verbatim
                    # path rather than claim there's no runway data at all.
                    note = ("I don't have structured runway data for this "
                            "aerodrome, so I won't single out a value — read the "
                            "exact figures from the AD 2.12 source below:")
                    await send_info(f"{note}\n\n"
                                    f"{answer(outcome, res, ex.runway, follow_query)}")
                return
            if status == "lighting_data":
                # AD 2.14 approach/runway lighting — the SAME misattribution
                # shape as AD 2.12, but with no safe symmetric subset: every
                # field (PAPI angle, lighting type) can genuinely differ
                # between a runway's two ends, so ANY lighting query routes
                # here, not just the asymmetric ones. Structured and
                # validated at ingestion via the same per-end tracking as
                # AD 2.12, so this is an exact key lookup.
                rec["path"] = "lighting_data"
                recs = (await asyncio.to_thread(get_lighting_data, res.icao)
                        if res.icao else [])
                if recs:
                    await send_info(lighting_data_reply(res, recs, ex.runway,
                                                         follow_query))
                else:
                    note = ("I don't have structured lighting data for this "
                            "aerodrome, so I won't single out a value — read the "
                            "exact figures from the AD 2.14 source below:")
                    await send_info(f"{note}\n\n"
                                    f"{answer(outcome, res, ex.runway, follow_query)}")
                return
            if status == "subsection_verbatim":
                # Minima (AD 2.22). Exact section, shown VERBATIM — synthesis
                # is never invoked, so the never-synthesize-a-decision-height
                # rule holds while retrieval stops being a similarity guess.
                section = ga
                rec["path"] = f"subsection_verbatim:{section}"
                sect_text = (await asyncio.to_thread(get_subsection_text, res.icao, section)
                             if res.icao else "")
                if sect_text:
                    await send_info(subsection_reply(res, section, sect_text, follow_query))
                else:
                    await send_info(answer(outcome, res, ex.runway, follow_query))
                return
            if status == "subsection":
                # Deterministic AD 2.x routing. `ga` is the exact subsection
                # id ("AD 2.17"). Because vectorise_aip_v3.py stores one chunk
                # per (aerodrome, subsection), get_subsection_text fetches
                # THAT subsection and nothing else — matching on EQUALITY, not
                # a LIKE prefix (which would make "AD 2.2" also pull AD 2.20
                # through AD 2.24, including the huge AD 2.22) — and with no
                # similarity ranking involved, so the
                # "top chunk was actually a different subsection" failure mode
                # cannot occur. Synthesis then runs over that single section,
                # which makes cross-subsection misattribution unrepresentable
                # rather than merely detectable.
                section = ga
                rec["path"] = f"subsection:{section}"
                if section == "AD 2.2" and res.icao:
                    # The stored AD 2.2 chunk is one line (the elevation), so
                    # it answered reference temperature and ARP questions with
                    # the elevation (gold run 2026-11-25). Answer from the
                    # structured record, every field labelled.
                    ad22 = await asyncio.to_thread(get_structured, res.icao, "2.2")
                    if ad22:
                        rec["path"] = "subsection:AD 2.2:structured"
                        await send_info(ad22_reply(res, ad22[0]))
                        return
                if section == "AD 2.6" and res.icao:
                    # Same for AD 2.6: the stored chunk is the sentence "DNAA RFF
                    # category 9"; the record keeps "CAT 9" as printed.
                    ad26 = await asyncio.to_thread(get_structured, res.icao, "2.6")
                    if ad26:
                        rec["path"] = "subsection:AD 2.6:structured"
                        await send_info(ad26_reply(res, ad26[0]))
                        return
                sect_text = (await asyncio.to_thread(get_subsection_text, res.icao, section)
                             if res.icao else "")
                if sect_text and section == "AD 2.22":
                    # Deterministic, zero-LLM slice for AD 2.22's known
                    # non-approach headings (General, Runway in use, Radar
                    # Procedures, VFR minima, VFR flights) — tried FIRST,
                    # since nothing is generated: the answer is either the
                    # source's own verbatim words or nothing at all. This is
                    # the safest possible path for these specific headings,
                    # strictly stronger than an LLM-synthesis round-trip.
                    # Falls through to synthesis below only when no known
                    # heading matches this query.
                    info_body = clarify.info_block_answer(sect_text, follow_query)
                    if info_body:
                        rec["path"] = "subsection:AD 2.22:info_block"
                        await send_info(info_block_reply(res, section, info_body))
                        return
                if sect_text:
                    ok, sans, single = await asyncio.to_thread(
                        synthesize.synthesize_over_section,
                        follow_query, sect_text, section, res.icao)
                    if ok:
                        sect_outcome = SearchOutcome(
                            results=[single], max_similarity=1.0,
                            used_part="AD", used_reference=res.icao, abstained=False)
                        await send_info(grounded_reply(sans, sect_outcome, res))
                    else:
                        # Verification declined — still the RIGHT subsection,
                        # shown verbatim. A safe fallback, not a degraded one.
                        await send_info(subsection_reply(res, section, sect_text,
                                                          follow_query))
                else:
                    # No stored chunk for this subsection (should not happen for
                    # the 36 standard aerodromes) — fall back to the existing
                    # vector-search answer rather than claim nothing exists.
                    await send_info(answer(outcome, res, ex.runway, follow_query))
                return
            rec["path"] = status if status in ("grounded", "not_in_aip") else "answer"
            if status == "grounded":
                await send_info(grounded_reply(ga, outcome, res))
            elif status == "not_in_aip":
                await send_info(not_in_aip(res))
            else:
                await send_info(answer(outcome, res, ex.runway, follow_query))

        # 5) charts (no AI). Aerodrome charts by ICAO; plus two special targets:
        #    Kano FIR en-route plates (icao_code DNKK) and the SAR units chart
        #    (icao_code GEN, GEN 3.6), which aren't tied to a normal aerodrome.
        chart_icao = res.icao
        ql = text.lower()
        is_sar = re.search(r"\bsar\b|search and rescue|\brescue\b", ql) is not None
        if chart_icao is None:
            if is_sar:
                chart_icao = "GEN"        # SAR Units chart
            elif res.reference == "DNKK" or "fir" in ql or "en-route" in ql or "enroute" in ql:
                chart_icao = "DNKK"       # Kano FIR en-route charts

        # SAR chart accompanies SAR text even without an explicit chart request.
        want_charts = _wants_chart(ex) or is_sar
        if chart_icao and want_charts:
            if chart_icao in ("GEN", "DNKK"):   # whole-section charts, unfiltered
                charts = await asyncio.to_thread(get_charts, chart_icao, "", "")
            else:
                term = f"{ex.procedure_type or ''} {text}"
                charts = await asyncio.to_thread(
                    get_charts_smart, chart_icao, term, ex.runway or "")
            await send_charts(chat_id, charts[: config.MAX_CHARTS], requested_runway=ex.runway)

    except DataUnavailable:
        # The database could not be read even after retrying (2026-11-09).
        # Say so plainly — never let a failed read look like "not published".
        rec["path"] = "data_unavailable"
        log.exception("process: AIP database unavailable")
        try:
            await send_message(chat_id, data_unavailable())
        except Exception:  # noqa: BLE001
            log.exception("failed to send data-unavailable message")
    except Exception:  # noqa: BLE001
        rec["path"] = "error"
        log.exception("process failed")
        try:
            await send_message(chat_id, error())
        except Exception:  # noqa: BLE001
            log.exception("failed to send error message")
    finally:
        try:
            if not rec.get("skip_log"):
                await asyncio.to_thread(
                    observability.log_query, chat_id=chat_id, query=log_as or text,
                    intent=rec["intent"], icao=rec["icao"], path=rec["path"],
                    similarity=rec["similarity"], charts=rec["charts"], qid=rec["qid"],
                    reply="\n\n".join(_replies))
        except Exception:  # noqa: BLE001
            log.exception("query log failed")
        finally:
            _REPLIES.reset(_replies_token)
