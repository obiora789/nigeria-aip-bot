"""
test_audit.py — the audit items of 2026-10-27, pinned so they cannot quietly
regress: answer audit trail, pinned models, a webhook that fails closed, AI
disclosure, privacy notice, erasure and retention.

No network. Run with:  pytest tests/test_audit.py -q
"""
import os
import re

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import datetime as dt  # noqa: E402


class FakeTable:
    """Records what would be sent to Supabase; can fail like PostgREST does."""

    def __init__(self, db, name):
        self.db, self.name, self.op, self.filters = db, name, None, []

    def insert(self, row):
        self.op, self.row = "insert", dict(row)
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, k, v):
        self.filters.append(("eq", k, v))
        return self

    def lt(self, k, v):
        self.filters.append(("lt", k, v))
        return self

    def execute(self):
        if self.op == "insert" and self.db.reject_columns:
            bad = [c for c in self.db.reject_columns if c in self.row]
            if bad:
                raise RuntimeError(f"PGRST204 Could not find the '{bad[0]}' column")
        self.db.calls.append((self.name, self.op, getattr(self, "row", None), self.filters))
        return type("R", (), {"data": [{}] * self.db.delete_n if self.op == "delete" else []})()


class FakeDB:
    def __init__(self, reject_columns=(), delete_n=0):
        self.calls, self.reject_columns, self.delete_n = [], tuple(reject_columns), delete_n

    def table(self, name):
        return FakeTable(self, name)


def _with_db(fake):
    """Swap in the fake query-log table and switch logging ON for this test.

    CI runs with QUERY_LOG_ENABLED=0 (measured 2026-11-20: these tests failed
    there and passed on the Mac). They test logging, so they turn it on
    themselves rather than depend on the machine's setting."""
    import config, observability
    real = (observability.supabase, config.QUERY_LOG_ENABLED)
    observability.supabase, config.QUERY_LOG_ENABLED = fake, True
    return real


def _restore(real):
    import config, observability
    observability.supabase, config.QUERY_LOG_ENABLED = real


# ------------------------------------------------------------ 2. audit trail
def test_every_logged_question_carries_its_answer_and_what_produced_it():
    import config, observability
    db = FakeDB()
    real = _with_db(db)
    observability._audit_columns_ok = True
    try:
        observability.log_query(chat_id=1, query="Lagos tower frequency", path="comms",
                                qid="q1", reply="Lagos — AD 2.18 ... 118.1 MHz")
    finally:
        _restore(real)
    row = db.calls[-1][2]
    assert row["reply"].startswith("Lagos — AD 2.18")
    assert config.EXTRACTION_MODEL in row["models"] and config.SYNTHESIS_MODEL in row["models"]
    assert config.EMBEDDING_MODEL in row["models"]
    assert re.fullmatch(r"[0-9a-f]{12}", row["prompt_version"])
    assert row["airac"] == config.AIRAC_CYCLE
    assert "app_version" in row


def test_logging_survives_a_table_without_the_audit_columns():
    """Code deployed before sql/14_audit_trail.sql has run must still log."""
    import observability
    db = FakeDB(reject_columns=("reply",))
    real = _with_db(db)
    observability._audit_columns_ok = True
    try:
        observability.log_query(chat_id=1, query="q", path="comms", reply="r")
        assert observability._audit_columns_ok is False, "says what to run"
        observability.log_query(chat_id=1, query="q2", path="comms", reply="r2")
    finally:
        _restore(real)
        observability._audit_columns_ok = True
    rows = [c[2] for c in db.calls if c[1] == "insert"]
    assert [r["query"] for r in rows] == ["q", "q2"], "no question lost"
    assert all("reply" not in r for r in rows)


def test_long_replies_are_truncated_not_dropped():
    import observability
    db = FakeDB()
    real = _with_db(db)
    try:
        observability.log_query(query="q", reply="x" * 20000)
    finally:
        _restore(real)
    assert len(db.calls[-1][2]["reply"]) == observability.REPLY_MAX


def test_the_prompt_fingerprint_changes_when_any_prompt_changes():
    import disambiguate, observability
    observability._prompt_version = None
    a = observability.prompt_version()
    real = disambiguate.JUDGE_SYSTEM
    disambiguate.JUDGE_SYSTEM = real + " changed"
    observability._prompt_version = None
    try:
        b = observability.prompt_version()
    finally:
        disambiguate.JUDGE_SYSTEM = real
        observability._prompt_version = None
    assert a != b and len(a) == 12


def test_main_logs_the_reply_it_sent():
    """GUARD (main.py's request path is not executed offline)."""
    import inspect, main
    src = inspect.getsource(main.process)
    assert "_REPLIES.set(" in src and "_REPLIES.reset(" in src
    assert 'reply="\\n\\n".join(_replies)' in src
    assert "_capture(text)" in inspect.getsource(main.send_message)
    assert "_capture(" in inspect.getsource(main.send_charts)


# ------------------------------------------------------------- 3. pinned models
def test_models_are_pinned_to_dated_snapshots():
    import config
    for name in (config.EXTRACTION_MODEL, config.SYNTHESIS_MODEL):
        if os.getenv("EXTRACTION_MODEL") or os.getenv("SYNTHESIS_MODEL"):
            break                                   # an explicit override is the operator's choice
        if name in config.NO_DATED_SNAPSHOT:
            continue                                # OpenAI lists no dated snapshot to pin
        assert re.search(r"-\d{4}-\d{2}-\d{2}$", name), f"{name} is an alias, not a snapshot"


def test_only_alias_only_models_may_go_unpinned():
    # The exception is a fixed list, so a typo or a new alias cannot slip
    # through: gpt-4o-mini (which has snapshots) is still refused.
    import config
    assert config.NO_DATED_SNAPSHOT == {"gpt-6-luna"}
    assert "gpt-4o-mini" not in config.NO_DATED_SNAPSHOT


def test_the_bot_token_is_not_logged():
    # httpx logs each request URL at INFO, and Telegram's URL holds the token.
    import logging, main  # noqa: F401
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


def test_the_model_shutdown_is_warned_in_time():
    import config
    m = config.EXTRACTION_MODEL
    if m not in config.MODEL_SHUTDOWN:
        return
    end = dt.date.fromisoformat(config.MODEL_SHUTDOWN[m])
    assert config.model_shutdown_warnings(end - dt.timedelta(days=400)) == []
    near = config.model_shutdown_warnings(end - dt.timedelta(days=30))
    assert near and "re-run the full checks" in near[0]
    assert "was shut down" in config.model_shutdown_warnings(end + dt.timedelta(days=1))[0]


# --------------------------------------------------------- 4. webhook secret
def test_the_webhook_fails_closed_and_compares_in_constant_time():
    import inspect, config, telegram
    real = (config.TELEGRAM_WEBHOOK_SECRET, config.ALLOW_INSECURE_WEBHOOK)
    try:
        config.TELEGRAM_WEBHOOK_SECRET, config.ALLOW_INSECURE_WEBHOOK = "", False
        assert telegram.verify_secret(None) is False, "no secret set: refuse"
        assert telegram.verify_secret("anything") is False
        config.ALLOW_INSECURE_WEBHOOK = True
        assert telegram.verify_secret(None) is True, "development override only"
        config.TELEGRAM_WEBHOOK_SECRET, config.ALLOW_INSECURE_WEBHOOK = "s3cret", False
        assert telegram.verify_secret("s3cret") is True
        assert telegram.verify_secret("s3cre") is False
        assert telegram.verify_secret(None) is False
        assert telegram.verify_secret("") is False
    finally:
        config.TELEGRAM_WEBHOOK_SECRET, config.ALLOW_INSECURE_WEBHOOK = real
    assert "compare_digest" in inspect.getsource(telegram.verify_secret)


# ----------------------------------------------------------- 6. AI disclosure
def test_vannie_says_it_is_ai_and_who_runs_it():
    import config
    for text in (config.GREETING, config.HELP):
        assert "AI" in text and "OpenAI" in text
    assert "AI" in config.DISCLAIMER
    assert "Reference aid only" in config.DISCLAIMER, "e2e.py checks this fragment"
    assert "/reset" in config.HELP and "/privacy" in config.HELP and "/deletemydata" in config.HELP


# ------------------------------------------------- 7. privacy and retention
def test_the_privacy_notice_states_storage_retention_processors_and_rights():
    import config
    real = (config.PRIVACY_CONTACT, config.LOG_RETENTION_DAYS)
    try:
        config.PRIVACY_CONTACT, config.LOG_RETENTION_DAYS = "", 180
        p = config.privacy_notice()
        assert "180 days" in p and "OpenAI" in p and "/deletemydata" in p
        assert "Data Protection Commission" in p
        assert "Contact:" not in p, "no invented contact"
        config.PRIVACY_CONTACT, config.LOG_RETENTION_DAYS = "dpo@example.org", 0
        p = config.privacy_notice()
        assert "dpo@example.org" in p and "until deleted" in p
    finally:
        config.PRIVACY_CONTACT, config.LOG_RETENTION_DAYS = real


def test_erasure_deletes_this_chats_rows_only():
    import config, observability
    db = FakeDB(delete_n=3)
    real = _with_db(db)
    pep = config.CHAT_HASH_PEPPER
    try:
        config.CHAT_HASH_PEPPER = "pepper"
        assert observability.delete_chat_logs(42) == 3
        name, op, _, filters = db.calls[-1]
        assert (name, op) == ("vannie_query_log", "delete")
        assert filters == [("eq", "chat_hash", observability._hash_chat(42))]
        config.CHAT_HASH_PEPPER = ""
        assert observability.delete_chat_logs(42) is None, "unlinkable rows: say so"
    finally:
        config.CHAT_HASH_PEPPER = pep
        _restore(real)


def test_retention_deletes_old_logs_and_expired_context():
    import config, observability
    db = FakeDB()
    real = _with_db(db)
    days = config.LOG_RETENTION_DAYS
    try:
        config.LOG_RETENTION_DAYS = 180
        observability.run_retention()
        tables = [(c[0], c[1]) for c in db.calls]
        assert ("conversation_context", "delete") in tables
        assert ("vannie_query_log", "delete") in tables
        db.calls.clear()
        config.LOG_RETENTION_DAYS = 0
        observability.run_retention()
        assert [c[0] for c in db.calls] == ["conversation_context"], "0 = keep logs"
    finally:
        config.LOG_RETENTION_DAYS = days
        _restore(real)


def test_context_keys_use_the_secret_pepper():
    import config, hashlib, memory
    pep = config.CHAT_HASH_PEPPER
    old = hashlib.sha256(b"vannie:42").hexdigest()[:16]
    try:
        config.CHAT_HASH_PEPPER = ""
        assert memory._hash(42) == old, "without a pepper, context still works"
        config.CHAT_HASH_PEPPER = "pepper"
        assert memory._hash(42) != old, "the public prefix is reversible"
        import observability
        assert memory._hash(42) != observability._hash_chat(42), "log and context keys differ"
    finally:
        config.CHAT_HASH_PEPPER = pep


def test_privacy_commands_exist_and_are_not_logged():
    """GUARD (main.py's request path is not executed offline)."""
    import inspect, main
    src = inspect.getsource(main.process)
    for cmd in ('"/privacy"', '"/deletemydata"'):
        i = src.find(cmd)
        assert i != -1, cmd
        assert 'rec["skip_log"] = ' in src[i:i + 300], f"{cmd} must not be logged"
    assert "observability.delete_chat_logs" in src and "memory.clear" in src
    warm = inspect.getsource(main._warm)
    assert "_retention_loop()" in warm and "model_shutdown_warnings" in warm
    assert "TELEGRAM_WEBHOOK_SECRET" in warm


# ------------------------------------- switching model (2026-11-07, gpt-6-luna)
def _efforts(ex, sy):
    import config
    real = (config.EXTRACTION_REASONING_EFFORT, config.SYNTHESIS_REASONING_EFFORT)
    config.EXTRACTION_REASONING_EFFORT, config.SYNTHESIS_REASONING_EFFORT = ex, sy
    return real


def test_gpt_4o_mini_gets_exactly_what_it_always_got():
    import config
    for m in ("gpt-4o-mini", "gpt-4o-mini-2024-07-18"):
        assert config.model_params(m, "extraction") == {"temperature": 0.0}
        assert config.model_params(m, "synthesis") == {"temperature": 0.0}
        assert config.reasoning_effort_for(m, "extraction") is None


def test_a_reasoning_model_gets_its_effort_and_temperature_only_when_off():
    """gpt-6-luna accepts temperature only with reasoning_effort 'none'."""
    import config
    real = _efforts("none", "low")
    try:
        assert config.model_params("gpt-6-luna", "extraction") == {
            "extra_body": {"reasoning_effort": "none"}, "temperature": 0.0}
        assert config.model_params("gpt-6-luna", "synthesis") == {
            "extra_body": {"reasoning_effort": "low"}}, "no temperature with reasoning on"
        assert config.model_params("gpt-6-luna-2026-09-23", "extraction")["extra_body"]
    finally:
        config.EXTRACTION_REASONING_EFFORT, config.SYNTHESIS_REASONING_EFFORT = real


def test_every_chat_call_goes_through_the_switch():
    """A call that still hard-codes temperature would be rejected by a
    reasoning model with reasoning on, or silently differ from the others."""
    import inspect, agent, disambiguate, synthesize
    for fn, role in ((agent.extract_query_parameters, '"extraction"'),
                     (disambiguate.judge, '"extraction"'),
                     (synthesize.generate_grounded_answer, '"synthesis"')):
        src = inspect.getsource(fn)
        assert "config.model_params(" in src and role in src, fn.__name__
        assert "temperature=" not in src, fn.__name__


def test_the_log_records_the_effort_with_the_model():
    import config, observability
    real_models = (config.EXTRACTION_MODEL, config.SYNTHESIS_MODEL)
    real = _efforts("none", "low")
    try:
        config.EXTRACTION_MODEL = config.SYNTHESIS_MODEL = "gpt-6-luna"
        m = observability._models()
        assert "extract=gpt-6-luna[none]" in m and "synth=gpt-6-luna[low]" in m
        config.EXTRACTION_MODEL = config.SYNTHESIS_MODEL = "gpt-4o-mini-2024-07-18"
        assert "[" not in observability._models()
    finally:
        config.EXTRACTION_MODEL, config.SYNTHESIS_MODEL = real_models
        config.EXTRACTION_REASONING_EFFORT, config.SYNTHESIS_REASONING_EFFORT = real
