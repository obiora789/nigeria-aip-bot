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
from collections import deque

from fastapi import BackgroundTasks, FastAPI, Header, Request, Response
from fastapi.responses import HTMLResponse

import config
import resolver
from agent import extract_query_parameters, get_embedding
from database import get_charts, get_charts_smart, search_aip
import synthesize
import facts
import observability
import toc
from responder import (ambiguous, answer, chart_intro, chart_not_found, error,
                       grounded_reply, low_confidence, not_found, not_in_aip,
                       unresolved)
from telegram import send_charts, send_message, verify_secret

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vannie.main")

app = FastAPI(title="Vannie — Nigeria AIP Reference Assistant")

# In-memory dedup + throttle. NOTE: single-instance only; use Redis if you scale
# horizontally so these are shared across workers.
_seen_updates: deque = deque(maxlen=config.DEDUP_CACHE_SIZE)
_seen_set: set = set()
_last_seen: dict = {}  # chat_id -> monotonic timestamp


@app.on_event("startup")
def _warm() -> None:
    try:
        resolver.load_index()
    except Exception:  # noqa: BLE001
        log.exception("index warmup failed; will lazy-load on first request")
    try:
        observability.startup_healthcheck()   # loud PASS/FAIL for OpenAI + Supabase
    except Exception:  # noqa: BLE001
        log.exception("startup healthcheck errored")


@app.get("/health")
def health() -> dict:
    checks = observability.startup_healthcheck()
    return {"status": "ok" if checks else "degraded", "airac": config.AIRAC_CYCLE}


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


def _dedup(update_id) -> bool:
    """True if this update_id was already seen."""
    if update_id is None:
        return False
    if update_id in _seen_set:
        return True
    _seen_set.add(update_id)
    _seen_updates.append(update_id)
    if len(_seen_set) > len(_seen_updates):  # trim evicted ids
        _seen_set.intersection_update(_seen_updates)
    return False


def _throttled(chat_id: int) -> bool:
    now = asyncio.get_event_loop().time()
    last = _last_seen.get(chat_id, 0.0)
    if now - last < config.PER_CHAT_COOLDOWN_SECONDS:
        return True
    _last_seen[chat_id] = now
    return False


@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks,
                  x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    if not verify_secret(x_telegram_bot_api_secret_token):
        return Response(status_code=403)

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return {"status": "ignored"}

    if _dedup(payload.get("update_id")):
        return {"status": "duplicate"}

    msg = payload.get("message") or payload.get("edited_message") or {}
    text = msg.get("text")
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not text or chat_id is None:
        return {"status": "ignored"}

    if _throttled(chat_id):
        return {"status": "throttled"}

    background.add_task(process, chat_id, text)
    return {"status": "accepted"}


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


async def _send_approach_procedures(chat_id, text, ex, res):
    """Retrieve and show the AD 2.22 approach procedure TEXT (holding, letdown,
    missed approach) for the requested approach. Shown verbatim — these are
    safety-critical procedures, so the pilot reads the AIP's exact wording, not a
    synthesized paraphrase."""
    q = (f"{res.label} instrument approach procedure {ex.procedure_type or ''} "
         f"runway {ex.runway or ''} holding procedure letdown procedure missed "
         f"approach procedure initial approach altitude AD 2.22")
    emb = await asyncio.to_thread(get_embedding, q)
    if emb is None:
        return
    outcome = await asyncio.to_thread(search_aip, emb, res, "", "")
    if outcome.abstained or not outcome.results:
        return
    await send_message(chat_id, answer(outcome, res, ex.runway))


async def process(chat_id: int, text: str) -> None:
    """All heavy lifting; runs after the 200 ack. Never raises to the caller."""
    rec = {"intent": None, "icao": None, "path": "unknown",
           "similarity": None, "charts": 0}
    try:
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
                await send_message(chat_id, ans)
                return

        # Structure/meta questions ("which part of the AIP covers X") are about
        # the document's organisation — answer from the ToC, never retrieval.
        if toc.is_structure_question(text):
            ans = toc.answer(text)
            if ans:
                rec["path"] = "structure"
                await send_message(chat_id, ans)
                return

        if ex.intent == "out_of_scope":
            rec["path"] = "out_of_scope"
            await send_message(chat_id, config.OUT_OF_SCOPE)
            return

        # 2) deterministic resolution
        res = await asyncio.to_thread(resolver.resolve, ex)
        rec["icao"] = res.icao
        if res.ambiguous:
            rec["path"] = "ambiguous"
            await send_message(chat_id, ambiguous(res))
            return
        if res.unresolved:
            rec["path"] = "unresolved"
            await send_message(chat_id, unresolved(res))
            return

        # ICAO <-> name mapping: answer deterministically from the static table.
        # No retrieval, no LLM — the safest possible path.
        if ex.intent == "icao_lookup":
            rec["path"] = "mapping"
            full = resolver.aerodrome_full_name(res.icao) or res.label
            await send_message(
                chat_id,
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
                await send_message(chat_id, chart_intro(res, ex))
                # For instrument approaches, show the AD 2.22 procedures (holding,
                # letdown, missed approach) as text, then the plate itself.
                if chart_icao not in ("GEN", "DNKK") and _is_approach(ex, text):
                    await _send_approach_procedures(chat_id, text, ex, res)
                await send_charts(chat_id, charts, requested_runway=ex.runway)
            else:
                rec["path"] = "chart_not_found"
                await send_message(chat_id, chart_not_found(res, ex))
            return

        # 3) embed an enriched query: expands the aerodrome name (PH -> Port
        #    Harcourt) and, for airspace, prepends AIP airspace terminology.
        search_text = resolver.build_search_text(ex, res, text)
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
            await send_message(chat_id, low_confidence(outcome))
            # still offer charts below if we have an ICAO
        elif outcome.abstained:
            rec["path"] = "not_found"
            await send_message(chat_id, not_found())
        else:
            status, ga = await asyncio.to_thread(
                synthesize.synthesize_decision, text, outcome.results)
            rec["path"] = status if status in ("grounded", "not_in_aip") else "answer"
            if status == "grounded":
                await send_message(chat_id, grounded_reply(ga, outcome, res))
            elif status == "not_in_aip":
                await send_message(chat_id, not_in_aip(res))
            else:
                await send_message(chat_id, answer(outcome, res, ex.runway))

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
                similarity=rec["similarity"], charts=rec["charts"])
        except Exception:  # noqa: BLE001
            log.exception("query log failed")
