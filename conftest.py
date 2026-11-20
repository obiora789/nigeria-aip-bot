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
"""
import os

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


@pytest.fixture(autouse=True)
def _fresh_enr_names():
    try:
        import resolver
    except Exception:  # noqa: BLE001 — a test that never imports resolver
        yield
        return
    resolver._PUBLISHED_ENTITIES = None
    resolver._PUBLISHED_PARTIAL = None
    resolver._PUBLISHED_RETRY_AT = 0.0
    import database
    database._RETRY_PAUSES = (0, 0, 0)   # refused reads retry without waiting
    yield
