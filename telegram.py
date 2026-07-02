"""
telegram.py — outbound delivery + webhook auth.

Plain text (no parse_mode) so AIP content never trips Markdown parsing. Charts
are sent as photos when they're images and as documents otherwise (a PDF sent to
sendPhoto fails). Webhook authenticity is checked against the secret token.
"""
import logging
from typing import List

import httpx

import config
from models import ChartRef
from responder import runway_warning, split_for_telegram

log = logging.getLogger("vannie.telegram")

_API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"


def verify_secret(header_value: str | None) -> bool:
    """If a webhook secret is configured, require an exact match."""
    if not config.TELEGRAM_WEBHOOK_SECRET:
        return True  # not configured (dev) — allow, but configure it in prod
    return header_value == config.TELEGRAM_WEBHOOK_SECRET


def feedback_kb(qid: str) -> dict:
    """Inline 👍/👎 keyboard; callback_data carries the query id for triage."""
    return {"inline_keyboard": [[
        {"text": "👍 Helpful", "callback_data": f"fb:up:{qid}"},
        {"text": "👎 Wrong", "callback_data": f"fb:down:{qid}"},
    ]]}


async def answer_callback(callback_id: str, text: str = "") -> None:
    """Acknowledge a button tap so Telegram stops the loading spinner."""
    async with httpx.AsyncClient(timeout=10) as http:
        try:
            await http.post(f"{_API}/answerCallbackQuery",
                            json={"callback_query_id": callback_id, "text": text})
        except Exception:  # noqa: BLE001
            log.exception("answerCallbackQuery error")


async def send_message(chat_id: int, text: str, reply_markup=None) -> None:
    parts = split_for_telegram(text)
    async with httpx.AsyncClient(timeout=15) as http:
        for i, part in enumerate(parts):
            body = {"chat_id": chat_id, "text": part,
                    "disable_web_page_preview": True}
            if reply_markup and i == len(parts) - 1:   # buttons under the last part
                body["reply_markup"] = reply_markup
            try:
                resp = await http.post(f"{_API}/sendMessage", json=body)
                if resp.status_code != 200:
                    log.warning("sendMessage %s: %s", resp.status_code, resp.text[:300])
            except Exception:  # noqa: BLE001
                log.exception("sendMessage error")


async def send_charts(chat_id: int, charts: List[ChartRef], requested_runway=None) -> None:
    if not charts:
        return
    async with httpx.AsyncClient(timeout=30) as http:
        for c in charts:
            caption = " ".join(
                b for b in [c.icao_code, c.procedure_type,
                            f"RWY {c.runway}" if c.runway else None, f"· {config.AIRAC_CYCLE}"]
                if b
            )
            # S5 — flag a chart that is the OPPOSITE runway end of what was asked.
            warn = runway_warning(requested_runway, c.runway)
            if warn:
                caption = f"{warn}\n{caption}"
            # S8 — terrain charts carry the obstacle-clearance safety caveat.
            if c.procedure_type and "terrain" in c.procedure_type.lower():
                caption = f"{caption}\n{config.S8_TERRAIN_CAVEAT}"

            endpoint, key = ("sendDocument", "document") if c.is_pdf else ("sendPhoto", "photo")
            try:
                resp = await http.post(f"{_API}/{endpoint}",
                                       json={"chat_id": chat_id, key: c.url, "caption": caption})
                if resp.status_code != 200:
                    log.warning("%s %s: %s", endpoint, resp.status_code, resp.text[:300])
                    # Fallback: if a photo was rejected, retry as a document.
                    if endpoint == "sendPhoto":
                        await http.post(f"{_API}/sendDocument",
                                        json={"chat_id": chat_id, "document": c.url, "caption": caption})
            except Exception:  # noqa: BLE001
                log.exception("send chart error")
