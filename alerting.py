"""
alerting.py — tell the operator when something breaks, instead of it sitting
silent in a log until someone notices the bot's gone quiet.

report(key, ok, detail) is the whole interface. On a healthy->degraded flip it
sends a throttled alert to the admin Telegram chat; on degraded->healthy it sends
a one-line recovery notice. Best-effort and self-contained (its own sync HTTP
call), so it can fire from anywhere and never breaks the caller.

Set ADMIN_CHAT_ID (your Telegram chat id) to enable. ALERT_ENABLED=0 disables.
"""
import logging
import time

import httpx

import config

log = logging.getLogger("vannie.alert")

_state: dict = {}   # key -> bool (last known health)
_last: dict = {}    # key -> monotonic time of last degradation alert


def _send(text: str) -> None:
    if not (config.ALERT_ENABLED and config.ADMIN_CHAT_ID and config.TELEGRAM_BOT_TOKEN):
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": config.ADMIN_CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception:  # noqa: BLE001 — alerting must never break the caller
        log.exception("alert send failed")


def report(key: str, ok: bool, detail: str = "") -> None:
    """Record a component's health and alert on state transitions.
      healthy -> degraded : throttled ⚠️ alert (once per ALERT_MIN_INTERVAL)
      degraded -> healthy : ✅ recovery notice (not throttled)"""
    prev = _state.get(key)
    _state[key] = ok

    if not ok:
        now = time.monotonic()
        if key in _last and (now - _last[key]) < config.ALERT_MIN_INTERVAL:
            return
        _last[key] = now
        _send(f"⚠️ Vannie: {key} DEGRADED — {detail or 'check failed'}")
    elif prev is False:
        _last.pop(key, None)
        _send(f"✅ Vannie: {key} recovered")


def test_alert() -> bool:
    """Send a one-off test alert so you can confirm the wiring end-to-end."""
    _send("🔔 Vannie alert test — if you see this, alerting is wired correctly.")
    return bool(config.ALERT_ENABLED and config.ADMIN_CHAT_ID)
