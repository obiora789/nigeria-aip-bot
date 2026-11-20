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
"""
import pytest


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
    yield
