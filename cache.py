"""
cache.py — dedup + per-chat throttle, Redis-backed with an in-memory fallback.

Why: the in-memory versions live in one instance's RAM, so they're wiped on every
restart (letting a duplicate update through or resetting throttles) and can't be
shared if you ever run a second instance. With REDIS_URL set, both become durable
and shared. Without it, behaviour is exactly as before — Redis is optional.

Async so the webhook hot path never blocks. Every Redis call is guarded: if Redis
hiccups, we fall back to in-memory rather than drop the request.
"""
import logging
from collections import deque

import config

log = logging.getLogger("vannie.cache")

# ── in-memory fallback state (also used if a Redis call fails) ────────────────
_seen_updates: deque = deque(maxlen=config.DEDUP_CACHE_SIZE)
_seen_set: set = set()
_last_seen: dict = {}   # chat_id -> event-loop time

# ── optional Redis (async) ────────────────────────────────────────────────────
_redis = None
if config.REDIS_URL:
    try:
        import redis.asyncio as aioredis
        _redis = aioredis.from_url(
            config.REDIS_URL, socket_timeout=2, socket_connect_timeout=2,
            decode_responses=True)
        log.info("cache: Redis configured")
    except Exception:  # noqa: BLE001
        _redis = None
        log.exception("cache: Redis init failed; using in-memory fallback")


async def ping() -> bool:
    """True if Redis is reachable (for startup logging / health). False if not
    configured or unreachable — the bot still works on the in-memory fallback."""
    if _redis is None:
        return False
    try:
        return bool(await _redis.ping())
    except Exception:  # noqa: BLE001
        log.exception("cache: Redis ping failed")
        return False


def _seen_in_memory(update_id) -> bool:
    if update_id in _seen_set:
        return True
    _seen_set.add(update_id)
    _seen_updates.append(update_id)
    if len(_seen_set) > len(_seen_updates):      # trim ids the deque evicted
        _seen_set.intersection_update(_seen_updates)
    return False


async def already_seen(update_id) -> bool:
    """True if this Telegram update_id was already processed (idempotency)."""
    if update_id is None:
        return False
    if _redis is not None:
        try:
            # SET NX EX -> truthy only when newly set; already-present => seen.
            was_new = await _redis.set(
                f"dedup:{update_id}", "1", nx=True, ex=config.DEDUP_TTL_SEC)
            return not was_new
        except Exception:  # noqa: BLE001
            log.exception("cache: redis dedup failed; in-memory fallback")
    return _seen_in_memory(update_id)


def _throttled_in_memory(chat_id, cooldown: float) -> bool:
    import asyncio
    now = asyncio.get_event_loop().time()
    if now - _last_seen.get(chat_id, 0.0) < cooldown:
        return True
    _last_seen[chat_id] = now
    return False


async def throttled(chat_id) -> bool:
    """True if this chat sent a message within the cooldown window."""
    cooldown = config.PER_CHAT_COOLDOWN_SECONDS
    if _redis is not None:
        try:
            was_new = await _redis.set(
                f"throttle:{chat_id}", "1", nx=True, px=int(cooldown * 1000))
            return not was_new     # couldn't set -> within cooldown -> throttled
        except Exception:  # noqa: BLE001
            log.exception("cache: redis throttle failed; in-memory fallback")
    return _throttled_in_memory(chat_id, cooldown)
