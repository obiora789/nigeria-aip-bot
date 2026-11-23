"""
test_disambiguation.py — six known bare questions: answer the usual reading,
offer the other with one button. Everything else: answered as typed.

No network. Run with:  pytest tests/test_disambiguation.py -q

Design since 2026-10-26 (option B). The model only labels the wording
(the topic copied from the question, one of six families, the place); the readings, button text
and rewritten questions are fixed in disambiguate.FAMILIES. Whether the model
labels well is measured, not unit-tested: quick_check.py (cheap) and
check_ambiguity.py (all 40 aerodromes).
"""
import os
import sys
import types

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

from types import SimpleNamespace as N  # noqa: E402

SIX = ("approach", "minima", "clearance", "lighting", "taxiing", "charges")


def _check(family="approach", topic=None, place="Sokoto"):
    return N(topic=family if topic is None else topic, family=family, place=place)


def _fake_client(parsed=None, boom=False, seen=None):
    class C:
        def parse(self, **kw):
            if seen is not None:
                seen.update(kw)
            if boom:
                raise RuntimeError("API down")
            return N(choices=[N(message=N(parsed=parsed))])
    return N(beta=N(chat=N(completions=C())))


# ------------------------------------------------------------------ decide
def test_a_bare_double_meaning_question_gives_its_family_and_place():
    import disambiguate as d
    assert d.decide(_check()) == ("approach", "Sokoto")
    for fam in SIX:
        assert d.decide(_check(fam, None, "Kano")) == (fam, "Kano")


def test_a_topic_of_more_than_one_word_means_a_single_meaning():
    """Measured 2026-10-26: 'approach plate for Sokoto', 'Abuja VOR approach',
    'ILS minima', 'landing fee', 'VFR clearance', 'clearance priorities' were
    all labelled double meanings at some point. The model's own topic, copied
    from the question, vetoes that whenever it is longer than one word."""
    import disambiguate as d
    for fam, topic in (("approach", "approach plate"), ("approach", "VOR approach"),
                       ("minima", "ILS minima"), ("minima", "take-off minima"),
                       ("charges", "landing fee"), ("clearance", "VFR clearance"),
                       ("clearance", "clearance priorities"), ("taxiing", "taxiway info"),
                       ("approach", "approach frequency")):
        assert d.decide(_check(fam, topic)) is None, topic
    for topic in ("approach", " approach ", "'approach'", "approach?"):
        assert d.decide(_check("approach", topic)) == ("approach", "Sokoto"), repr(topic)
    assert d.decide(_check("approach", "")) is None, "no topic: no evidence"
    # measured 2026-10-26: 'What is Soku approach' -> topic 'Soku approach'
    assert d.decide(_check("approach", "Soku approach", "Soku")) == ("approach", "Soku")
    assert d.decide(_check("lighting", "Birnin Kebbi lighting", "Birnin Kebbi")) == \
        ("lighting", "Birnin Kebbi")
    assert d.decide(_check("approach", "Soku approach plate", "Soku")) is None
    assert d.decide(_check("approach", "Sokoto approach", "Soku")) is None, \
        "only the model's own place is taken out"


def test_no_family_no_place_or_a_failed_check_means_no_button():
    import disambiguate as d
    assert d.decide(None) is None, "a failed check must never change the answer"
    assert d.decide(_check("none")) is None
    assert d.decide(_check("ats units")) is None, "only the six fixed families"
    assert d.decide(_check("approach", None, "")) is None, "the rewritten questions need a place"
    assert d.decide(N()) is None, "a malformed check must not crash"


def test_place_whitespace_is_tidied():
    import disambiguate as d
    assert d.decide(_check("lighting", None, "  Birnin   Kebbi ")) == ("lighting", "Birnin Kebbi")


# ------------------------------------------------------- fixed readings
def test_readings_are_fixed_default_first_and_name_the_place():
    import disambiguate as d
    r = d.readings("approach", "Sokoto")
    assert [x["question"] for x in r] == [
        "What is the frequency of Sokoto Approach?",
        "Show me the instrument approach plate for Sokoto"], \
        "Sokoto approach defers to the ATC unit (Obiora, 2026-10-21)"
    for fam in SIX:
        dq, aq = d.default_question(fam, "Kano"), d.alt_question(fam, "Kano")
        assert dq != aq, fam
        if fam != "charges":                      # GEN 4.2 is national
            assert "Kano" in dq and "Kano" in aq, fam
        assert len(d.FAMILIES[fam]["button"]) <= 40, "a phone shows ~40 characters"


def test_ask_still_returns_readings_for_the_check_scripts():
    """check_ambiguity.py / check_approach_routing.py read ask(q) as
    'treated as a double meaning?' and route every reading's question."""
    import agent, disambiguate as d
    real = agent.client
    try:
        agent.client = _fake_client(_check("charges", None, "Lagos"))
        opts = d.ask("charges at Lagos")
        assert [o["label"] for o in opts] == ["Aerodrome charges", "Air navigation charges"]
        agent.client = _fake_client(_check("none", "landing fee", "Lagos"))
        assert d.ask("landing fee at Lagos") is None
    finally:
        agent.client = real


def test_a_failing_model_call_means_no_button():
    import agent, disambiguate as d
    real = agent.client
    agent.client = _fake_client(boom=True)
    try:
        assert d.pick("Sokoto approach") is None
    finally:
        agent.client = real


def test_the_check_sends_its_own_prompt_and_the_question():
    import agent, disambiguate as d
    seen, real = {}, agent.client
    agent.client = _fake_client(_check(), seen=seen)
    try:
        assert d.pick("Show me sokoto approach") == ("approach", "Sokoto")
    finally:
        agent.client = real
    assert seen["messages"][0]["content"] == d.JUDGE_SYSTEM
    assert seen["messages"][1]["content"] == "Show me sokoto approach"
    assert seen["temperature"] == 0.0


# -------------------------------------------------------------- button
def test_the_button_carries_its_own_state_and_fits_telegram():
    import disambiguate as d
    b = d.alt_button("approach", "Sokoto", "DNSO")
    assert b["text"] == "Did you mean the approach plate?"
    assert b["callback_data"] == "alt:approach:Sokoto"
    assert d.from_callback(b["callback_data"]) == (
        "approach", "Show me the instrument approach plate for Sokoto")
    for fam in SIX:
        data = d.alt_button(fam, "Warri Industrial", "DNWI")["callback_data"]
        assert len(data.encode()) <= 64, "Telegram's callback_data limit"


def test_a_place_too_long_for_the_button_falls_back_to_the_icao():
    import disambiguate as d
    long = "Port Harcourt International Airport Omagwa Rivers State"
    assert d.alt_button("clearance", long, "DNPO")["callback_data"] == "alt:clearance:DNPO"
    assert d.alt_button("clearance", long, None) is None, "never a truncated, wrong place"


def test_bad_or_old_callbacks_do_nothing():
    import disambiguate as d
    for bad in ("alt:", "alt:approach:", "alt:nonsense:Kano", "amb:0:q1", "", None):
        assert d.from_callback(bad) is None, bad


# ---------------------------------------- is the alternative in the AIP?
def _with_database(charts=(), sections=None, boom=False):
    fake = types.ModuleType("database")
    sections = sections or {}

    def get_charts(icao, procedure_type="", runway=""):
        if boom:
            raise RuntimeError("db down")
        return list(charts)

    def get_subsection_text(icao, section):
        if boom:
            raise RuntimeError("db down")
        return sections.get((icao, section), "")
    fake.get_charts, fake.get_subsection_text = get_charts, get_subsection_text
    return fake


def _exists(fam, icao, **kw):
    import disambiguate as d
    real = sys.modules.get("database")
    sys.modules["database"] = _with_database(**kw)
    try:
        return d.alternative_exists(fam, icao)
    finally:
        if real is None:
            sys.modules.pop("database", None)
        else:
            sys.modules["database"] = real


def test_the_button_is_left_off_only_when_the_aip_plainly_has_nothing():
    assert _exists("lighting", "DNKN", sections={("DNKN", "AD 2.15"): "ABN ..."}) is True
    assert _exists("lighting", "DNKN") is False, "no AD 2.15 text: nothing to offer"
    assert _exists("taxiing", "DNKN") is False
    assert _exists("clearance", "DNKN") is False
    assert _exists("charges", "DNKN", sections={("NATIONAL", "GEN 4.2"): "x"}) is True
    # approach plate: any chart, or any AD 2.22 text (not the parsed headers,
    # which miss real approaches at some aerodromes)
    assert _exists("approach", "DNIL", charts=["iac"]) is True
    assert _exists("approach", "DNIL", sections={("DNIL", "AD 2.22"): "ILS RWY 23"}) is True
    assert _exists("approach", "DNWI") is False


def _heli(icao, **kw):
    import disambiguate as d
    real = sys.modules.get("database")
    sys.modules["database"] = _with_database(**kw)
    d._heliport_cache.clear()
    try:
        return d.is_heliport(icao), d.applies_here("approach", icao), d.applies_here("lighting", icao)
    finally:
        d._heliport_cache.clear()
        if real is None:
            sys.modules.pop("database", None)
        else:
            sys.modules["database"] = real


def test_at_a_heliport_approach_is_answered_as_typed():
    """Obiora, 2026-10-27: a heliport has no AD 2.7, AD 2.15 or AD 2.8 text
    in the stored AIP (Soku, DNSK), and 'Soku approach' is not a choice
    between an Approach unit and a plate there."""
    assert _heli("DNSK") == (True, False, True), "heliport: approach off, others unchanged"
    full = {("DNKN", s): "text" for s in ("AD 2.7", "AD 2.15", "AD 2.8")}
    assert _heli("DNKN", sections=full) == (False, True, True)
    one = {("DNKN", "AD 2.8"): "TWY A ..."}
    assert _heli("DNKN", sections=one)[0] is False, "ALL three must be empty"
    assert _heli(None) == (False, True, True), "no ICAO: not treated as a heliport"
    assert _heli("DNKN", boom=True) == (False, True, True), "database error: not a heliport"


def test_a_heliport_answer_is_never_cached():
    """An outage that returned empty results must not turn an aerodrome into
    a heliport for the life of the server."""
    import disambiguate as d
    real = sys.modules.get("database")
    d._heliport_cache.clear()
    try:
        sys.modules["database"] = _with_database()                       # empty: looks like a heliport
        assert d.is_heliport("DNKN") is True
        full = {("DNKN", s): "text" for s in ("AD 2.7", "AD 2.15", "AD 2.8")}
        sys.modules["database"] = _with_database(sections=full)          # data back
        assert d.is_heliport("DNKN") is False
        sys.modules["database"] = _with_database()                       # empty again
        assert d.is_heliport("DNKN") is False, "a proven aerodrome stays one"
    finally:
        d._heliport_cache.clear()
        if real is None:
            sys.modules.pop("database", None)
        else:
            sys.modules["database"] = real


def test_when_unsure_the_button_is_offered():
    assert _exists("lighting", None) is True, "no ICAO: offer it"
    assert _exists("lighting", "DNKN", boom=True) is True, "database error: offer it"
    assert _exists("minima", "DNKN") is True, "no reliable signal for take-off minima"


# ------------------------------------------------------------- schema/prompt
def test_the_model_writes_its_evidence_before_its_label():
    import agent, inspect, schemas
    fields = list(schemas.AmbiguityCheck.model_fields)
    assert fields == ["topic", "family", "place"], \
        "topic first: evidence before the label"
    fam = schemas.AmbiguityCheck.model_json_schema()["properties"]["family"]["enum"]
    assert sorted(fam) == sorted(("none",) + SIX)
    assert "interpretations" not in agent._SYSTEM
    assert "interpretations" not in inspect.getsource(schemas.AIPQueryExtraction)


def test_every_family_has_a_bare_example_and_the_traps_are_named():
    import re, disambiguate as d
    p = re.sub(r"\s+", " ", d.JUDGE_SYSTEM)
    for fam in SIX:
        assert f'"{fam}"' in p, fam
    for trap in ("'Lagos approach frequency'", "'Jos VOR approach'",
                 "'approach plate for Enugu'", "'ILS minima for Kano'",
                 "'take-off minima at Yola'", "'landing fee at Benin'",
                 "'runway lighting at Ilorin'", "'Kano fuel'"):
        assert trap in p, trap


def test_the_judge_prompt_uses_no_stress_set_question():
    """The stress set is the out-of-sample measure; its questions must not
    appear in the prompt as examples."""
    import csv, disambiguate as d
    path = next((p for p in ("quick_stress_294.csv", "stress_questions.csv")
                 if os.path.exists(p)), None)
    if not path:
        return
    p = d.JUDGE_SYSTEM.lower()
    for r in csv.DictReader(open(path, encoding="utf-8")):
        q = (r.get("question") or "").strip().lower()
        if len(q) > 12:
            assert f"'{q}'" not in p, q


# ----------------------------------------------------------------- main.py
def test_main_answers_the_default_and_offers_the_other():
    """GUARD (main.py's request path is not executed offline)."""
    import inspect, main
    src = inspect.getsource(main.process)
    start = src.find("asyncio.to_thread(disambiguate.pick, text)")
    assert start != -1 and start < src.find("extract_query_parameters, text"), \
        "the check must start alongside extraction, not after it"
    assert "None if from_choice else" in src, "a rewritten question must not be judged again"
    act = src.find("_pick = await _amb")
    assert act != -1, "main.process never acts on the check"
    assert src.find('if ex.intent == "out_of_scope":') < act, \
        "a refused question must not be answered as a double meaning"
    assert act < src.find("charges.section_for("), \
        "the charges route would answer 'charges at Lagos' first"
    block = src[act:src.find("charges.section_for(")]
    assert "disambiguate.default_question(" in block
    assert "from_choice=True" in block
    assert "alternative_exists" in block and "alt_button" in block
    assert "skip_log" in block, "one pilot question must log one row"
    assert block.find("disambiguate.applies_here") < block.find("disambiguate.default_question("), \
        "the heliport rule must run before the default is answered"
    assert 'kb["inline_keyboard"].append([alt_button])' in src
    cb = inspect.getsource(main.handle_feedback)
    assert 'data.startswith("alt:")' in cb
    assert "disambiguate.from_callback(" in cb
    assert 'log_as=f"[alt:{family}]' in cb, "taps must be countable in the log"
    assert 'data.startswith("amb:")' in cb, "old ask-first buttons in chats still work"


def test_old_ask_first_buttons_still_resolve():
    import disambiguate as d
    p = {"kind": d.KIND, "qid": "q9",
         "options": [{"label": "A", "question": "qa"}, {"label": "B", "question": "qb"}]}
    assert d.chosen_question(p, "1", "q9") == "qb"
    assert d.chosen_question(p, "1", "OLD") is None
    assert d.chosen_question({}, "0", "q9") is None
