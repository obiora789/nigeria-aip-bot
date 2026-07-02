"""
memory.py — short-term conversation context (best-effort, TTL-bounded).

Two jobs, both convenience-only and always overridable by what the pilot actually
says:
  • pending slot-fill — the bot asked "which aerodrome?"; remember the original
    request so a bare "Lagos" completes it instead of starting over.
  • last-aerodrome carry — remember the last aerodrome so "what about the ILS?"
    can resolve against it.

Hard rules live in main.process(): an explicit aerodrome ALWAYS overrides carried
context; carried context only fills a gap; and whenever context is used it is
SURFACED in the reply, never applied silently. This module is just storage.
"""
import datetime as dt
import hashlib
import logging

import config
from database import supabase

log = logging.getLogger("vannie.memory")


def _hash(chat_id) -> str:
    return hashlib.sha256(f"vannie:{chat_id}".encode()).hexdigest()[:16]


def _past(ts: str) -> bool:
    try:
        exp = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt.datetime.now(dt.timezone.utc) >= exp
    except Exception:  # noqa: BLE001
        return True


def load(chat_id) -> dict:
    """Current non-expired context: {'last_icao':…, 'pending':{…}} or {}."""
    if not config.CONTEXT_ENABLED:
        return {}
    try:
        r = (supabase.table("conversation_context").select("*")
             .eq("chat_hash", _hash(chat_id)).limit(1).execute())
        rows = r.data or []
        if not rows or _past(rows[0].get("expires_at")):
            return {}
        return {"last_icao": rows[0].get("last_icao"),
                "pending": rows[0].get("pending")}
    except Exception:  # noqa: BLE001
        log.exception("context load failed")
        return {}


def _write(chat_id, *, last_icao, pending) -> None:
    if not config.CONTEXT_ENABLED:
        return
    now = dt.datetime.now(dt.timezone.utc)
    exp = now + dt.timedelta(minutes=config.CONTEXT_TTL_MIN)
    row = {"chat_hash": _hash(chat_id), "last_icao": last_icao, "pending": pending,
           "updated_at": now.isoformat(), "expires_at": exp.isoformat()}
    try:
        supabase.table("conversation_context").upsert(row).execute()
    except Exception:  # noqa: BLE001
        log.exception("context save failed")


def save_pending(chat_id, ex, raw: str, last_icao=None) -> None:
    """Remember a request awaiting an aerodrome (keeps any last_icao)."""
    _write(chat_id, last_icao=last_icao, pending={
        "intent": ex.intent, "procedure_type": ex.procedure_type,
        "runway": ex.runway, "raw": raw})


def save_last(chat_id, icao) -> None:
    """Remember the last aerodrome and clear any pending slot-fill."""
    if icao:
        _write(chat_id, last_icao=icao, pending=None)
