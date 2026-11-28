"""Car 4 — the answer layer (audit 19 Nov, D11-D15 + citations). Each test
failed on the code before the fix."""
import inspect
from types import SimpleNamespace

from models import AIPResult, Resolution, SearchOutcome

RES = Resolution(icao="DNKN", label="Kano")


def test_d11_focus_never_cuts_a_spaced_number():
    from responder import _focus
    out = _focus("X" * 50 + " TORA 12 500 m " + "pad " * 300, ["TORA"], width=60)
    assert "12 500" in out and "12 5 …" not in out


def test_d11_focus_ignores_case_and_matches_whole_tokens():
    from responder import _focus
    t = "Some header text. " * 30 + "TOWER 118.500 MHZ H24 " + "z " * 200
    assert "TOWER 118.500" in _focus(t, ["tower"], width=200)
    t = ("Obstacle height 2200 ft at the mast. " + "padding words " * 40
         + "RWY 22 THR elev 345 m " + "q " * 200)
    assert "RWY 22 THR elev 345 m" in _focus(t, ["22"], width=200)


def test_d11_focus_window_starts_on_a_whole_token():
    from responder import _focus
    t = "alpha " * 40 + "RWY 04 THR 1 234.5 m " + "beta " * 200
    out = _focus(t, ["1"], width=80)
    assert all(tok in ("…",) or tok in t.split() for tok in out.split())


def test_d11_answer_focuses_on_the_query_words_in_order():
    src = inspect.getsource(__import__("responder")._best_excerpt)
    assert "dict.fromkeys" in src and "list(want)" not in src


def test_d12_a_procedure_line_with_a_range_is_not_page_furniture():
    import procedures
    kept = procedures._clean("Descend to 2 500 FT between 10 and 15 DME ABC\n"
                             "27 NOV 25 AIRAC AMDT 03/2025\nTurn left")
    assert "between 10 and 15 DME ABC" in kept
    assert "27 NOV 25" not in kept


def _outcome(*contents):
    return SearchOutcome(results=[AIPResult(content=c, similarity=0.6, aip_section="AD 2.18",
                                            reference_tag="DNKN") for c in contents],
                         max_similarity=0.6, used_reference="DNKN")


def test_d13_not_stated_shows_the_closest_text_to_check():
    from responder import not_in_aip
    out = not_in_aip(RES, _outcome("DNKN TWR Kano Tower 118.600 MHz H24"), "kano tower frequency")
    assert "isn't stated in the AIP data I retrieved" in out
    assert "Closest AIP text I retrieved — check it yourself" in out
    assert "Kano Tower 118.600 MHz" in out
    assert "Closest" not in not_in_aip(RES)          # no outcome: as before


def test_d13_main_passes_the_outcome_to_not_in_aip():
    import main
    src = inspect.getsource(main.process)
    assert "not_in_aip(res, outcome, follow_query)" in src
    assert "not_in_aip(res, ad_out, follow_query)" in src
    assert "send_info(not_in_aip(res))" not in src


class _Q:
    def __init__(self, log):
        self.log = log
    def __getattr__(self, name):
        def call(*a, **k):
            self.log.append(name)
            return self
        return call
    def execute(self):
        return SimpleNamespace(data=[{"scope_id": "TEMSA"}])


def test_d15_scope_ids_are_read_in_a_stable_order(monkeypatch):
    import database
    calls = []
    monkeypatch.setattr(database, "supabase", SimpleNamespace(table=lambda *_: _Q(calls)))
    assert database.list_scope_ids("ENR_POINT") == ["TEMSA"]
    assert "order" in calls and calls.index("order") < calls.index("range")


def test_facts_cite_every_section_they_come_from():
    from responder import facts_reply
    enr = Resolution(icao=None, label="Nigeria", part="ENR", scope_kind="ENR_POINT",
                     scope_id="TEMSA")
    facts = [{"subsection": "4.1", "entity": "", "label": "VOR", "fact_value": "113.8"},
             {"subsection": "4.4", "entity": "", "label": "Point", "fact_value": "TEMSA"}]
    out = facts_reply(enr, facts)
    assert "ENR 4.1, ENR 4.4" in out and "ENR 2 " not in out and "ENR 2\n" not in out


def test_an_answer_from_two_excerpts_shows_both_sources():
    from responder import _source_block
    outcome = _outcome("RWY 06 TORA 3300 m", "RWY 24 TORA 3000 m")
    ans = SimpleNamespace(facts_used=[SimpleNamespace(value="3300", source_excerpt=1),
                                      SimpleNamespace(value="3000", source_excerpt=2)],
                          answer="", computation="")
    out = _source_block(outcome, ans)
    assert "RWY 06 TORA 3300 m" in out and "RWY 24 TORA 3000 m" in out
