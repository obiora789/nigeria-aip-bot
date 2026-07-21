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


def test_declared_reply_handles_genuine_null_field():
    """A per-field None is now a real, expected case (the DNKT fix: TODA/ASDA/
    LDA are published but TORA genuinely isn't) — migrated from
    aip_declared_distances (which never stored a partial record at all) to
    aip_structured (which does, correctly, via null-over-guess). Must render
    as an honest 'not published', never a bare 'None' string."""
    import responder
    from models import Resolution
    res = Resolution(); res.label = "Katsina"; res.icao = "DNKT"
    recs = [{"runway": "05", "tora": None, "toda": 3500, "asda": 3500, "lda": 3500}]
    out = responder.declared_distance_reply(res, recs)
    assert "TORA not published" in out
    assert "TODA 3500 m" in out
    assert "None" not in out


def test_get_declared_distances_reads_aip_structured():
    """Confirms the migration off the stale aip_declared_distances table:
    field names must be remapped from aip_structured's tora_m/toda_m/asda_m/
    lda_m (the ad213_extractor.py schema) to the tora/toda/asda/lda contract
    every caller (declared_distance_reply, main.py's dispatch) already
    expects — a pure data-source swap, not a shape change. Confirmed a real
    gap this fixes: the old table had 69 rows for AD 2.13 against
    aip_structured's 74, meaning several aerodromes' more rigorously
    validated data (including real fixes for DNSO and DNKT) was unused."""
    import database
    from unittest.mock import patch, MagicMock

    fake_rows = [{"record": {"runway": "05", "tora_m": None, "toda_m": 3500,
                             "asda_m": 3500, "lda_m": 3500, "remarks": None}}]
    mock_resp = MagicMock()
    mock_resp.data = fake_rows
    with patch.object(database.supabase, "table") as mock_table:
        chain = mock_table.return_value
        chain.select.return_value.eq.return_value.eq.return_value.order.return_value.execute.return_value = mock_resp
        out = database.get_declared_distances("DNKT")
    mock_table.assert_called_with("aip_structured")
    assert out == [{"runway": "05", "tora": None, "toda": 3500,
                    "asda": 3500, "lda": 3500, "remarks": None}]


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


# --- rwy_data guard: a general runway query with no specific field asked
#     ("Abuja runway") must route to the new structured lookup, not fall
#     through to low-confidence vector search. Confirmed the actual live bug
#     this fixes: "Abuja runway" matched neither the existing asymmetric-field
#     guard (_RWY_BEARING_RE/_THR_FIELD_RE) nor any structured-lookup path, so
#     it fell all the way through to generate_grounded_answer() and returned a
#     55%-similarity match against AD 2.22's approach minima table instead of
#     the runway designation the pilot actually asked for.

def test_general_runway_query_routes_to_structured_lookup():
    import synthesize
    for q in ("Abuja runway", "runways at Kano", "runway data for DNAA",
              "what runways does Lagos have", "how many runways in kano"):
        assert synthesize.synthesize_decision(q, [])[0] == "rwy_data", q


def test_rwy_field_queries_use_structured_lookup():
    """AD 2.12 field queries (length/width/PCN/strength/surface) must reach the
    EXACT structured record, not general synthesis.

    These were originally excluded from rwy_data to preserve behaviour that
    predated the structured lookup existing. That caused a real live failure:
    "What is the PCN for Lagos Runways" was pushed into general synthesis and
    abstained, while the PCN sat in the aip_structured 2.12 record the whole
    time (ad212_extractor puts strength/PCN in each end's detail, and
    runway_data_reply displays it)."""
    import synthesize
    for q in ("runway length DNAA", "width of runway 04", "PCN of runway 22",
              "runway dimensions lagos", "surface strength of RWY 18L",
              "What is the PCN for Lagos Runways", "runway surface at Kano"):
        assert synthesize.synthesize_decision(q, [])[0] == "rwy_data", q


def test_rwy_abbreviation_is_covered():
    """Pilots write "RWY" constantly. The guard originally matched only
    "runway", so "surface strength of RWY 18L" fell through to general
    synthesis while "PCN of runway 22" routed correctly — the same query in
    two spellings behaving differently."""
    import synthesize
    for q in ("surface strength of RWY 18L", "PCN for RWY 18R",
              "RWY 26 dimensions", "what is RWY 04 made of"):
        assert synthesize.synthesize_decision(q, [])[0] == "rwy_data", q


def test_lighting_guard_survives_a_runway_number():
    """"RWY 26 lighting" put the runway NUMBER between the designator and
    "lighting", so the pattern missed and the broader runway guard swallowed
    it. Lighting must keep precedence regardless of where the number sits."""
    import synthesize
    for q in ("RWY 26 lighting", "lighting for RWY 26", "runway lighting DNAA",
              "touchdown zone lighting", "RWY 18L lights"):
        assert synthesize.synthesize_decision(q, [])[0] == "lighting_data", q


def test_asymmetric_fields_still_outrank_the_general_runway_guard():
    """Per-end fields must still reach the verbatim rwy_char path — that guard
    is evaluated first and must not be swallowed by the broader check."""
    import synthesize
    for q in ("true bearing of runway 04", "threshold elevation of RWY 22",
              "threshold coordinates for RWY 18L"):
        assert synthesize.synthesize_decision(q, [])[0] == "rwy_char", q


def test_rwy_data_guard_does_not_overfire_on_asymmetric_fields():
    """Asymmetric fields must still route to the existing rwy_char guard, not
    the new general one — order in synthesize_decision() matters here."""
    import synthesize
    for q in ("true bearing of runway 04", "threshold elevation of RWY 22",
              "threshold coordinates for RWY 18L"):
        assert synthesize.synthesize_decision(q, [])[0] == "rwy_char", q


def test_runway_data_reply_keeps_ends_separate():
    """The load-bearing safety property: each runway end's text must appear
    under its OWN label, never merged into one string — this is the exact
    subsection the original misattribution incident happened on."""
    from responder import runway_data_reply
    from models import Resolution
    res = Resolution(icao="DNAA", label="Abuja/Nnamdi Azikiwe")
    records = [{
        "icao": "DNAA", "designation": "04/22",
        "length_m": 3610, "width_m": 45,
        "end_detail": {
            "04": "THR elevation 331 m, slope 0.5%",
            "22": "THR elevation 342 m, slope -0.3%",
        },
    }]
    out = runway_data_reply(res, records)
    assert "RWY 04/22" in out
    assert "3610 x 45 m" in out
    assert "[04] THR elevation 331 m" in out
    assert "[22] THR elevation 342 m" in out
    # the two ends' data must never appear concatenated into one line
    assert "331 m, slope -0.3%" not in out
    assert "AD 2.12" in out
    assert config.AIRAC_CYCLE in out
    assert "Reference aid only" in out


def test_runway_data_reply_filters_to_requested_runway():
    from responder import runway_data_reply
    from models import Resolution
    res = Resolution(icao="DNMM", label="Lagos/Murtala Muhammed")
    records = [
        {"icao": "DNMM", "designation": "18L/36R", "length_m": 3900, "width_m": 60,
         "end_detail": {"18L": "note A", "36R": "note B"}},
        {"icao": "DNMM", "designation": "18R/36L", "length_m": 2745, "width_m": 45,
         "end_detail": {"18R": "note C", "36L": "note D"}},
    ]
    out = runway_data_reply(res, records, requested_runway="18R")
    assert "18R/36L" in out
    assert "2745 x 45" in out
    assert "18L/36R" not in out
    assert "3900" not in out


# --- AD 2.14 lighting guard: the SAME misattribution shape as AD 2.12, but
#     with NO safe symmetric subset — every field (PAPI angle, lighting type)
#     can genuinely differ between a runway's two ends, so ANY lighting
#     query must route to structured lookup, not just asymmetric ones.
#     Confirmed a real ordering bug while building this: "runway lighting"
#     contains the word "runway", which would be caught by the earlier,
#     more general rwy_data check first (since "lighting" isn't in its
#     excluded-keywords list) and never reach the lighting guard at all —
#     fixed by checking the more specific lighting guard first.

def test_lighting_query_routes_to_structured_lookup():
    import synthesize
    for q in ("runway lighting DNAA", "runway lights lagos",
              "lighting at Lagos runway", "PAPI angle for RWY 04",
              "approach lighting kano", "touchdown zone lighting",
              "VASIS at DNMM", "centreline lighting abuja"):
        assert synthesize.synthesize_decision(q, [])[0] == "lighting_data", q


def test_lighting_guard_does_not_overfire_on_other_subsections():
    """AD 2.15 (beacon/apron/WDI) and AD 2.9 (taxiway markings) also use the
    word 'lighting' in a completely different context — a bare match would
    misroute those queries into this guard."""
    import synthesize
    for q in ("ABN/IBN lighting characteristics", "apron floodlights DNAA",
              "WDI lighting", "secondary power supply lighting",
              "taxiway markings and lighting", "beacon lighting hours"):
        assert synthesize.synthesize_decision(q, [])[0] != "lighting_data", q


def test_lighting_guard_wins_over_general_runway_check():
    """The actual ordering bug found while building this: a lighting query
    containing the word 'runway' must NOT be caught by the earlier, more
    general rwy_data check — the more specific guard must run first."""
    import synthesize
    assert synthesize.synthesize_decision("runway lighting DNAA", [])[0] == "lighting_data"
    # confirm the general runway check still works for genuinely non-lighting queries
    assert synthesize.synthesize_decision("Abuja runway", [])[0] == "rwy_data"


def test_lighting_data_reply_keeps_ends_separate():
    """The same load-bearing safety property as runway_data_reply: each
    runway end's text must appear under its OWN label, never merged."""
    from responder import lighting_data_reply
    from models import Resolution
    res = Resolution(icao="DNKN", label="Kano/Mallam Aminu Kano")
    records = [{
        "icao": "DNKN", "designation": "06/24",
        "end_detail": {
            "06": "PALS PAPI 400m from THR, CAT I angle 3\u00b0",
            "24": "PALS PAPI 400m from THR, CAT I angle 3\u00b025'",
        },
    }]
    out = lighting_data_reply(res, records)
    assert "RWY 06/24" in out
    assert "[06] PALS PAPI 400m from THR, CAT I angle 3\u00b0" in out
    assert "[24] PALS PAPI 400m from THR, CAT I angle 3\u00b025'" in out
    assert "AD 2.14" in out
    assert config.AIRAC_CYCLE in out


def test_lighting_data_reply_handles_general_notes_case():
    """Confirmed genuine case (ad214_extractor.py): some aerodromes have no
    runway-end lighting rows at all — 'no lighting published' is a real fact,
    not an extraction failure, and must display as such rather than error."""
    from responder import lighting_data_reply
    from models import Resolution
    res = Resolution(icao="DNBY", label="Amassoma/Bayelsa")
    records = [{
        "icao": "DNBY", "designation": None,
        "end_detail": {"general_notes": "NIL"},
    }]
    out = lighting_data_reply(res, records)
    assert "NIL" in out
    assert "AD 2.14" in out


# --- deterministic AD 2.x subsection routing. vectorise_aip_v3.py stores one
#     chunk per (aerodrome, subsection), so get_section_text() is an EXACT
#     fetch — the router identifies which subsection a question is about, and
#     retrieval stops being a similarity guess. Runs LAST, after every
#     dedicated guard, so it only claims queries that would otherwise fall
#     through to undirected vector search (the population that produced the
#     confirmed "Abuja runway -> AD 2.22 minima table at 55%" failure).

def test_subsection_router_detects_correct_subsection():
    import subsection_router as sr
    cases = [
        ("what is the RFF category at Kano", "AD 2.6"),
        ("fuel types available at Lagos", "AD 2.4"),
        ("hotels near Abuja airport", "AD 2.5"),
        ("transition altitude for DNMM", "AD 2.17"),
        ("CTR limits for Kano", "AD 2.17"),
        ("bird hazards at Sokoto", "AD 2.23"),
        ("obstacles in the approach at DNAA", "AD 2.10"),
        ("MET office hours at Lagos", "AD 2.11"),
        ("ABN beacon characteristics DNAA", "AD 2.15"),
        ("stop bars at Lagos", "AD 2.9"),
        ("altimeter checkpoint location", "AD 2.8"),
        ("customs and immigration hours", "AD 2.3"),
        ("magnetic variation at Kano", "AD 2.2"),
        ("noise abatement at Lagos", "AD 2.21"),
        ("TLOF elevation DNCA", "AD 2.16"),
        ("taxiing limitations at Abuja", "AD 2.20"),
    ]
    for q, expected in cases:
        assert sr.detect_subsection(q) == expected, (q, sr.detect_subsection(q))


def test_subsection_router_returns_none_when_not_confident():
    """The router must only claim a query when a DISTINCTIVE term matched.
    Returning a subsection for everything would kill general synthesis for
    questions that legitimately span sections — None is the safe default that
    preserves existing behaviour."""
    import subsection_router as sr
    for q in ("what is the elevation of Abuja", "tell me about Lagos airport",
              "where is Kano", "DNAA", "hello"):
        assert sr.detect_subsection(q) is None, (q, sr.detect_subsection(q))


def test_transition_altitude_routes_to_ats_airspace_not_flight_procedures():
    """Real bug caught while building this: 'transition altitude' was in the
    AD 2.22 pattern, which is checked first, so it hijacked a query that
    belongs to AD 2.17 (where AD217Extractor defines transition_altitude as a
    canonical field)."""
    import subsection_router as sr
    assert sr.detect_subsection("transition altitude for DNMM") == "AD 2.17"


def test_ad222_approach_vs_text_only_split():
    """AD 2.22 approach queries have a corresponding AD 2.24 plate to show
    alongside the procedure text. Other AD 2.22 content (take-off minima, PBN
    coding tables) has NO corresponding plate — showing an arbitrary approach
    chart beside such an answer would imply a connection that doesn't exist.

    Scope note (a real CI failure caught this): these test queries must be
    genuine TEXT-intent approach questions, not chart/plate requests. A query
    like "ILS approach plate for Lagos" never reaches detect_subsection() in
    the live pipeline at all — it's classified as intent="chart_retrieval" by
    the LLM extraction and intercepted by main.py's chart-handling path
    (_wants_chart -> _run_chart_decision -> clarify.decide) well before
    synthesize_decision() is ever called. _AD222_RE deliberately has no bare
    "approach"/"plate" term, specifically so it doesn't try to claim queries
    that a different, earlier mechanism already owns correctly."""
    import subsection_router as sr
    for q in ("holding and letdown for sokoto approach",
              "ILS holding procedure for Lagos", "missed approach for RWY 26"):
        assert sr.detect_subsection(q) == "AD 2.22", q
        assert sr.is_approach_query(q) is True, q
    for q in ("take-off minima for DNAA", "PBN requirements at Kano",
              "circling minima DNMM"):
        assert sr.detect_subsection(q) == "AD 2.22", q
        assert sr.is_approach_query(q) is False, q


def test_dedicated_guards_still_win_over_router():
    """Every dedicated guard is more specific and already proven — the router
    runs last and must never steal a query one of them would have claimed."""
    import synthesize
    for q, expected in [
        ("TORA for rwy 22", "declared_distance"),
        ("Lagos tower frequency", "comms"),
        ("runway lighting DNAA", "lighting_data"),
        ("Abuja runway", "rwy_data"),
        ("true bearing of runway 04", "rwy_char"),
    ]:
        assert synthesize.synthesize_decision(q, [])[0] == expected, q


def test_router_status_returns_subsection_id():
    """The router's status carries the exact subsection id as its second
    element, which main.py feeds straight to get_section_text()."""
    import synthesize
    status, payload = synthesize.synthesize_decision("RFF category at Kano", [])
    assert status == "subsection"
    assert payload == "AD 2.6"


# --- clarify.py merge (from ad222_respond.py): the deterministic, zero-LLM
#     info-block slicer for AD 2.22's known non-approach headings. This is
#     the ONLY piece of ad222_respond.py that was genuinely valuable and not
#     duplicated elsewhere — everything else in that file operated on raw
#     PDF words at query time (never possible in the live bot) or duplicated
#     what main.py's chart-decision path already does correctly.

_SAMPLE_AD222 = """AD 2.22 FLIGHT PROCEDURES — DNSO (source: AIP pages 966-970)

2.22.1 General
All aircraft shall comply with standard noise abatement procedures.
Pilots are advised to maintain radio contact at all times.

2.22.2 Runway in use
RWY 08 is normally used for landing and take-off in calm wind conditions.
RWY 26 may be used when wind conditions dictate.

2.22.3 Radar Procedures
Radar vectoring is available for arriving and departing aircraft.

2.22.4 Procedures for VFR flights within CTR
VFR flights shall remain below 3500 ft within the CTR unless cleared otherwise.

2.22.5 VFR weather minima
Minimum visibility for VFR flight is 5 km.

2.22.6 Instrument approach procedures for RWY 26 based on VOR/DME
Holding procedure: as depicted.
Letdown procedure: descend to MDA.
Missed approach: climb straight ahead to 3500 ft.
"""


def test_info_block_answer_does_not_leak_into_approach_section():
    """Real bug found and fixed while merging: the original hardcoded
    end-boundary (inherited from ad222_respond.py) assumed every aerodrome
    numbers the section after VFR minima as '2.22.7' — on different
    numbering, the slice bled straight through the entire Instrument
    Approach Procedures section (Holding/Letdown/Missed Approach text) into
    what should have been a short VFR-minima answer."""
    import clarify
    out = clarify.info_block_answer(_SAMPLE_AD222, "vfr minima at sokoto")
    assert out is not None
    assert "Minimum visibility" in out
    assert "Holding procedure" not in out
    assert "Missed approach" not in out


def test_info_block_answer_covers_all_five_headings():
    import clarify
    cases = [
        ("general procedure for sokoto", "radio contact"),
        ("what runway is in use at sokoto", "RWY 08"),
        ("radar procedures for sokoto", "Radar vectoring"),
        ("vfr minima at sokoto", "Minimum visibility"),
    ]
    for q, expect_text in cases:
        out = clarify.info_block_answer(_SAMPLE_AD222, q)
        assert out is not None, q
        assert expect_text in out, (q, out)


def test_info_block_answer_returns_none_for_unrecognized_or_approach_queries():
    """None means 'no known heading matched' — the caller falls back to LLM
    synthesis over the whole section rather than guessing which heading was
    meant. An approach-procedure query must also return None here (it's
    handled by procedures.py, not this deterministic slicer)."""
    import clarify
    assert clarify.info_block_answer(_SAMPLE_AD222, "elevation of sokoto") is None
    assert clarify.info_block_answer(_SAMPLE_AD222, "holding procedure for sokoto") is None


def test_clarify_type_vocabularies_serve_distinct_purposes():
    """norm_type (display/chart-matching, uppercase) and
    norm_type_for_procedures (procedures.py's own matching key, lowercase)
    are deliberately kept separate rather than merged into one, since they
    feed different consumers with different case conventions."""
    import clarify
    assert clarify.norm_type("vor/dme") == "VOR"
    assert clarify.norm_type_for_procedures("VOR/DME") == "vor"
    assert clarify.norm_type("rnav (gnss)") == "RNAV"
    assert clarify.norm_type_for_procedures("GNSS") == "rnav"


def test_clarify_decide_unaffected_by_merge():
    """The merge must not change decide()'s existing, already-proven
    behaviour — confirmed by re-running its own established cases."""
    import clarify
    from types import SimpleNamespace
    charts = [
        SimpleNamespace(procedure_type="ILS Approach Chart", runway="26"),
        SimpleNamespace(procedure_type="VOR Approach Chart", runway="08"),
        SimpleNamespace(procedure_type="VOR Approach Chart", runway="26"),
    ]
    d = clarify.decide(charts)
    assert d.action == "ask_type"
    assert set(d.options) == {"ILS", "VOR"}

    d2 = clarify.decide(charts, specified_type="VOR")
    assert d2.action == "ask_runway"
    assert set(d2.options) == {"08", "26"}

    d3 = clarify.decide(charts, specified_type="VOR", specified_runway="08")
    assert d3.action == "send"
    assert len(d3.charts) == 1


# --- semantic subsection routing (flag-gated, default OFF). The keyword
#     router only fires on terms someone thought to list; this picks the
#     subsection the RETRIEVER ranked highest, using the results
#     synthesize_decision already has (no extra DB call), and answers from
#     that ONE section. Two guards stop it being confidently wrong where
#     keywords would simply stay silent.

def _res(section, sim):
    from models import AIPResult
    return AIPResult(content="x", similarity=sim, chart_url=None,
                     aip_section=section, reference_tag="DNAA")


def test_semantic_subsection_picks_clear_winner():
    import synthesize
    assert synthesize.semantic_subsection(
        [_res("AD 2.4", 0.62), _res("AD 2.5", 0.41), _res("AD 2.3", 0.38)]) == "AD 2.4"


def test_semantic_subsection_declines_on_near_tie():
    """Two subsections near-tied means picking either is a coin flip —
    decline, and the caller keeps its pre-existing behaviour."""
    import synthesize
    assert synthesize.semantic_subsection([_res("AD 2.4", 0.62), _res("AD 2.5", 0.60)]) is None


def test_semantic_subsection_declines_on_weak_match():
    """A weak top score means the retriever has no real opinion — don't
    manufacture one."""
    import synthesize
    assert synthesize.semantic_subsection([_res("AD 2.4", 0.28), _res("AD 2.5", 0.11)]) is None


def test_semantic_subsection_ignores_non_ad2_sections():
    import synthesize
    assert synthesize.semantic_subsection(
        [_res("ENR 1.5", 0.90), _res("GEN 2.1", 0.85), _res("AD 2.6", 0.55)]) == "AD 2.6"
    assert synthesize.semantic_subsection([_res("ENR 1.5", 0.9)]) is None
    assert synthesize.semantic_subsection([]) is None


def test_semantic_subsection_uses_best_chunk_per_section():
    """A section is scored by its BEST chunk, not its first or its average."""
    import synthesize
    assert synthesize.semantic_subsection(
        [_res("AD 2.4", 0.31), _res("AD 2.4", 0.66), _res("AD 2.9", 0.40)]) == "AD 2.4"


def test_semantic_routing_is_off_by_default():
    """Must change nothing until deliberately enabled and measured."""
    import config
    assert config.SEMANTIC_SUBSECTION_ENABLED is False


def test_keyword_and_safety_guards_outrank_semantic():
    """Even with semantic routing on, the proven guards and keyword router
    must still win — semantic is a FALLBACK, not a replacement."""
    import synthesize, config
    orig = config.SEMANTIC_SUBSECTION_ENABLED
    config.SEMANTIC_SUBSECTION_ENABLED = True
    try:
        r = [_res("AD 2.4", 0.62), _res("AD 2.5", 0.41)]
        assert synthesize.synthesize_decision("RFF category Kano", r) == ("subsection", "AD 2.6")
        assert synthesize.synthesize_decision("approach minima DNAA", r)[0] == "subsection_verbatim"
        assert synthesize.synthesize_decision("Lagos tower frequency", r)[0] == "comms"
        # and the genuinely-uncovered phrasing now routes instead of guessing
        assert synthesize.synthesize_decision(
            "what services are available at Enugu", r) == ("subsection", "AD 2.4")
    finally:
        config.SEMANTIC_SUBSECTION_ENABLED = orig


def test_get_subsection_text_matches_exactly_not_by_prefix():
    """A LIKE prefix makes "AD 2.2" also match AD 2.20-2.24 (including the
    ~55k-char AD 2.22), which would hand synthesis six subsections at once and
    reintroduce cross-subsection misattribution. Equality is required."""
    import database, inspect
    src = inspect.getsource(database.get_subsection_text)
    assert '.eq("aip_section", section)' in src
    assert ".like(" not in src


def test_airspace_redirect_survives_a_null_aerodrome_name():
    """The redirect must not depend on the extractor populating
    aerodrome_name. For "lagos control zone" the LLM can read the whole
    phrase as an airspace NAME and leave that field null, which silently
    disabled the redirect and sent the query to ENR 1.1 @ 63%.

    resolver.match_name() scans free text and returns ICAO CODES — so the
    rescue path must feed the result back as icao_code, not aerodrome_name
    ('DNMM' is not an alias of itself)."""
    import resolver
    _seed_index()          # deterministic: only the seeded aerodromes exist here
    for q, want in (
            ("what is the lateral limit for lagos control zone", "DNMM"),
            ("vertical limits of the kano control zone", "DNKN"),
            ("airspace classification for abuja TMA", "DNAA"),
            ("transition altitude for port harcourt", "DNPO")):
        hits = resolver.match_name(q)
        assert hits == {want}, (q, hits)


def test_main_redirect_feeds_scan_result_as_icao_code():
    """Guard the exact bug above: assigning match_name()'s output to
    aerodrome_name silently fails to resolve."""
    import inspect, main
    src = inspect.getsource(main)
    assert "resolver.match_name(" in src, "text-scan rescue missing from main.py"
    assert "_ex2.icao_code = _scan_icao" in src, "scan result must be fed back as icao_code"


def test_main_has_the_ad217_airspace_redirect():
    """GUARD AGAINST SILENT LOSS. This redirect has been dropped once already
    by a rebuild that started from a copy without it, putting the bug straight
    back into production. It lives in main.py's request path, which the
    offline tests otherwise never execute, so assert its presence directly.

    Fixes: "what is the lateral limit for lagos ctr" -> ENR 3.1 @ 59% and
    "what is the lateral limit for maiduguri ctr" -> ENR 3.1 @ 54%, when both
    aerodromes' own AD 2.17 held the exact answer."""
    import inspect, main
    src = inspect.getsource(main)
    assert 'res.reference == "AIRSPACE"' in src, "AD 2.17 airspace redirect is MISSING from main.py"
    assert "_normalise_subsection" in src, "redirect no longer consults the classifier"
    assert "aerodrome_fact" in src, "redirect no longer re-resolves to the aerodrome"


def test_ad217_redirect_is_narrow_enough():
    """It must fire for an aerodrome's own airspace and NOT for genuine
    en-route content, otherwise real ENR queries get pinned to an AD section
    where they will never be found."""
    import subsection_router as sr, synthesize as sy
    for q in ("what is the lateral limit for lagos ctr",
              "what is the lateral limit for maiduguri ctr",
              "What is the limits for Lagos CTR?", "CTR vertical limits Kano"):
        assert sr.detect_subsection(q) == "AD 2.17" or sy._normalise_subsection("2.17"), q
    for q in ("airways through the Kano FIR", "waypoints on UL433",
              "what are the restricted areas in Nigeria"):
        assert sr.detect_subsection(q) != "AD 2.17", q


# --- field-level fact retrieval. One embedding per FIELD instead of one per
#     subsection. DNMM's AD 2.22 was 79,871 characters behind a single vector,
#     which is measurably why the top chunk was so often the wrong section
#     ("lateral limit for lagos ctr" -> ENR 3.1 @ 59%). Facts are shown
#     VERBATIM — no synthesis, so no hallucination surface.

def test_facts_reply_groups_by_entity_and_never_merges():
    """Each line is a stored value under its OWN entity. Two runways' values
    must never appear on one line — the entity is part of the retrieved unit,
    not reconstructed at display time."""
    from responder import facts_reply
    from models import Resolution
    res = Resolution(); res.label = "Lagos"; res.icao = "DNMM"
    facts = [
        {"subsection": "2.13", "entity": "RWY 18L", "label": "TORA",
         "fact_value": "2745 m", "similarity": 0.77},
        {"subsection": "2.13", "entity": "RWY 18R", "label": "TORA",
         "fact_value": "3900 m", "similarity": 0.63},
    ]
    out = facts_reply(res, facts)
    assert "RWY 18L:" in out and "RWY 18R:" in out
    assert "2745 m" in out and "3900 m" in out
    # the two runways' values must be on separate lines under separate headings
    for line in out.splitlines():
        assert not ("2745" in line and "3900" in line), line
    assert "AD 2.13" in out
    assert config.AIRAC_CYCLE in out


def test_facts_reply_cites_the_single_subsection():
    from responder import facts_reply
    from models import Resolution
    res = Resolution(); res.label = "Lagos"; res.icao = "DNMM"
    facts = [{"subsection": "2.17", "entity": "", "label": "Designation and lateral limits",
              "fact_value": "CTR. A circle radius 20NM", "similarity": 0.71}]
    out = facts_reply(res, facts)
    assert "AD 2.17" in out
    assert "CTR. A circle radius 20NM" in out


def test_facts_reply_abstains_on_empty():
    from responder import facts_reply, not_in_aip
    from models import Resolution
    res = Resolution(); res.label = "Lagos"; res.icao = "DNMM"
    assert facts_reply(res, []) == not_in_aip(res)


def test_facts_path_is_off_by_default():
    """Must change nothing until deliberately enabled and measured."""
    import config
    assert config.FACTS_ENABLED is False


def test_facts_path_is_wired_into_main():
    """GUARD: this lives in main.py's request path, which the offline tests
    never execute, so assert its presence directly — the AD 2.17 redirect was
    silently lost once by a rebuild and put a live bug straight back."""
    import inspect, main
    src = inspect.getsource(main)
    assert "search_facts" in src, "facts retrieval not wired into main.py"
    assert "facts_reply" in src, "facts reply not wired into main.py"
    assert "config.FACTS_MIN_SIM" in src, "confidence floor not applied"


# --- LLM subsection classification. 35 regexes were doing SEMANTIC
#     CLASSIFICATION — deciding what a pilot's question is about — which is
#     the one job an LLM does better than code, and the extraction call
#     already reads the same text. Every phrasing failure was that one
#     mistake repeated: "PCN" excluded by a keyword list, "RWY" missing where
#     "runway" was present, "OCA/H" finding its section then discarding it.
#     The classifier needs no phrasing enumerated. Regexes remain for SAFETY
#     POLICY, which is a rule rather than a guess.

def _ex_sub(sub):
    from types import SimpleNamespace
    return SimpleNamespace(ad2_subsection=sub)


def test_llm_subsection_fixes_the_real_failures():
    """The four confirmed live failures, all fixed by classification."""
    import synthesize
    for q, sub, want in [
            ("What is the PCN for Lagos Runways", "2.12", "rwy_data"),
            ("what is the OCA/H for Lagos", "2.22", "subsection_verbatim"),
            ("What is the limits for Lagos CTR?", "2.17", "subsection"),
            ("what is the lateral limit for lagos ctr", "2.17", "subsection")]:
        assert synthesize.synthesize_decision(q, [], _ex_sub(sub))[0] == want, q


def test_llm_subsection_handles_phrasings_no_keyword_list_covers():
    """The actual point: these match nothing in any keyword list."""
    import synthesize
    for q, sub, want in [
            ("how thick is the tarmac at Lagos", "2.12", "rwy_data"),
            ("can a 747 land at Sokoto", "2.12", "rwy_data"),
            ("who do I call on the radio at Kano", "2.18", "comms"),
            ("what beacons help me find Abuja at night", "2.19", "navaid"),
            ("is there anywhere to eat at Port Harcourt", "2.5", "subsection"),
            ("how far can I roll before rotating on 18L", "2.13", "declared_distance")]:
        assert synthesize.synthesize_decision(q, [], _ex_sub(sub))[0] == want, q


def test_safety_policy_overrides_the_classifier():
    """A wrong or adversarial classification must NEVER unlock something the
    policy forbids. Minima, procedures and restrictions are rules."""
    import synthesize
    for q, sub, want in [
            ("approach minima for RWY 04 Lagos", "2.12", "subsection_verbatim"),
            ("decision height at Kano", "2.17", "subsection_verbatim"),
            ("holding procedure Sokoto", "2.12", "approach_procedure"),
            ("missed approach RWY 21", "2.14", "approach_procedure"),
            ("night flying ban at Lagos", "2.20", "fallback"),
            ("is Kano under curfew", "2.3", "fallback")]:
        assert synthesize.synthesize_decision(q, [], _ex_sub(sub))[0] == want, q


def test_asymmetric_fields_survive_a_coarse_classification():
    """Per-end fields are a distinction WITHIN AD 2.12 that the classifier
    won't make, so the regex still applies inside that subsection."""
    import synthesize
    for q in ("true bearing of runway 04", "threshold elevation RWY 22",
              "threshold coordinates for RWY 18L"):
        assert synthesize.synthesize_decision(q, [], _ex_sub("2.12"))[0] == "rwy_char", q


def test_routing_is_backward_compatible_without_extraction():
    """ex=None (or a null classification) must fall back to keyword routing
    exactly as before — nothing regresses if the classifier is unavailable."""
    import synthesize
    for q, want in [("Lagos tower frequency", "comms"), ("TORA for rwy 22", "declared_distance"),
                    ("RFF category Kano", "subsection"), ("Abuja runway", "rwy_data"),
                    ("runway lighting DNAA", "lighting_data")]:
        assert synthesize.synthesize_decision(q, [])[0] == want, q
        assert synthesize.synthesize_decision(q, [], _ex_sub(None))[0] == want, q


def test_subsection_normalisation():
    """The classifier may answer '2.12', 'AD 2.12' or 'ad2.12'."""
    import synthesize as S
    for raw in ("2.12", "AD 2.12", "ad2.12", " AD  2.12 "):
        assert S._normalise_subsection(raw) == "AD 2.12", raw
    for raw in (None, "", "banana", "2.99", "3.1"):
        assert S._normalise_subsection(raw) is None, raw


# --- pilot-phrasing routing coverage. The existing 63-question eval set sent
#     33 of 63 queries to "grounded" (plain vector search) and exercised NONE
#     of the structured-lookup paths, so nothing caught these three real
#     misroutes. Each was found by testing realistic phrasing variants:
#       1. _DECLARED_RE matched only the abbreviations, so a pilot writing
#          "takeoff run available" fell through to general synthesis — the one
#          path asymmetric per-runway values must never take.
#       2. _NAVAID_RE had a plural bug ("navaid\b" never matched "navaids")
#          and required a value word, so "what navaids are at Abuja" missed.
#       3. _COMMS_SVC_RE fired on bare apron/ramp/ground, hijacking AD 2.8
#          (apron strength), AD 2.4 (ground handling), AD 2.9 (apron
#          markings), AD 2.15 (apron floodlights) and AD 2.20 (parking) —
#          8 of 8 real phrasings went to comms.

_ROUTING_CASES = {
    "declared_distance": ["TORA for rwy 22 Lagos", "what is the LDA at DNAA",
        "how long is the takeoff run available on 18L",
        "landing distance available RWY 26", "accelerate stop distance Kano"],
    "comms": ["Lagos tower frequency", "what freq is Kano ground",
        "ATIS frequency for DNAA", "apron frequency Lagos", "contact ground Kano"],
    "navaid": ["VOR frequency at Kano", "what navaids are at Abuja",
        "DME channel for Sokoto", "navaid list DNAA"],
    "rwy_char": ["true bearing of runway 04", "threshold elevation of RWY 22"],
    "rwy_data": ["Abuja runway", "runways at Kano", "how many runways in Enugu"],
    "lighting_data": ["runway lighting DNAA", "PAPI angle for RWY 04",
        "approach lights Kano", "runway lights at Lagos"],
    "approach_procedure": ["holding procedure for Sokoto", "letdown for RWY 26",
        "missed approach RWY 21 Lagos"],
    "subsection_verbatim": ["approach minima for DNAA", "decision height RWY 18R"],
    "fallback": ["night flying ban at Lagos", "is Kano under curfew"],
    "subsection": ["RFF category at Kano", "fuel available at Lagos",
        "transition altitude DNMM", "bird hazards Sokoto", "MET office hours Lagos",
        "ABN beacon DNAA", "magnetic variation Kano", "apron strength DNAA",
        "ground handling at Kano", "apron surface Lagos", "apron floodlights DNAA"],
}


def test_pilot_phrasing_routes_correctly():
    """Every realistic phrasing must reach its intended path. A miss here means
    the query silently falls through to plain vector search."""
    import synthesize
    bad = []
    for want, queries in _ROUTING_CASES.items():
        for q in queries:
            got = synthesize.synthesize_decision(q, [])[0]
            if got != want:
                bad.append(f"{q!r}: want {want}, got {got}")
    assert not bad, "misrouted:\n  " + "\n  ".join(bad)


def test_comms_guard_does_not_hijack_shared_vocabulary():
    """apron/ramp/ground name AD 2.8/2.4/2.9/2.15/2.20 fields too — they must
    not route to comms without an explicit frequency word."""
    import synthesize
    for q in ("apron strength DNAA", "ground handling at Kano", "apron surface Lagos",
              "apron floodlights DNAA", "taxiway and apron markings Kano",
              "ramp surface strength"):
        assert synthesize.synthesize_decision(q, [])[0] != "comms", q
    # ...but WITH a frequency word they still must
    for q in ("apron frequency Lagos", "what freq is Kano ground", "contact ground Kano"):
        assert synthesize.synthesize_decision(q, [])[0] == "comms", q


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
