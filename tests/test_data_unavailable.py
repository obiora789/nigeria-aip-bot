"""
test_data_unavailable.py — a failed database read is never "not published".

Measured 2026-11-07/09: Supabase dropped ~0.5% of reads ("Server disconnected")
and every one silently became empty data, i.e. "not in the AIP". These tests
pin the fix: dropped connections are retried, anything still failing raises
database.DataUnavailable, and the pilot is told the database could not be
reached — never that a section is unpublished.

No network. Run with:  pytest tests/test_data_unavailable.py -q
"""
import os

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import httpx  # noqa: E402


class FakeQuery:
    """Any chain of query-builder calls; execute() follows a script of outcomes."""

    def __init__(self, script):
        self.script = script

    def __getattr__(self, name):
        return lambda *a, **k: self

    def execute(self):
        self.script["calls"] += 1
        step = self.script["steps"].pop(0) if self.script["steps"] else self.script["default"]
        if isinstance(step, Exception):
            raise step
        return type("R", (), {"data": step})()


class FakeSupabase:
    def __init__(self, steps=None, default=None):
        self.script = {"steps": list(steps or []), "default": default, "calls": 0}

    def table(self, *a, **k):
        return FakeQuery(self.script)

    def rpc(self, *a, **k):
        return FakeQuery(self.script)


def _drop():
    return httpx.RemoteProtocolError("Server disconnected")


class _Swap:
    """Swap database.supabase and remove retry pauses for one test."""

    def __init__(self, fake):
        self.fake = fake

    def __enter__(self):
        import database
        self.db = database
        self.real = (database.supabase, database._RETRY_PAUSES)
        database.supabase, database._RETRY_PAUSES = self.fake, (0, 0, 0)
        return self.fake

    def __exit__(self, *exc):
        self.db.supabase, self.db._RETRY_PAUSES = self.real
        return False


# ------------------------------------------------------------- _execute
def test_a_dropped_connection_is_retried_and_succeeds():
    import database
    fake = FakeSupabase(steps=[_drop(), [{"content": "TWR 118.1 MHZ", "source_page": 1}]])
    with _Swap(fake):
        assert database.get_subsection_text("DNMM", "AD 2.18") == "TWR 118.1 MHZ"
    assert fake.script["calls"] == 2


def test_a_read_that_keeps_failing_raises_never_returns_empty():
    import database
    fake = FakeSupabase(default=_drop())
    with _Swap(fake):
        try:
            database.get_subsection_text("DNDS", "AD 2.15")
            raise AssertionError("returned instead of raising")
        except database.DataUnavailable:
            pass
    assert fake.script["calls"] == database.READ_ATTEMPTS


def test_a_bad_query_is_not_retried_but_still_raises():
    import database
    fake = FakeSupabase(default=ValueError("column does not exist"))
    with _Swap(fake):
        try:
            database.get_charts("DNAA")
            raise AssertionError("returned instead of raising")
        except database.DataUnavailable:
            pass
    assert fake.script["calls"] == 1


def test_every_read_raises_instead_of_returning_empty_data():
    """All thirteen reads. Before 2026-11-09 each returned "" or [] here —
    the exact shape of an unpublished section."""
    import database
    emb = [0.0] * 3
    from models import Resolution
    reads = {
        "search_aip": lambda: database.search_aip(emb, Resolution(icao="DNAA", reference="DNAA")),
        "get_charts": lambda: database.get_charts("DNAA"),
        "get_section_text": lambda: database.get_section_text("DNAA", "AD 2.22"),
        "search_facts": lambda: database.search_facts(emb, "DNAA"),
        "search_facts_scoped": lambda: database.search_facts_scoped(emb, "ENR_POINT", "TEMSA"),
        "list_scope_ids": lambda: database.list_scope_ids("ENR_ROUTE"),
        "find_aip_scope": lambda: database.find_aip_scope("TEMSA"),
        "get_subsection_text": lambda: database.get_subsection_text("DNAA", "AD 2.18"),
        "get_declared_distances": lambda: database.get_declared_distances("DNAA"),
        "get_runway_physical_data": lambda: database.get_runway_physical_data("DNAA"),
        "get_lighting_data": lambda: database.get_lighting_data("DNAA"),
        "get_aerodrome_data": lambda: database.get_aerodrome_data("DNAA"),
        "get_charts_smart": lambda: database.get_charts_smart("DNAA", "ILS", ""),
    }
    for name, call in reads.items():
        with _Swap(FakeSupabase(default=_drop())):
            try:
                out = call()
            except database.DataUnavailable:
                continue
            raise AssertionError(f"{name} returned {out!r} on a failed read")


def test_empty_results_are_still_empty_when_the_read_works():
    """A section that is genuinely not stored must still come back empty."""
    import database
    with _Swap(FakeSupabase(default=[])):
        assert database.get_subsection_text("DNSK", "AD 2.15") == ""
        assert database.get_charts("DNSK") == []
        assert database.list_scope_ids("ENR_ROUTE") == []


def test_list_scope_ids_retries_each_page():
    import database
    page1 = [{"scope_id": f"R{i}"} for i in range(1000)]
    fake = FakeSupabase(steps=[page1, _drop(), [{"scope_id": "LAST"}]])
    with _Swap(fake):
        ids = database.list_scope_ids("ENR_ROUTE")
    assert len(ids) == 1001 and "LAST" in ids


# ------------------------------------------- ENR names are never cached partial
def test_a_failed_read_never_leaves_enr_names_cached_incomplete():
    import database, resolver
    saved = (resolver._PUBLISHED_ENTITIES, resolver._PUBLISHED_PARTIAL,
             resolver._PUBLISHED_RETRY_AT, database.list_scope_ids)
    try:
        resolver._PUBLISHED_ENTITIES = resolver._PUBLISHED_PARTIAL = None
        resolver._PUBLISHED_RETRY_AT = 0.0

        def down(kind):
            raise database.DataUnavailable("dropped")
        database.list_scope_ids = down
        partial = resolver.published_entities()
        assert resolver._PUBLISHED_ENTITIES is None, "a partial set must not be cached"
        assert "TEMSA" not in partial

        database.list_scope_ids = lambda kind: ["TEMSA"] if kind == "ENR_POINT" else []
        assert resolver.published_entities() is partial, "within the retry window"
        resolver._PUBLISHED_RETRY_AT = 0.0                      # window over
        full = resolver.published_entities()
        assert full.get("TEMSA") == "ENR_POINT"
        assert resolver._PUBLISHED_ENTITIES is full, "a complete read is cached"
    finally:
        (resolver._PUBLISHED_ENTITIES, resolver._PUBLISHED_PARTIAL,
         resolver._PUBLISHED_RETRY_AT, database.list_scope_ids) = saved


# -------------------------------------- the double-meaning checks degrade safely
def test_a_failed_read_shows_the_button_and_is_not_a_heliport():
    """Measured 2026-11-09: Dutse's AD 2.15 read dropped and the button was
    left off as if Dutse published no other lighting."""
    import disambiguate
    disambiguate._heliport_cache.clear()
    with _Swap(FakeSupabase(default=_drop())):
        assert disambiguate.alternative_exists("lighting", "DNDS") is True
        assert disambiguate.is_heliport("DNDS") is False
    disambiguate._heliport_cache.clear()


# ----------------------------------------------------------- what the pilot sees
def test_the_pilot_is_told_the_database_was_unreachable():
    import inspect, main, responder
    msg = responder.data_unavailable()
    assert "couldn't reach the AIP database" in msg
    assert "not published" not in msg.lower()
    src = inspect.getsource(main.process)
    i = src.find("except DataUnavailable:")
    assert i != -1 and i < src.find("    except Exception:  # noqa: BLE001\n        rec[\"path\"] = \"error\""), \
        "DataUnavailable must be handled before the generic error"
    assert 'rec["path"] = "data_unavailable"' in src and "data_unavailable()" in src
