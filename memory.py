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
    """Current non-expired context: {'last_icao':…, 'pending':{…}, 'last_query':…} or {}."""
    if not config.CONTEXT_ENABLED:
        return {}
    try:
        r = (supabase.table("conversation_context").select("*")
             .eq("chat_hash", _hash(chat_id)).limit(1).execute())
        rows = r.data or []
        if not rows or _past(rows[0].get("expires_at")):
            return {}
        return {"last_icao": rows[0].get("last_icao"),
                "pending": rows[0].get("pending"),
                "last_query": rows[0].get("last_query")}
    except Exception:  # noqa: BLE001
        log.exception("context load failed")
        return {}


def _write(chat_id, *, last_icao, pending, last_query=None) -> None:
    if not config.CONTEXT_ENABLED:
        return
    now = dt.datetime.now(dt.timezone.utc)
    exp = now + dt.timedelta(minutes=config.CONTEXT_TTL_MIN)
    row = {"chat_hash": _hash(chat_id), "last_icao": last_icao, "pending": pending,
           "last_query": last_query,
           "updated_at": now.isoformat(), "expires_at": exp.isoformat()}
    try:
        supabase.table("conversation_context").upsert(row).execute()
    except Exception:  # noqa: BLE001
        log.exception("context save failed")


def clear(chat_id) -> bool:
    """Forget everything remembered about this chat. Returns True on success.

    DELETES the row rather than writing nulls into it. A row of nulls still has
    an expires_at and still satisfies "context exists", which is the state that
    made this necessary: an aerodrome pinned by an earlier turn kept answering
    later, unrelated questions ("Using your last aerodrome, Abuja") long after
    the pilot had moved on.

    Carries no aerodrome, no pending request, no last query — a pilot asking
    for a reset means all of it, and leaving one field behind is exactly how a
    stale value survives a reset that appeared to work."""
    if not config.CONTEXT_ENABLED:
        return True
    try:
        supabase.table("conversation_context") \
            .delete().eq("chat_hash", _hash(chat_id)).execute()
        return True
    except Exception:  # noqa: BLE001
        log.exception("context clear failed")
        return False


def save_pending(chat_id, ex, raw: str, last_icao=None) -> None:
    """Remember a request awaiting an aerodrome (keeps any last_icao)."""
    _write(chat_id, last_icao=last_icao, last_query=raw, pending={
        "intent": ex.intent, "procedure_type": ex.procedure_type,
        "runway": ex.runway, "raw": raw})


def save_last(chat_id, icao, last_query=None) -> None:
    """Remember the last aerodrome + query and clear any pending slot-fill."""
    if icao:
        _write(chat_id, last_icao=icao, pending=None, last_query=last_query)


def save_scope_pending(chat_id, icao, ident, query, qid, last_icao=None) -> None:
    """Remember an aerodrome-or-navaid choice awaiting a button tap.

    A VOR ident names both — "ABC" is Abuja AND the ABC VOR/DME in ENR 4.1 —
    so the original QUERY is stored and re-run once the pilot picks. qid guards
    against a stale tap acting on a request that has since been replaced.

    Its own writer, like save_chart_pending: save_pending() builds a DIFFERENT
    shape from an extraction object and takes (chat_id, ex, raw), so calling it
    with a ready-made dict raised TypeError on this path and the bot answered
    "something went wrong" — and even had it accepted, the record would have
    carried no `kind` or `qid` for the callback guard to match."""
    # DO NOT write last_icao from `icao`. save_chart_pending does, correctly —
    # there the aerodrome is already settled. Here it is the OPEN QUESTION, and
    # recording DNAA as the conversation's aerodrome asserts the very reading
    # we are asking the pilot to choose.
    #
    # Confirmed live: "What is the frequency of ABC?" asked correctly the first
    # time, then the SAME question 26 seconds later skipped the prompt and
    # answered from Abuja's AD 2.17 — because the first ask had quietly pinned
    # last_icao=DNAA and the follow-up context path resolved straight to it.
    # An ambiguity must not resolve itself as a side effect of being raised.
    _write(chat_id, last_icao=last_icao, last_query=query,
           pending={"kind": "scope_clar", "icao": icao, "ident": ident,
                    "query": query, "qid": qid})


def save_chart_pending(chat_id, icao, label, ptype, runway, qid, last_icao=None) -> None:
    """Remember an under-specified chart request awaiting a clarifying answer
    (a button tap or a bare type/runway token). qid guards against stale taps;
    label lets the tap handler rebuild the reply without re-resolving."""
    _write(chat_id, last_icao=last_icao or icao, last_query=None,
           pending={"kind": "chart_clar", "icao": icao, "label": label,
                    "type": ptype or "", "runway": runway or "", "qid": qid})

