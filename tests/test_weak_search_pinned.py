"""D30: a weak similarity search no longer stops a question whose aerodrome is
a pinned ICAO and whose AD 2 subsection the model named — the decision never
depended on the search results. Everything else still stops at the gate."""
import inspect
from types import SimpleNamespace

import main
from models import Resolution, SearchOutcome

WEAK = SearchOutcome(results=[], max_similarity=0.385, abstained=True, reason="low_confidence")
UMUERI = Resolution(icao="DNAN", label="Umueri", part="AD", reference="DNAN")


def _ex(sub):
    return SimpleNamespace(intent="aerodrome_fact", aerodrome_name="Umueri", icao_code=None,
                           ad2_subsection=sub, procedure_type=None, runway=None)


def _decide(q, sub, res=UMUERI, stored=("AD 2.2", "AD 2.4", "AD 2.6", "AD 2.18", "AD 2.19",
                                         "AD 2.13", "AD 2.22"), monkeypatch=None):
    monkeypatch.setattr(main, "get_subsection_text",
                        lambda icao, sec: "text" if sec in stored else "")
    return main._decide_past_weak_search(q, WEAK, _ex(sub), res)


def test_named_subsection_at_a_pinned_aerodrome_gets_past(monkeypatch):
    assert _decide("What is the fire fighting category at Umueri?", "2.6",
                   monkeypatch=monkeypatch) == ("subsection", "AD 2.6")
    assert _decide("umueri aerodrome reference point", "2.2",
                   monkeypatch=monkeypatch) == ("subsection", "AD 2.2")
    assert _decide("What fuel types are available at Umueri?", "2.4",
                   monkeypatch=monkeypatch) == ("subsection", "AD 2.4")
    assert _decide("Umueri tower frequency", "2.18", monkeypatch=monkeypatch) == ("comms", None)


def test_everything_else_still_stops_at_the_gate(monkeypatch):
    q = "What is the fire fighting category at Umueri?"
    assert _decide(q, None, monkeypatch=monkeypatch) is None             # model named no section
    national = Resolution(icao=None, label="Nigeria", part="ENR", is_national=True)
    assert _decide(q, "2.6", res=national, monkeypatch=monkeypatch) is None
    assert _decide(q, "2.6", stored=(), monkeypatch=monkeypatch) is None  # nothing stored
    # Statuses whose fallback can show the retrieved chunks keep the refusal.
    assert _decide("declared distances at Umueri", "2.13", monkeypatch=monkeypatch) is None
    assert _decide("ILS approach procedure for Umueri", "2.22", monkeypatch=monkeypatch) is None


def test_the_gate_consults_it_before_refusing():
    src = inspect.getsource(main.process)
    i = src.index("_decide_past_weak_search, follow_query, outcome, ex, res")
    assert i < src.index('rec["path"] = "low_confidence"')
    assert 'outcome.reason == "low_confidence" and pinned is None' in src
