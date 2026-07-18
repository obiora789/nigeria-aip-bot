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


# --- defense-in-depth: the synthesis path itself refuses approach procedures,
#     independently of routing (the second layer for the DNBK bug).

def test_synthesis_refuses_approach_procedures():
    import synthesize
    # guard returns BEFORE any LLM call, so this is offline-safe
    status, _ = synthesize.synthesize_decision(
        "what are the holding and letdown procedures for DNBK", [])
    assert status == "approach_procedure", status


def test_synthesis_procedure_guard_does_not_overfire():
    import synthesize
    # a normal factual question must not match the procedure guard
    assert synthesize._PROC_RE.search("lagos tower frequency") is None
    assert synthesize._PROC_RE.search("transition altitude at kano") is None


# --- navaid-value guard: never synthesize one navaid's value from a multi-navaid
#     AD 2.19 block (the DNMM VOR-distance misattribution).

def test_navaid_value_query_refuses_synthesis():
    import synthesize
    status, _ = synthesize.synthesize_decision(
        "distance from the VOR to threshold of rwy 18L in lagos", [])
    assert status == "navaid", status


def test_navaid_guard_does_not_overfire():
    import synthesize
    # normal single-value queries must NOT be caught (they still synthesize)
    for q in ("lagos tower frequency", "elevation of abuja",
              "declared distances for DNAA"):
        assert not (synthesize._NAVAID_RE.search(q)
                    and synthesize._NAVAID_VALUE_RE.search(q)), q


# --- declared distances: structured, exact, never misattributed (Lagos 18L!=18R)

def test_declared_distance_query_routes_structured():
    import synthesize
    for q in ("TORA for RWY 22 in abuja", "declared distances for lagos",
              "LDA for runway 18R"):
        status, _ = synthesize.synthesize_decision(q, [])
        assert status == "declared_distance", (q, status)


def test_declared_reply_per_runway_no_misattribution():
    import responder
    from models import Resolution
    res = Resolution(); res.label = "Lagos"; res.icao = "DNMM"
    recs = [{"runway": "18L", "tora": "2745", "toda": "2745", "asda": "2788", "lda": "2745"},
            {"runway": "18R", "tora": "3900", "toda": "3900", "asda": "4020", "lda": "3900"}]
    a = responder.declared_distance_reply(res, recs, "18L", "tora for 18L")
    b = responder.declared_distance_reply(res, recs, "18R", "tora for 18R")
    assert "TORA: 2745 m" in a and "TORA: 3900 m" in b, (a, b)


# --- comms guard (AD 2.18): never synthesize one ATS service's frequency

def test_comms_query_refuses_synthesis():
    import synthesize
    for q in ("lagos tower frequency", "ground frequency for abuja",
              "ATIS frequency at kano", "approach frequency for lagos"):
        status, _ = synthesize.synthesize_decision(q, [])
        assert status == "comms", (q, status)


def test_comms_guard_does_not_overfire():
    import synthesize
    # ILS freq -> navaid (not comms); TORA -> declared; normal -> synthesize
    assert synthesize.synthesize_decision("ILS frequency for abuja", [])[0] == "navaid"
    assert synthesize.synthesize_decision("TORA for rwy 22", [])[0] == "declared_distance"
    assert synthesize.synthesize_decision("elevation of abuja", [])[0] != "comms"


# --- AD 2.12 asymmetric-field guard: bearing / threshold elevation / threshold
#     coords differ per runway end; aerodrome elevation + length/width/PCN are safe.

def test_rwy_asymmetric_field_refuses_synthesis():
    import synthesize
    for q in ("true bearing of runway 04", "threshold elevation of RWY 22",
              "THR elevation rwy 04", "threshold coordinates for RWY 18L"):
        assert synthesize.synthesize_decision(q, [])[0] == "rwy_char", q


def test_rwy_char_guard_does_not_overfire():
    import synthesize
    # aerodrome elevation and symmetric fields must still synthesize
    for q in ("elevation of abuja", "aerodrome elevation lagos",
              "runway length DNAA", "width of runway 04", "PCN of runway 22"):
        assert synthesize.synthesize_decision(q, [])[0] != "rwy_char", q


# --- restriction/authorisation guard: a numbered/lettered list item (e.g.
#     "(3) At night") quoted without its governing clause ("unless authorised
#     by...") can reverse the rule's meaning, and the reply can cite the wrong
#     AD section for it (confirmed on a real DNMM night-flying query, cited as
#     AD 2.20 when the governing text was AD 2.22.5.1). Never synthesize these.

def test_restriction_query_refuses_synthesis():
    import synthesize
    for q in ("what's the night-flying ban at Lagos?", "is Kano under a curfew?",
              "are VFR flights restricted at night in Abuja?",
              "does Lagos require authorization for night flights?"):
        assert synthesize.synthesize_decision(q, [])[0] == "fallback", q


def test_restriction_guard_does_not_overfire():
    import synthesize
    # ordinary factual questions must still synthesize
    for q in ("elevation of abuja", "runway length DNAA", "lagos tower frequency"):
        assert synthesize._RESTRICTION_RE.search(q) is None, q


# --- verifier fix for the multi-entity misattribution class: every fact must
#     verify against the ONE excerpt it declares (source_excerpt), not a
#     flattened blob of every retrieved chunk — and an answer with zero
#     facts_used (which let a fabricated-or-cross-cited prose claim through
#     with no check at all) is now rejected outright.

def test_verify_rejects_answer_with_no_facts_used():
    import synthesize
    from schemas import GroundedAnswer
    ans = GroundedAnswer(answerable=True, answer="No pilot may operate a VFR "
                         "flight: (3) At night.", facts_used=[], computation="")
    ok, issues = synthesize.verify_grounded_answer(ans, [])
    assert ok is False
    assert any("facts_used" in i for i in issues), issues


def test_verify_rejects_cross_excerpt_number_bleed():
    import synthesize
    from schemas import GroundedAnswer, GroundedFact
    from models import AIPResult
    excerpt_a = AIPResult(content="RWY 04 Threshold elevation: 331 m",
                          similarity=0.7, aip_section="AD 2.12", reference_tag="DNAA")
    excerpt_b = AIPResult(content="RWY 22 Threshold elevation: 342 m",
                          similarity=0.65, aip_section="AD 2.12", reference_tag="DNAA")
    results = [excerpt_a, excerpt_b]
    # 331 is real, but it's in excerpt #1 — citing excerpt #2 for it must fail.
    bad = GroundedAnswer(
        answerable=True, answer="RWY 22 threshold elevation is 331 m.",
        facts_used=[GroundedFact(value="331 m", what="RWY 22 threshold elevation",
                                 source_excerpt=2)],
        computation="")
    ok, issues = synthesize.verify_grounded_answer(bad, results)
    assert ok is False, issues
    good = GroundedAnswer(
        answerable=True, answer="RWY 22 threshold elevation is 342 m.",
        facts_used=[GroundedFact(value="342 m", what="RWY 22 threshold elevation",
                                 source_excerpt=2)],
        computation="")
    ok2, issues2 = synthesize.verify_grounded_answer(good, results)
    assert ok2, issues2


def test_verify_allows_legitimate_multi_excerpt_computation():
    """B1-style runway comparison: two facts, each citing its OWN excerpt,
    feeding one computation, must still pass — the fix must not be so strict
    it breaks legitimate cross-fact arithmetic."""
    import synthesize
    from schemas import GroundedAnswer, GroundedFact
    from models import AIPResult
    rwy_a = AIPResult(content="RWY 18R/36L Dimensions (m): 3900 x 60",
                      similarity=0.65, aip_section="AD 2.12", reference_tag="DNMM")
    rwy_b = AIPResult(content="RWY 18L/36R Dimensions (m): 2745 x 45",
                      similarity=0.63, aip_section="AD 2.12", reference_tag="DNMM")
    results = [rwy_a, rwy_b]
    ans = GroundedAnswer(
        answerable=True,
        answer="RWY 18R/36L is 1155 m longer (3900 - 2745 = 1155) and 15 m "
               "wider (60 - 45 = 15).",
        facts_used=[
            GroundedFact(value="3900 x 60", what="RWY 18R/36L length x width",
                        source_excerpt=1),
            GroundedFact(value="2745 x 45", what="RWY 18L/36R length x width",
                        source_excerpt=2),
        ],
        computation="3900-2745=1155; 60-45=15")
    ok, issues = synthesize.verify_grounded_answer(ans, results)
    assert ok, issues
    # a third, never-cited number must still be rejected
    tampered = GroundedAnswer(
        answerable=True,
        answer="RWY 18R/36L is 1155 m longer and its PCN is 80.",
        facts_used=ans.facts_used, computation="3900-2745=1155")
    ok2, issues2 = synthesize.verify_grounded_answer(tampered, results)
    assert ok2 is False, issues2


def test_source_block_cites_declared_excerpt_not_reranked_guess():
    """responder._source_block must cite the excerpt the fact actually declared
    (source_excerpt), not a word-overlap re-guess made after generation — the
    mechanism that let a real DNMM answer get cited as AD 2.20 when its
    governing text was actually AD 2.22.5.1."""
    import responder
    from schemas import GroundedAnswer, GroundedFact
    from models import AIPResult, SearchOutcome
    excerpt_2_20 = AIPResult(
        content="DNMM AD 2.20 LOCAL AERODROME REGULATIONS 2.20.1 Airport "
                "regulations None, except where specified in Part 2 - ENR.",
        similarity=0.57, aip_section="AD 2.20", reference_tag="DNMM")
    excerpt_2_22 = AIPResult(
        content="2.22.5.1 VFR flights requiring ATC authorisation Unless "
                "authorised by the appropriate ATS authority, no pilot may "
                "operate a VFR flight: (1) Above FL 150; (2) At transonic and "
                "supersonic speeds; or (3) At night.",
        similarity=0.61, aip_section="AD 2.22", reference_tag="DNMM")
    outcome = SearchOutcome(results=[excerpt_2_20, excerpt_2_22], max_similarity=0.61)
    ans = GroundedAnswer(
        answerable=True,
        answer="Unless authorised by ATC, no VFR flight may operate above "
               "FL 150, at transonic/supersonic speed, or at night.",
        facts_used=[GroundedFact(
            value="Unless authorised by the appropriate ATS authority, no "
                  "pilot may operate a VFR flight: (1) Above FL 150; (2) At "
                  "transonic and supersonic speeds; or (3) At night.",
            what="VFR flight authorisation requirement (AD 2.22.5.1)",
            source_excerpt=2)],
        computation="")
    block = responder._source_block(outcome, ans)
    assert "AD 2.22" in block, block
    assert "AD 2.20" not in block, block


# --- AD 2.2 aerodrome-data path: fetch-by-section then synthesize (fixes the
#     Kano reference-temperature false abstention).

def test_aerodrome_data_query_detected():
    import main
    for q in ("aerodrome reference temperature of Kano", "magnetic variation lagos",
              "ARP coordinates for kano", "transition altitude DNAA"):
        assert main._AERODROME_DATA_RE.search(q), q


def test_aerodrome_data_does_not_catch_other_fields():
    import main
    for q in ("tower frequency", "TORA for rwy 22", "threshold elevation of rwy 04",
              "how many runways in kano"):
        assert not main._AERODROME_DATA_RE.search(q), q
