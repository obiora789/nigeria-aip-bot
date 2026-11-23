"""
test_overview.py — a bare aerodrome name gets the same summary card everywhere.

Reported live 2026-11-21: "Kebbi" -> AD 2.11, "Lagos" -> AD 2.1, "Sokoto" ->
AD 2.17, "Kano" -> AD 2.1. With nothing asked, the facts search returned
whichever entries happened to contain the name. Now the model says the
message only names an aerodrome (schemas.aerodrome_overview) and every
aerodrome gets the same card — AD 2.2 reference point and elevation, AD 2.12
runways, AD 2.18 frequencies — with topic buttons.

No network. Run with:  pytest tests/test_overview.py -q
"""
import asyncio
import os

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

from types import SimpleNamespace as N  # noqa: E402

KANO_22 = [{"arp_site_description": "120249N 0083126E  Centre of RWY 06/24",
            "elevation_m": 476.0, "elevation_ft": 1562.0}]
KANO_RWY = [{"designation": "06/24", "length_m": 3300.0, "width_m": 60.0},
            {"designation": "05/23", "length_m": 2600.0, "width_m": 45.0}]
KANO_COMMS = [{"service": "TWR", "frequencies": [{"value": "118.1", "unit": "MHZ"}]},
              {"service": "APP", "frequencies": [{"value": "119.9", "unit": "MHZ"},
                                                 {"value": "124.0", "unit": "MHZ"}]}]


def _sections(card):
    return [ln for ln in card.splitlines() if ln.endswith(")") and "(AD 2." in ln]


# ---------------------------------------------------------------- the card
def test_every_aerodrome_gets_the_same_sections_in_the_same_order():
    import responder
    full = responder.overview_reply(N(label="Kano", icao="DNKN"), KANO_22, KANO_RWY, KANO_COMMS)
    empty = responder.overview_reply(N(label="Gombe", icao="DNGO"), [], [], [])
    assert _sections(full) == _sections(empty) == [
        "Aerodrome (AD 2.2)", "Runways (AD 2.12)", "ATS communications (AD 2.18)"]


def test_the_card_shows_the_stored_values_as_stored():
    import responder
    card = responder.overview_reply(N(label="Kano", icao="DNKN"), KANO_22, KANO_RWY, KANO_COMMS)
    assert card.startswith("Kano — DNKN")
    assert "Reference point: 120249N 0083126E  Centre of RWY 06/24" in card
    assert "Elevation: 476 m / 1562 ft" in card
    assert "RWY 06/24 — 3300 x 60 m" in card and "RWY 05/23 — 2600 x 45 m" in card
    assert "TWR: 118.1 MHZ" in card and "APP: 119.9 MHZ, 124.0 MHZ" in card
    assert "Source: Nigeria AIP · AD 2.2, 2.12, 2.18" in card


def test_a_missing_section_says_so_and_never_borrows():
    import responder
    card = responder.overview_reply(N(label="Gombe", icao="DNGO"), [], [], [])
    for sub in ("AD 2.2", "AD 2.12", "AD 2.18"):
        assert f"Not in my stored copy — see {sub}." in card


def test_numbers_lose_only_a_float_zero_never_digits():
    import responder
    assert responder._plain_number(199.0) == "199"
    assert responder._plain_number(893.1) == "893.1"
    assert responder._plain_number("1562") == "1562"


# ------------------------------------------------------- what main.py sends
class _Main:
    def __init__(self, charts, heliport=False):
        import main, disambiguate
        self.main, self.dis = main, disambiguate
        self.sent = []
        self.saved = (main.get_structured, main.get_runway_physical_data,
                      main.get_charts_smart, main.send_message, disambiguate.is_heliport)
        main.get_structured = lambda icao, sub: KANO_22 if sub == "2.2" else KANO_COMMS
        main.get_runway_physical_data = lambda icao: KANO_RWY
        main.get_charts_smart = lambda icao, t="", r="": charts
        disambiguate.is_heliport = lambda icao: heliport

        async def send_message(chat_id, text, reply_markup=None):
            self.sent.append((text, reply_markup))
        main.send_message = send_message

    def topics(self):
        asyncio.run(self.main._send_overview(1, N(label="Kano", icao="DNKN"), "q1"))
        kb = self.sent[-1][1]["inline_keyboard"]
        return [b["callback_data"].split(":")[1] for row in kb for b in row
                if b["callback_data"].startswith("ov:")], kb

    def close(self):
        (self.main.get_structured, self.main.get_runway_physical_data,
         self.main.get_charts_smart, self.main.send_message, self.dis.is_heliport) = self.saved


def test_only_topics_the_aerodrome_has_are_offered():
    from models import ChartRef
    with_all = [ChartRef(url="a", procedure_type="ILS Approach Chart", runway="06"),
                ChartRef(url="b", procedure_type="SID Chart"),
                ChartRef(url="c", procedure_type="Aerodrome Chart")]
    m = _Main(with_all)
    try:
        topics, kb = m.topics()
        assert topics == ["rwy", "freq", "app", "sid", "adc", "met", "hrs"]
        assert kb[-1][0]["callback_data"] == "fb:up:q1", "feedback stays under the card"
    finally:
        m.close()
    m = _Main([ChartRef(url="c", procedure_type="Aerodrome Chart")])
    try:
        assert m.topics()[0] == ["rwy", "freq", "adc", "met", "hrs"]
    finally:
        m.close()


def test_a_heliport_is_never_offered_approaches():
    from models import ChartRef
    m = _Main([ChartRef(url="a", procedure_type="Approach Chart")], heliport=True)
    try:
        assert "app" not in m.topics()[0]
    finally:
        m.close()


def test_every_topic_button_fits_telegram_and_names_a_real_question():
    import main, resolver, telegram
    for icao in resolver.VALID_ICAO:
        kb = telegram.overview_kb(icao, list(main.OVERVIEW_TOPICS))
        for row in kb["inline_keyboard"]:
            for b in row:
                assert len(b["callback_data"].encode()) <= 64, b
                assert b["callback_data"].split(":")[1] in main.OVERVIEW_TOPICS


def test_a_topic_tap_runs_its_question_for_that_aerodrome():
    import main
    calls, saved = [], main.process

    async def spy(chat_id, text, **kw):
        calls.append((text, kw))
    main.process = spy
    try:
        asyncio.run(main.handle_feedback({"id": None, "data": "ov:rwy:DNKN",
                                          "from": {"id": 1}}))
        assert calls == [("Runway physical characteristics at DNKN",
                          {"from_choice": True,
                           "log_as": "[overview:rwy] Runway physical characteristics at DNKN"})]
    finally:
        main.process = saved


def test_a_forged_topic_or_icao_is_refused():
    import main
    calls, sent, saved = [], [], (main.process, main.send_message)

    async def spy(chat_id, text, **kw):
        calls.append(text)

    async def send_message(chat_id, text, reply_markup=None):
        sent.append(text)
    main.process, main.send_message = spy, send_message
    try:
        for data in ("ov:drop:DNKN", "ov:rwy:XXXX", "ov:rwy"):
            asyncio.run(main.handle_feedback({"id": None, "data": data, "from": {"id": 1}}))
        assert not calls and all("no longer valid" in t for t in sent)
    finally:
        main.process, main.send_message = saved


# ------------------------------------------------------- the model decides
def test_the_prompt_and_schema_describe_a_bare_name():
    import agent, schemas
    assert "aerodrome_overview=true" in agent._SYSTEM
    assert "aerodrome_overview" in schemas.AIPQueryExtraction.model_fields
    assert "aerodrome_overview" in agent._REQUIRED_FIELDS


def test_a_bare_code_is_described_as_a_card_not_a_mapping():
    # "DNAA" on its own got the one-line mapping reply in 19-22 of 40 runs;
    # the prompt now says a code typed alone asks nothing. The model decides.
    import agent, schemas
    assert "'DNAA'" in agent._SYSTEM.split("- icao_lookup:")[1].split("- airspace_lookup")[0]
    assert "NOT icao_lookup" in agent._SYSTEM
    assert "'DNAA'" in schemas.AIPQueryExtraction.model_fields["aerodrome_overview"].description


def test_the_structured_read_fails_loudly():
    import database, httpx

    class Down:
        def __getattr__(self, n):
            return lambda *a, **k: self

        def execute(self):
            raise httpx.ConnectError("down")
    saved = (database.supabase, database._RETRY_PAUSES)
    database.supabase, database._RETRY_PAUSES = Down(), (0, 0, 0)
    try:
        database.get_structured("DNKN", "2.2")
        raise AssertionError("returned instead of raising")
    except database.DataUnavailable:
        pass
    finally:
        database.supabase, database._RETRY_PAUSES = saved


def test_every_published_aerodrome_is_named_to_the_model_as_nigerian():
    # gpt-6-luna refused Bonny/Forcados/Escravos as out of scope and did not
    # know Bebi, Soku, PHSIA or Gbaran Ubie. The prompt now lists all 40 from
    # resolver.AERODROMES (data, not a wording rule), before the ICAO rule.
    import agent, resolver
    roster = agent._SYSTEM.split("THE 40 AERODROMES THIS AIP PUBLISHES")[1].split("ICAO: only set")[0]
    for icao in resolver.AERODROMES:
        assert resolver.aerodrome_full_name(icao) in roster, icao
    assert not any(c in roster for c in resolver.AERODROMES)   # names only: never infer a code
    assert "never meets (c)" in roster
