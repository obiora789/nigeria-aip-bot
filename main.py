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

from fastapi import BackgroundTasks, FastAPI, Header, Request, Response
from fastapi.responses import HTMLResponse

import cache
import config
import resolver
from agent import extract_query_parameters, get_embedding
from database import get_charts, get_charts_smart, get_section_text, search_aip
import synthesize
import facts
import memory
import clarify
import observability
import procedures
import toc
from responder import (ambiguous, answer, chart_intro, chart_not_found, error,
                       grounded_reply, low_confidence, not_found, not_in_aip,
                       unresolved)
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
    return HTMLResponse(observability.render_dashboard(rows, days))


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
    return "approach" in t or _APPROACH_RE.search(t) is not None


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
            if chart_icao is None and (res.reference == "DNKK" or "fir" in ql
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
                synthesize.synthesize_decision, follow_query, outcome.results)
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
