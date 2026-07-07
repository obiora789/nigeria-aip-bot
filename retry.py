"""
retry.py — exponential backoff with jitter for transient external-call failures.

Wraps OpenAI (and any) calls so a rate-limit (429) or a brief network/5xx blip
becomes a short wait-and-succeed instead of a failed answer. This is resilience,
not just scale: even modest bursts can momentarily spike OpenAI calls past the
per-minute limit, and without backoff those turn into user-visible failures.

Only TRANSIENT errors are retried (429, timeouts, connection resets, 5xx). Real
errors (bad request, auth) raise immediately — retrying them just wastes time.
After the last attempt the exception propagates, so the caller's existing
fallback (abstention / "busy, try again") still runs.

Synchronous by design: the OpenAI calls already run in a threadpool
(asyncio.to_thread), so a blocking sleep here never stalls the event loop.
"""
import logging
import random
import time

log = logging.getLogger("vannie.retry")

# Exception CLASS NAMES treated as transient (matched by name to avoid importing
# openai/httpx here and coupling this module to their versions).
_TRANSIENT_NAMES = {
    "RateLimitError", "APITimeoutError", "APIConnectionError",
    "InternalServerError", "APIError",                       # OpenAI SDK
    "ConnectError", "ReadTimeout", "ConnectTimeout", "WriteTimeout",
    "PoolTimeout", "RemoteProtocolError", "ReadError",        # httpx
}
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


def is_transient(exc: Exception) -> bool:
    if type(exc).__name__ in _TRANSIENT_NAMES:
        return True
    code = (getattr(exc, "status_code", None)
            or getattr(getattr(exc, "response", None), "status_code", None))
    return code in _TRANSIENT_STATUS


def retry_call(fn, *args, attempts: int = 3, base: float = 0.5, cap: float = 8.0,
               **kwargs):
    """Call fn(*args, **kwargs) with exponential backoff + jitter on transient
    errors. Re-raises on non-transient errors or after the final attempt."""
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            if not is_transient(e) or i == attempts - 1:
                raise
            delay = min(cap, base * (2 ** i)) + random.uniform(0, base)
            log.warning("transient %s — retry %d/%d in %.1fs",
                        type(e).__name__, i + 1, attempts - 1, delay)
            time.sleep(delay)
