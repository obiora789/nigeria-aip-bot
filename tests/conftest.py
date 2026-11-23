"""
conftest.py — pytest loads this automatically before any test file.

Every test starts with the ENR-names cache in resolver.py empty.

Why (measured 2026-11-20): since 2026-11-09, a failed read of the ENR names
is kept as a "partial" set for 60 seconds instead of being cached forever
(resolver.published_entities). On the Mac, pytest uses the real supabase
library against the fake test URL, so an early test's read fails and leaves
that partial set behind. Tests that reset only resolver._PUBLISHED_ENTITIES
then got the partial set — VOR idents only, no TEMSA or KELAK — and
test_offline.py failed two tests that are correct. Bot behaviour is
unaffected; this only stops one test's leftovers reaching the next.

The tests never reach the network (measured 2026-11-20). GitHub's CI took 14
minutes per run where the Mac takes under one: two tests make 12 database
attempts, and on GitHub's machines the fake https://test.supabase.co address
can hang until a timeout instead of failing at once. Below, every address the
bot could call points at a closed port on this machine, so each attempt fails
instantly, and real keys from a .env can never be used by a test (no OpenAI
spend, no reads of the live database). pytest loads this file before any test
file, so these are in place before config.py reads them.

That was not enough on GitHub (CI #67, 20 Nov: still stuck at 30% after 10
minutes). Since supabase-py 2.29 (April 2026) the library retries a failed
read itself, waiting 1 + 2 + 4 seconds, and GitHub installs the newest
version while the Mac has an older one. So no test uses the real database
client at all: before every test, the client that database.py, memory.py and
observability.py hold is replaced by _Offline, which fails each read at once
with the same error a dropped connection gives. Tests that need data still
install their own fakes, as before. Measured: 38 tests make 120 reads; none
reaches Telegram or OpenAI.
"""
import sys as _sys, pathlib as _pl  # repo layout: bot at the top, scripts in folders
_R = next(d for d in _pl.Path(__file__).resolve().parents if (d / "main.py").exists())
_sys.path[1:1] = [str(p) for p in (_R, _R / "checks", _R / "ingestion", _R / "ingestion" / "validate")
                  if str(p) not in _sys.path]
import os

import httpx
import pytest

_NOWHERE = "http://127.0.0.1:9"          # closed port: refused immediately
os.environ.update({
    "SUPABASE_URL": _NOWHERE,
    "SUPABASE_KEY": "test",
    "OPENAI_API_KEY": "test",
    "OPENAI_BASE_URL": _NOWHERE + "/v1",
    "TELEGRAM_BOT_TOKEN": "test",
    "QUERY_LOG_ENABLED": "0",            # tests that check logging turn it on themselves
})


class _Offline:
    """Stands in for the Supabase client: any query chain, and execute()
    fails at once like a dropped connection (database.py retries it, then
    raises DataUnavailable — the same path a real outage takes)."""

    def __getattr__(self, name):
        return lambda *a, **k: self

    def execute(self):
        raise httpx.ConnectError("offline test: no database")


_OFFLINE = _Offline()


@pytest.fixture(autouse=True)
def _offline_and_fresh():
    try:
        import database
        import memory
        import observability
        import resolver
    except Exception:  # noqa: BLE001 — a test that never imports the bot
        yield
        return
    database.supabase = memory.supabase = observability.supabase = _OFFLINE
    database._RETRY_PAUSES = (0, 0, 0)   # failed reads retry without waiting
    resolver._PUBLISHED_ENTITIES = None
    resolver._PUBLISHED_PARTIAL = None
    resolver._PUBLISHED_RETRY_AT = 0.0
    yield
