"""
test_sids.py — SIDs work like approaches (Obiora, 2026-11-21).

Reported live: "SID Lagos" dumped several AD 2.22 coding tables at once, one of
them ERSED 1M — a STAR for RWY 18L, not a SID. Now: the model says the
question is about a SID (schemas.sid_request), Vannie asks the runway, then
the SID, from the aerodrome's own AD 2.22 headings, and sends that one SID.

The fixtures are real lines from the stored AD 2.22 text (ad222_by_icao.json),
trimmed. No network. Run with:  pytest test_sids.py -q
"""
import asyncio
import os

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

from types import SimpleNamespace as N  # noqa: E402

LAGOS = """2.22.7 PBN PROCEDURE
2.22.7.1 RNAV (GNSS) RWY 18 / 36
2.22.7.1.3 ILLUSTRATIVE CODING STAR RW18L
2.22.7.1.3.1 GUMOM 1M (GUMO1M)
2.22.7.1.3.2 ERSED 1M (ERSE1M)
2.22.7.1.3.5 Description of PBN Procedures
2.22.7.1.4 ILLUSTRATIVE CODING SID RW18R
2.22.7.1.4.1 IBA 1Q (IBA1Q)
 070 TF OVNIK - 211
(209.0)
 -2 15.0 - 3500 230 - RNAV 1
2.22.7.1.4.2 PISBA 1Q (PISB1Q)
2.22.7.1.4.8 Description of PBN Procedures
SID Formal Description Abbreviated Description
IBA 1Q Climb on course 181°M, at or above 465ft, continue on
course 181°M to MM800 at or above 1000ft, turn right on
track 271°M to MM801 max IAS 200kt, proceed on track
001°M to MM803 at or above FL70, track 091°M to LAG then
047°M to IBA
[M181,A465+] -MM800 [M181,A1000+] -
MM801[K200] -MM803 [FL70+] - LAG-IBA
PISBA 1Q Climb on course 181°M, at or above 465ft, continue on
course 181°M to MM800 at or above 1000ft, turn right on
30 OCT 25
AIRAC AMDT 02/2025
AD 2-DNMM-21
2.22.7.1.5 ILLUSTRATIVE CODING SID RW18L
2.22.7.1.5.1 IBA 1S (IBA1S)
2.22.7.1.5.3 BUDSI 1S (BUDS 1S)
2.22.7.1.5.6 Description of PBN Procedures
BUDSI 1S Climb on course 181°M, at or above 502ft, continue on course
181°M to MM810 at or above 1000ft, turn left on track 091°M to
MM811 max IAS 200kt, then on track 001°M to MM812, at or above
FL65, continue on track 001°M to BUDSI
[M181,A502+]-- MM810 [M181,A1000+] -
MM811[K200] - MM812 -[FL65+] - BUD￾SI
2.22.7.1.6 ILLUSTRATIVE CODING RNP APCH RW18R
"""

# Port Harcourt publishes AKDIT 2S under "SID RW21", but its own description
# climbs on 029°M — RWY 03. It must not be offered behind a RWY 21 button.
PORT_HARCOURT = """2.22.7.1.3 ILLUSTRATIVE CODING SID RW21
2.22.7.1.3.1 AKDIT 1S (AKDI1S)
2.22.7.1.3.2 AKDIT 2S (AKDI2S)
2.22.7.1.3.5 Description of PBN Procedures
AKDIT 1S Climb on course 209°M, at or above 1600ft, turn right course 230°M to AKDIT
AKDIT 2S Climb on course 029°M, at or above 3200ft, turn left track 296°M to AKDIT. [M029, A3200+ L] -- AKDIT [M296]
2.22.7.1.4 ILLUSTRATIVE CODING RNP RWY 03
"""


# --------------------------------------------------------- the catalogue
def test_the_runway_and_sid_choices_come_from_the_published_headings():
    import sids
    cat = sids.catalogue(LAGOS)
    assert [(s.runway, s.name) for s in cat] == [
        ("18R", "IBA 1Q"), ("18R", "PISBA 1Q"), ("18L", "IBA 1S"), ("18L", "BUDSI 1S")]


def test_a_star_is_never_offered_as_a_sid():
    """The live reply listed ERSED 1M under 'SID Lagos'. It is a STAR."""
    import sids
    names = {s.name for s in sids.catalogue(LAGOS)}
    assert "ERSED 1M" not in names and "GUMOM 1M" not in names


def test_a_sid_contradicting_its_runway_heading_is_left_out():
    import sids
    left_out = []
    cat = sids.catalogue(PORT_HARCOURT, left_out)
    assert [s.name for s in cat] == ["AKDIT 1S"]
    assert [s.name for s in left_out] == ["AKDIT 2S"]


def test_no_sid_headings_means_no_catalogue_not_a_claim():
    import sids
    assert sids.catalogue("2.22.3.2 Departure procedures\nClimb straight ahead") == []
    assert sids.decide([]).action == "none"


# ----------------------------------------------------------- the choice
def test_runway_first_then_the_sid_like_approaches():
    import sids
    cat = sids.catalogue(LAGOS)
    d = sids.decide(cat)
    assert d.action == "ask_runway" and d.options == ["18L", "18R"]
    d = sids.decide(cat, runway="18R")
    assert d.action == "ask_sid" and d.options == ["IBA 1Q", "PISBA 1Q"] and d.runway == "18R"
    d = sids.decide(cat, runway="18R", name="PISBA 1Q")
    assert d.action == "send" and d.sids[0].name == "PISBA 1Q"


def test_a_named_sid_is_sent_without_asking():
    import sids
    cat = sids.catalogue(LAGOS)
    for asked in ("IBA 1Q", "iba1q", "IBA1Q"):
        d = sids.decide(cat, name=asked)
        assert d.action == "send" and d.sids[0].name == "IBA 1Q", asked


def test_a_fix_shared_by_two_runways_asks_the_runway():
    import sids
    d = sids.decide(sids.catalogue(LAGOS), name="IBA")
    assert d.action == "ask_runway" and d.options == ["18L", "18R"]


def test_a_runway_without_sids_says_so_and_offers_the_real_ones():
    import sids
    d = sids.decide(sids.catalogue(LAGOS), runway="36")
    assert d.action == "no_runway" and d.options == ["18L", "18R"] and d.runway == "36"


# ------------------------------------------------------ what is shown
def test_a_complete_description_is_shown_verbatim():
    import sids
    iba = sids.decide(sids.catalogue(LAGOS), name="IBA 1Q").sids[0]
    text = sids.description(LAGOS, iba)
    assert text.startswith("IBA 1Q Climb on course 181°M, at or above 465ft")
    assert text.rstrip().endswith("LAG-IBA") and "PISBA" not in text


def test_a_description_cut_by_a_page_break_is_not_shown():
    """PISBA 1Q's row stops at '30 OCT 25' before reaching PISBA."""
    import sids
    pisba = sids.decide(sids.catalogue(LAGOS), name="PISBA 1Q").sids[0]
    assert sids.description(LAGOS, pisba) is None


def test_an_invisible_hyphen_does_not_hide_a_complete_description():
    import sids
    budsi = sids.decide(sids.catalogue(LAGOS), name="BUDSI 1S").sids[0]
    text = sids.description(LAGOS, budsi)
    assert text and text.endswith("BUDSI") and "￾" not in text


def test_a_sid_with_no_description_row_gets_none():
    import sids
    iba_s = sids.decide(sids.catalogue(LAGOS), name="IBA 1S").sids[0]
    assert sids.description(LAGOS, iba_s) is None


def test_the_reply_never_shows_a_coding_table_and_points_to_the_chart():
    import responder, sids
    res = N(label="Lagos", icao="DNMM")
    pisba = sids.decide(sids.catalogue(LAGOS), name="PISBA 1Q").sids[0]
    out = responder.sid_reply(res, pisba, None)
    assert "PISBA 1Q (PISB1Q) — SID from Lagos RWY 18R" in out
    assert "SID chart" in out and "070 TF" not in out
    iba = sids.decide(sids.catalogue(LAGOS), name="IBA 1Q").sids[0]
    out = responder.sid_reply(res, iba, sids.description(LAGOS, iba))
    assert "verbatim from AD 2.22" in out and "LAG-IBA" in out


# ------------------------------------------------------ the model decides
def _bs(raw, **kw):
    import agent, resolver
    from schemas import AIPQueryExtraction
    resolver.load_index()
    base = dict(intent="procedure_lookup", filter_part="AD")
    base.update(kw)
    return agent._backstop(AIPQueryExtraction(**base), raw)


def test_the_models_sid_answer_goes_to_the_sid_choice():
    for q in ("SID Lagos", "what are the departures from Lagos", "IBA 1Q departure Lagos"):
        ex = _bs(q, sid_request=True, aerodrome_name="Lagos")
        assert ex.intent == "chart_retrieval" and ex.procedure_type == "SID", q


def test_without_the_models_sid_answer_nothing_changes():
    ex = _bs("Lagos departure frequency", intent="frequency_retrieval",
             aerodrome_name="Lagos")
    assert ex.intent == "frequency_retrieval"


def test_an_approach_answer_wins_over_a_sid_answer():
    ex = _bs("approach procedures Lagos", sid_request=True,
             approach_request="procedure", aerodrome_name="Lagos")
    assert ex.procedure_type is None, "the approach flow asks the type"


def test_the_prompt_and_schema_describe_sid_requests():
    import agent, schemas
    assert "with sid_request=true: 'SID Lagos'" in agent._SYSTEM
    assert "and its SIDs are NOT procedure_lookup" in agent._SYSTEM
    f = schemas.AIPQueryExtraction.model_fields
    assert "sid_request" in f and "sid_name" in f
    assert {"sid_request", "sid_name"} <= agent._REQUIRED_FIELDS


# ------------------------------------------ the flow main.py runs (stubbed)
class _Flow:
    """Run main._run_sid_decision against the fixture, recording what the
    pilot would see. No network."""

    def __init__(self, text=LAGOS, charts=None):
        import main, memory
        self.main, self.memory = main, memory
        self.sent, self.info, self.charts, self.pending = [], [], [], []
        self.saved = (main.get_section_text, main.get_charts_smart, main.send_message,
                      main.send_charts, memory.save_sid_pending)
        main.get_section_text = lambda icao, sec="AD 2.22": text
        main.get_charts_smart = lambda icao, t="", r="": charts or []

        async def send_message(chat_id, t, reply_markup=None):
            self.sent.append((t, reply_markup))

        async def send_charts(chat_id, cs, requested_runway=None):
            self.charts.extend(cs)
        main.send_message, main.send_charts = send_message, send_charts
        memory.save_sid_pending = lambda *a, **k: self.pending.append(a)

    def run(self, runway="", name=""):
        async def info(t):
            self.info.append(t)
        return asyncio.run(self.main._run_sid_decision(
            1, N(icao="DNMM", label="Lagos"), "DNMM", runway, name, info))

    def close(self):
        (self.main.get_section_text, self.main.get_charts_smart, self.main.send_message,
         self.main.send_charts, self.memory.save_sid_pending) = self.saved


def test_the_flow_asks_runway_then_sid_then_sends_one():
    from models import ChartRef
    charts = [ChartRef(url="u1", procedure_type="SID Chart", icao_code="DNMM"),
              ChartRef(url="u2", procedure_type="ILS Approach Chart", runway="18R",
                       icao_code="DNMM")]
    f = _Flow(charts=charts)
    try:
        assert f.run() is True
        text, kb = f.sent[-1]
        assert "which runway" in text
        assert [b["callback_data"].split(":")[2] for row in kb["inline_keyboard"]
                for b in row] == ["18L", "18R"]
        assert f.run(runway="18R") is True
        text, kb = f.sent[-1]
        assert "RWY 18R — which one" in text
        assert [b["text"] for row in kb["inline_keyboard"] for b in row] == ["IBA 1Q", "PISBA 1Q"]
        assert f.run(runway="18R", name="IBA 1Q") is True
        assert "IBA 1Q (IBA1Q) — SID from Lagos RWY 18R" in f.info[0]
        assert [c.url for c in f.charts] == ["u1"], "only SID chart pages"
    finally:
        f.close()


def test_an_aerodrome_without_sid_headings_keeps_the_old_behaviour():
    f = _Flow(text="2.22.3.2 Departure procedures\nClimb straight ahead")
    try:
        assert f.run() is False and not f.sent and not f.info
    finally:
        f.close()


def test_a_button_tap_continues_the_choice():
    import main, memory
    f = _Flow()
    saved_load = memory.load
    memory.load = lambda chat_id: {"pending": {"kind": "sid_clar", "icao": "DNMM",
                                               "label": "Lagos", "runway": "", "name": "",
                                               "qid": "q1", "options": ["18L", "18R"]}}
    saved_log = main.observability.log_query
    main.observability.log_query = lambda **k: None
    try:
        asyncio.run(main.handle_feedback({"id": None, "data": "sid:rwy:18L:q1",
                                          "from": {"id": 1}}))
        text, kb = f.sent[-1]
        assert "RWY 18L — which one" in text
        assert [b["text"] for row in kb["inline_keyboard"] for b in row] == ["IBA 1S", "BUDSI 1S"]
    finally:
        memory.load, main.observability.log_query = saved_load, saved_log
        f.close()


def test_the_sids_own_fix_never_replaces_the_aerodrome():
    """Measured 2026-11-21: 'PISBA 1Q SID at Lagos' became waypoint PISBA,
    and Lagos was dropped. PISBA is a published ENR point; here it is only
    the name of Lagos's SID."""
    import database, resolver
    saved = (resolver._PUBLISHED_ENTITIES, database.find_aip_scope)
    resolver._PUBLISHED_ENTITIES = {
        **{v.upper(): "ENR_NAVAID" for v in resolver.VOR_IDENTS.values()},
        "PISBA": "ENR_POINT", "IKSEM": "ENR_POINT"}
    database.find_aip_scope = lambda n: []
    try:
        for q, place, name in (("PISBA 1Q SID at Lagos", "Lagos", "PISBA 1Q"),
                               ("IBA 1Q departure Lagos", "Lagos", "IBA 1Q"),
                               ("IKSEM 1S departure from Abuja", "Abuja", "IKSEM 1S")):
            ex = _bs(q, sid_request=True, sid_name=name, aerodrome_name=place)
            assert ex.aerodrome_name == place, (q, ex.aerodrome_name)
            assert ex.intent == "chart_retrieval" and ex.procedure_type == "SID"
        # a waypoint question that is NOT a SID question still resolves to it
        ex = _bs("where is PISBA near Lagos", intent="airspace_lookup",
                 aerodrome_name="Lagos")
        assert ex.aerodrome_name == "PISBA"
    finally:
        resolver._PUBLISHED_ENTITIES, database.find_aip_scope = saved


def test_with_no_sid_chart_stored_the_reply_never_points_to_one():
    """Measured live 2026-11-21: 0 SID chart pages stored at every aerodrome.
    'See the SID chart' with no chart sent is no answer."""
    import responder, sids
    res = N(label="Kano", icao="DNKN")
    pisba = sids.decide(sids.catalogue(LAGOS), name="PISBA 1Q").sids[0]
    out = responder.sid_reply(res, pisba, None, chart_stored=False)
    assert "the SID chart in the official AIP (Kano AD 2.24)" in out
    assert "chart below" not in out
    iba = sids.decide(sids.catalogue(LAGOS), name="IBA 1Q").sids[0]
    out = responder.sid_reply(res, iba, sids.description(LAGOS, iba), chart_stored=False)
    assert "LAG-IBA" in out and "I don't have this SID's chart stored" in out
    out = responder.sid_reply(res, iba, sids.description(LAGOS, iba), chart_stored=True)
    assert "chart stored" not in out


def test_the_flow_says_so_when_no_sid_chart_is_stored():
    f = _Flow(charts=[])
    try:
        f.run(runway="18R", name="PISBA 1Q")
        assert "official AIP (Lagos AD 2.24)" in f.info[0] and not f.charts
        assert len(f.info) == 1, "no 'SID chart(s)' intro without charts"
    finally:
        f.close()


def test_the_sid_chart_for_the_chosen_runway_is_sent():
    from models import ChartRef
    charts = [ChartRef(url="s18L", procedure_type="SID Chart", runway="18L"),
              ChartRef(url="s18R", procedure_type="SID Chart", runway="18R")]
    f = _Flow(charts=charts)
    try:
        f.run(runway="18R", name="IBA 1Q")
        assert [c.url for c in f.charts] == ["s18R"]
        assert "SID chart for Lagos RWY 18R" in f.info[-1]
    finally:
        f.close()
