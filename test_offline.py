"""
test_offline.py — fast unit tests for the deterministic, safety-critical parts.
No network, no API calls. Run with:  pytest test_offline.py

These cover the pieces where a bug is dangerous or silent: the ICAO resolver
(wrong-airport guard), reply formatting (citation + AIRAC + disclaimer always
present), and Telegram message splitting.
"""
import os

# Dummy env so the modules import without real credentials. No network happens
# at import time (the Supabase/OpenAI clients are constructed lazily).
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

import config                       # noqa: E402
import resolver                     # noqa: E402
from models import AIPResult, Resolution, SearchOutcome  # noqa: E402
from responder import answer, split_for_telegram          # noqa: E402
from schemas import AIPQueryExtraction                     # noqa: E402


def _ex(**kw):
    base = dict(intent="frequency_retrieval", filter_part="AD")
    base.update(kw)
    return AIPQueryExtraction(**base)


def _seed_index():
    """Hand-seed the resolver alias index so we don't rebuild from the full table."""
    resolver._ALIASES.clear()
    resolver._ALIASES.extend([
        ("lagos", "DNMM"), ("murtala muhammed", "DNMM"),
        ("abuja", "DNAA"), ("nnamdi azikiwe", "DNAA"),
        ("kano", "DNKN"),
        ("port harcourt", "DNPO"), ("ph", "DNPO"),
        ("delta", "DNMM"), ("delta", "DNAA"),   # deliberately ambiguous
    ])
    resolver._LABELS.clear()
    resolver._LABELS.update({"DNMM": "Lagos", "DNAA": "Abuja", "DNKN": "Kano",
                             "DNPO": "Port Harcourt"})
    resolver._loaded = True


# --- resolver: the wrong-airport guard -------------------------------------

def test_name_resolves_to_single_icao():
    _seed_index()
    assert resolver.resolve(_ex(aerodrome_name="Lagos")).icao == "DNMM"


def test_explicit_dn_code_used():
    _seed_index()
    r = resolver.resolve(_ex(icao_code="DNAA"))
    assert r.icao == "DNAA" and r.part == "AD"


def test_unknown_dn_code_is_refused_not_guessed():
    _seed_index()
    r = resolver.resolve(_ex(icao_code="DNZZ"))
    assert r.unresolved and r.icao is None


def test_unknown_name_is_refused():
    _seed_index()
    r = resolver.resolve(_ex(aerodrome_name="Heathrow"))
    assert r.unresolved and r.icao is None


def test_ambiguous_name_asks_instead_of_guessing():
    _seed_index()
    r = resolver.resolve(_ex(aerodrome_name="delta"))
    assert r.ambiguous == ["DNAA", "DNMM"] and r.icao is None


def test_no_aerodrome_with_national_intent_routes_national():
    _seed_index()
    r = resolver.resolve(_ex(intent="national_lookup"))
    assert r.is_national and r.icao is None and r.reference == "NATIONAL"


def test_airspace_intent_routes_enr_airspace():
    _seed_index()
    r = resolver.resolve(_ex(intent="airspace_lookup", aerodrome_name="Kano"))
    # airspace ignores the aerodrome name and targets the AIRSPACE tag
    assert r.is_national and r.part == "ENR" and r.reference == "AIRSPACE"
    assert r.icao is None


def test_out_of_scope_indicator_is_explained_not_searched():
    _seed_index()
    r = resolver.resolve(_ex(icao_code="DNBI"))   # Bida — registered, unpublished
    assert r.unresolved and "Bida" in r.reason


def test_fir_code_routes_enroute():
    _seed_index()
    r = resolver.resolve(_ex(icao_code="DNKK"))
    assert r.is_national and r.reference == "DNKK" and r.part == "ENR"


def test_ad_intent_without_aerodrome_asks():
    _seed_index()
    r = resolver.resolve(_ex(intent="frequency_retrieval"))
    assert r.unresolved and "Which aerodrome" in r.reason


# --- responder: currency + citation + disclaimer always present ------------

def test_answer_has_citation_currency_and_disclaimer():
    outcome = SearchOutcome(
        results=[AIPResult(content="TWR 118.100 MHz", similarity=0.87,
                           aip_section="AD 2", reference_tag="DNAA")],
        max_similarity=0.87, used_part="AD", used_reference="DNAA",
        abstained=False,
    )
    out = answer(outcome, Resolution(icao="DNAA", label="Abuja/Nnamdi Azikiwe"))
    assert "AD 2 / DNAA" in out          # per-chunk citation
    assert "87% match" in out            # confidence shown
    assert config.AIRAC_CYCLE in out     # currency stamp
    assert "Reference aid only" in out   # disclaimer
    assert "118.100" in out              # value shown verbatim


# --- telegram: long messages are split under the 4096 limit ----------------

def test_short_message_not_split():
    assert split_for_telegram("hello") == ["hello"]


def test_long_message_split_within_limit():
    big = ("paragraph\n\n" * 1000).strip()
    parts = split_for_telegram(big)
    assert len(parts) > 1
    assert all(len(p) <= 4096 for p in parts)


# --- query enrichment: PH -> Port Harcourt + airspace terms ----------------

def test_ph_expands_to_port_harcourt_in_airspace_query():
    _seed_index()
    res = resolver.resolve(_ex(intent="airspace_lookup", aerodrome_name="PH approach"))
    # Search stays on AIRSPACE — NOT pinned to the aerodrome's AD section.
    assert res.is_national and res.reference == "AIRSPACE" and res.icao is None
    # ...but the canonical name is carried for the embedding (the longer-term fix).
    assert res.aerodrome_hint == "Port Harcourt"


def test_build_search_text_enriches_airspace_query():
    _seed_index()
    ex = _ex(intent="airspace_lookup", aerodrome_name="PH approach")
    res = resolver.resolve(ex)
    st = resolver.build_search_text(ex, res, "lateral limits of PH approach")
    assert "Port Harcourt" in st                    # PH expanded
    assert "TMA" in st                              # airspace terminology added
    assert "lateral limits of PH approach" in st    # original preserved


def test_build_search_text_leaves_ad_query_minimal():
    _seed_index()
    ex = _ex(intent="frequency_retrieval", aerodrome_name="Lagos")
    res = resolver.resolve(ex)
    st = resolver.build_search_text(ex, res, "Lagos tower frequency")
    assert st.startswith("Lagos")                   # canonical name prepended
    assert "TMA" not in st                          # no airspace terms on AD intents


# --- VOR idents resolve to the right aerodrome (from AD 2.19) ---------------

def test_vor_idents_resolve_via_real_index():
    resolver.load_index(force=True)                 # build the full alias index
    assert resolver.resolve(_ex(aerodrome_name="POT")).icao == "DNPO"
    assert resolver.resolve(_ex(aerodrome_name="LAG")).icao == "DNMM"
    assert resolver.resolve(_ex(aerodrome_name="KAN")).icao == "DNKN"
    # works inside a natural phrase too
    assert resolver.resolve(_ex(aerodrome_name="POT VOR frequency")).icao == "DNPO"
    resolver._loaded = False                        # let other tests re-seed cleanly


# --- routing backstops added in the fix patch (deterministic, regression-guarded)

def test_runway_inventory_routes_to_runway_data():
    """'how many runways in Lagos' must be a runway-data query, not out_of_scope."""
    _seed_index()
    import agent
    resolver.VALID_ICAO.add("DNMM")
    ex = agent._backstop(_ex(intent="out_of_scope", aerodrome_name="Lagos"),
                         "How many runways are there in Lagos")
    assert ex.intent == "runway_data", ex.intent


def test_followup_not_treated_as_greeting():
    """A follow-up wrongly tagged greeting flows on (so context carry can resolve)."""
    _seed_index()
    import agent
    ex = agent._backstop(_ex(intent="general_greeting"), "Can you list them?")
    assert ex.intent != "general_greeting", ex.intent


def test_real_greeting_stays_greeting():
    """An actual greeting is still a greeting."""
    _seed_index()
    import agent
    ex = agent._backstop(_ex(intent="general_greeting"), "Hi")
    assert ex.intent == "general_greeting", ex.intent


# --- carry-poisoning guard: a named-but-unresolved place must NOT borrow the
#     last aerodrome (the "Jalingo -> answered for Asaba" bug).

def test_named_place_blocks_followup_carry():
    import main
    # "approach plate for Jalingo" -> extractor sets aerodrome_name, no icao
    assert main._names_a_place(_ex(aerodrome_name="Jalingo")) is True
    assert main._names_a_place(_ex(icao_code="DNXX")) is True


def test_bare_followup_allows_carry():
    import main
    # "what about the ILS?" -> no place named -> carry may fire
    assert main._names_a_place(_ex()) is False


# --- approach-procedure requests must route to the chart/approach flow, NOT
#     general synthesis (the DNBK holding/letdown safety bug).

def test_approach_procedures_route_to_chart():
    _seed_index()
    import agent
    resolver.VALID_ICAO.add("DNBK")
    ex = agent._backstop(_ex(intent="procedure_lookup", aerodrome_name="Birnin Kebbi"),
                         "what are the holding and letdown procedures for DNBK approach")
    assert ex.intent == "chart_retrieval", ex.intent


def test_frequency_query_not_rerouted_to_chart():
    _seed_index()
    import agent
    ex = agent._backstop(_ex(intent="frequency_retrieval", aerodrome_name="Lagos"),
                         "lagos tower frequency")
    assert ex.intent == "frequency_retrieval", ex.intent
