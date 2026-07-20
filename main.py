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
import logging
import re
import uuid
from types import SimpleNamespace

from fastapi import BackgroundTasks, FastAPI, Form, Header, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

import cache
import config
import resolver
from agent import extract_query_parameters, get_embedding
from database import (get_aerodrome_data, get_charts, get_charts_smart,
                      get_declared_distances, get_lighting_data,
                      get_runway_physical_data, get_section_text,
                      get_subsection_text, search_aip)
from models import AIPResult, SearchOutcome
import synthesize
import subsection_router
import facts
import memory
import clarify
import observability
import procedures
import toc
from responder import (ambiguous, answer, chart_intro, chart_not_found,
                       comms_reply, declared_distance_reply, error, grounded_reply,
                       info_block_reply, lighting_data_reply, low_confidence,
                       navaid_reply, not_found, not_in_aip, runway_data_reply,
                       rwy_char_reply, subsection_reply, unresolved)
from telegram import (answer_callback, clarify_runway_kb, clarify_type_kb,
                      feedback_kb, send_charts, send_message, verify_secret)

logging.basicConfig(level=logging.INFO)
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


async def _health_monitor() -> None:
    """Re-run the deep check on an interval; alerting.report() fires on
    healthy<->degraded transitions (throttled, with recovery notices)."""
    while True:
        await asyncio.sleep(config.DEEP_CHECK_INTERVAL_SEC)
        try:
            await asyncio.to_thread(observability.healthcheck)
        except Exception:  # noqa: BLE001
            log.exception("periodic healthcheck errored")


@app.get("/health")
def health() -> dict:
    """Cheap liveness — no external calls, safe for frequent Render pings."""
    return {"status": "ok", "airac": config.AIRAC_CYCLE}


@app.get("/health/deep")
def health_deep(token: str = ""):
    """Deep check (OpenAI + Supabase). Token-gated because it makes API calls."""
    if not config.DASHBOARD_TOKEN or token != config.DASHBOARD_TOKEN:
        return Response("not found", status_code=404)
    ok, fails = observability.healthcheck()
    return {"status": "ok" if ok else "degraded", "failed": fails,
            "airac": config.AIRAC_CYCLE}


@app.get("/dashboard")
def dashboard(token: str = "", days: int = 30):
    """Read-only observability dashboard. Token-gated; disabled unless
    DASHBOARD_TOKEN is set. Renders live from the query log — no third-party
    egress, mutations only via the triage CLI."""
    if not config.DASHBOARD_TOKEN or token != config.DASHBOARD_TOKEN:
        return Response("not found", status_code=404)
    days = max(1, min(int(days), 90))
    rows = observability.fetch_log(days=days)
    return HTMLResponse(observability.render_dashboard(rows, days, config.DASHBOARD_TOKEN))


@app.post("/dashboard/prune")
def dashboard_prune(token: str = Form(""), before_days: int = Form(90)):
    """Age-based prune from the dashboard. Token-gated; floored at 7 days in
    prune_logs so recent data can't be wiped. A full wipe is CLI-only."""
    if not config.DASHBOARD_TOKEN or token != config.DASHBOARD_TOKEN:
        return Response("not found", status_code=404)
    observability.prune_logs(before_days)
    # back to the dashboard (month view)
    return RedirectResponse(f"/dashboard?token={config.DASHBOARD_TOKEN}&days=30",
                            status_code=303)


@app.get("/dashboard/export.csv")
def dashboard_export(token: str = "", days: int = 30):
    """Download the query log for the window as CSV (offline analysis / audit)."""
    if not config.DASHBOARD_TOKEN or token != config.DASHBOARD_TOKEN:
        return Response("not found", status_code=404)
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
    await send_info(chart_intro(res, shim))
    await _send_approach_procedures(chat_id, shim, res, send_info)
    await send_charts(chat_id, d.charts, requested_runway=runway or None)
    return "send"


_AVIATION_INTENTS = {"chart_retrieval", "procedure_lookup", "frequency_retrieval",
                     "runway_data", "aerodrome_fact", "airspace_lookup"}


def _bare_aerodrome(ex) -> bool:
    """True when the message is essentially just naming a place ('Lagos', 'DNMM')
    — the safe signal that it answers an earlier 'which aerodrome?'. Kept strict
    (icao_lookup, no field) so 'elevation of Abuja' is treated as a NEW query, not
    a slot-fill answer."""
    return ex.intent == "icao_lookup" and not ex.procedure_type and not ex.runway


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
_AERODROME_DATA_RE = re.compile(
    r"\b(reference temp\w*|ref\.?\s?temp\w*|magnetic variation|mag\.?\s?var\w*|"
    r"annual change|aerodrome reference point|\barp\b|geoid|"
    r"transition (altitude|level)|aerodrome elevation|elevation of the aerodrome)\b",
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
    ])


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


async def process(chat_id: int, text: str) -> None:
    """All heavy lifting; runs after the 200 ack. Never raises to the caller."""
    rec = {"intent": None, "icao": None, "path": "unknown",
           "similarity": None, "charts": 0, "qid": uuid.uuid4().hex[:12]}
    kb = feedback_kb(rec["qid"])   # 👍/👎 buttons

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

        # 1) extract (sync SDK -> threadpool)
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
            await send_info(config.OUT_OF_SCOPE)
            return

        # 2) deterministic resolution
        ctx = await asyncio.to_thread(memory.load, chat_id)
        res = await asyncio.to_thread(resolver.resolve, ex)
        rec["icao"] = res.icao

        # --- conversation context: fill a GAP only, always surfaced -----------
        ctx_note = None
        follow_query = text
        pending = ctx.get("pending")
        if pending and _bare_aerodrome(ex) and res.icao:
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

        if res.ambiguous:
            rec["path"] = "ambiguous"
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

        # ICAO <-> name mapping: answer deterministically from the static table.
        # No retrieval, no LLM — the safest possible path.
        if ex.intent == "icao_lookup":
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

    except Exception:  # noqa: BLE001
        rec["path"] = "error"
        log.exception("process failed")
        try:
            await send_message(chat_id, error())
        except Exception:  # noqa: BLE001
            log.exception("failed to send error message")
    finally:
        try:
            await asyncio.to_thread(
                observability.log_query, chat_id=chat_id, query=text,
                intent=rec["intent"], icao=rec["icao"], path=rec["path"],
                similarity=rec["similarity"], charts=rec["charts"], qid=rec["qid"])
        except Exception:  # noqa: BLE001
            log.exception("query log failed")
