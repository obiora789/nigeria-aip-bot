"""
test_approach_routing.py — an instrument approach always reaches the plate flow.

No network, no API calls. Run with:  pytest test_approach_routing.py -q

Confirmed live (2026-10-20):
  "What is the ILS approach in Kano" -> Kano AD 2.17: the TMA's "Kano Approach"
                                        CALL SIGN, CTR/TMA limits, TA.
  "What is the approach in Kano"     -> Kano AD 2.1: city and aerodrome name.

Neither reached the chart flow. The model filed both as text lookups, the
subsection classifier matched the word "approach" to the Approach UNIT in
AD 2.17, and the facts path answered whatever field sat nearest. An approach
procedure has exactly one safe destination — the plate flow, which asks type
and runway — so these tests pin the routing, not the wording.
"""
import os

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")


def _bs(raw, **kw):
    import agent, resolver
    from schemas import AIPQueryExtraction
    resolver.load_index()
    base = dict(intent="aerodrome_fact", filter_part="AD")
    base.update(kw)
    return agent._backstop(AIPQueryExtraction(**base), raw)


def test_no_wording_patterns_decide_approach_routing():
    """GUARD. The owner's rule: these queries are routed on the model's
    judgement, never on patterns over the pilot's words. Three such helpers
    existed (approach type from procedure_type text, minima words, declared-
    distance words) and were removed."""
    import agent
    for name in ("_approach_type", "_asks_minima", "_asks_declared", "_APPROACH_TYPES"):
        assert not hasattr(agent, name), f"{name} is back"


def test_procedure_answer_reaches_the_plate_flow():
    """When the model's approach_request is 'procedure' — e.g. the pilot tapped
    'approach plate' on the Sokoto choice — that wins over the intent, and an
    untyped request goes to the plate flow to ask the type. (Bare 'Sokoto
    approach' itself is now a two-way choice: test_disambiguation.py.)"""
    for q, intent in [("Show me sokoto approach", "frequency_retrieval"),
                      ("What is sokoto approach", "aerodrome_fact"),
                      ("sokoto approach", "procedure_lookup")]:
        ex = _bs(q, intent=intent, approach_request="procedure", aerodrome_name="Sokoto")
        assert ex.intent == "chart_retrieval", (q, ex.intent)
        assert ex.procedure_type is None, "no type named: the plate flow must ASK"


def test_typed_approach_carries_its_type_to_the_plate_flow():
    ex = _bs("What is the ILS approach in Kano", intent="procedure_lookup",
             approach_request="procedure", approach_type="ILS",
             procedure_type="ILS approach", aerodrome_name="Kano")
    assert ex.intent == "chart_retrieval" and ex.procedure_type == "ILS"


def test_the_approach_unit_stays_a_frequency_question():
    """'Sokoto approach frequency' is the ATC unit. Even if the intent came
    back as a chart, the model's 'unit' answer turns it back."""
    for intent in ("frequency_retrieval", "chart_retrieval"):
        ex = _bs("Sokoto approach frequency", intent=intent,
                 approach_request="unit", aerodrome_name="Sokoto")
        assert ex.intent == "frequency_retrieval", (intent, ex.intent)


def test_questions_that_are_not_about_an_approach_are_untouched():
    for q, intent, kw in [
            ("ILS frequency at Kano", "frequency_retrieval", {"procedure_type": "ILS"}),
            ("ILS category at Lagos", "aerodrome_fact", {"procedure_type": "ILS"}),
            ("SID for Lagos runway 18R", "procedure_lookup", {"procedure_type": "SID"})]:
        ex = _bs(q, intent=intent, aerodrome_name=q.split()[-1] if "Kano" in q else "Lagos", **kw)
        assert ex.intent == intent, (q, ex.intent)


def test_prompt_and_schema_name_the_approach_rule():
    import agent, inspect, re, schemas
    assert "AN INSTRUMENT APPROACH at an aerodrome is chart_retrieval" in agent._SYSTEM
    # 'Sokoto approach' is the ATC unit by default AND offered as a choice
    # (test_disambiguation.py), never silently the plate.
    assert "'Show me Sokoto approach'" in agent._SYSTEM
    assert "SID/STAR/approach/missed-approach procedure text" not in agent._SYSTEM
    src = re.sub(r'"\s*\n\s*"', "", inspect.getsource(schemas))
    assert "approach_request: Optional[Literal[\"procedure\", \"minima\", \"unit\"]]" in src
    assert "use 'IAP'" not in src
    fields = list(getattr(schemas.AIPQueryExtraction, "model_fields", None)
                  or schemas.AIPQueryExtraction.__annotations__)
    assert "approach_request" in fields and "approach_type" in fields


def test_the_aerodrome_name_is_not_filtered_out_of_unscoped_facts():
    """GUARD. A filter that dropped AD 2.1 from unscoped facts searches was
    added for 'What is the approach in Kano' and then REMOVED: the stress set
    showed it turned 'Amassoma aerodrome name?' into a low-confidence refusal.
    The approach question is fixed by routing it to the plate flow instead."""
    import inspect, main
    src = inspect.getsource(main.process)
    assert '!= "2.1"' not in src, "the AD 2.1 filter is back — it hides real AD 2.1 answers"


def test_declared_distances_never_become_a_chart():
    """Stress set, 2026-10-20: 'lda at Uyo RWY 03' and 'toda at Zaria RWY 23'
    were answered with a plate. Decided on the model's subsection (AD 2.13)."""
    for q, place in [("lda at Uyo RWY 03", "Uyo"), ("toda at Zaria RWY 23", "Zaria")]:
        ex = _bs(q, intent="chart_retrieval", aerodrome_name=place, ad2_subsection="2.13")
        assert ex.intent == "runway_data", (q, ex.intent)
    # An approach question the model filed under 2.13 by mistake stays a chart.
    ex = _bs("ILS approach Uyo", intent="chart_retrieval", aerodrome_name="Uyo",
             ad2_subsection="2.13", approach_request="procedure", approach_type="ILS")
    assert ex.intent == "chart_retrieval"


def test_clearance_priorities_are_ad_2_7_not_atc_clearances():
    """Stress set: 'clearance priorities at Lagos' went to AD 2.22's 'ATC
    clearances for VFR flights' at 4 aerodromes. The schema must separate them."""
    import inspect, re, schemas
    # Join adjacent string literals so a phrase split across source lines
    # still matches, without depending on the pydantic version.
    desc = re.sub(r'"\s*\n\s*"', "", inspect.getsource(schemas))
    assert "clearance priorities, clearing equipment" in desc
    assert "never an ATC clearance" in desc
    assert "ATC clearances for VFR flights" in desc


def test_minima_keep_their_verbatim_route():
    """'ILS minima for Kano' stays procedure_lookup (AD 2.22 verbatim), on the
    model's approach_request='minima', not on the word."""
    ex = _bs("ILS approach minima at Kano", intent="procedure_lookup",
             approach_request="minima", approach_type="ILS", aerodrome_name="Kano")
    assert ex.intent == "procedure_lookup", ex.intent


# --- the aerodrome must be the one the user named ------------------------------

def test_model_cannot_swap_the_named_aerodrome_for_a_better_known_one():
    """Confirmed by check_approach_routing.py across all 40 aerodromes:
    'Soku VOR approach' was answered as SOKOTO. Soku (DNSK) is published;
    the model 'corrected' it to Sokoto (DNSO)."""
    for kw in ({"icao_code": "DNSO"}, {"aerodrome_name": "Sokoto"}):
        ex = _bs("Soku VOR approach", intent="chart_retrieval",
                 procedure_type="VOR", **kw)
        assert ex.icao_code == "DNSK", (kw, ex.icao_code, ex.aerodrome_name)


def test_the_models_choice_stands_when_the_user_named_it():
    """Sokoto asked as Sokoto, as its ICAO code, and a VOR ident question all
    keep the model's aerodrome."""
    assert _bs("Sokoto VOR approach", intent="chart_retrieval",
               icao_code="DNSO").icao_code == "DNSO"
    assert _bs("DNSO VOR approach", intent="chart_retrieval",
               icao_code="DNSO").icao_code == "DNSO"
    assert _bs("what is the Lagos tower frequency", intent="frequency_retrieval",
               aerodrome_name="Lagos").aerodrome_name == "Lagos"


def test_text_naming_several_aerodromes_is_left_to_the_model():
    """'Lagos to Port Harcourt' names two. resolver.match_name keeps only the
    longest match, which would wrongly report one; this guard must not."""
    import agent
    assert agent._aerodromes_named_in("distance from Lagos to Port Harcourt") == {"DNMM", "DNPO"}
    ex = _bs("distance from Lagos to Port Harcourt", intent="aerodrome_fact",
             icao_code="DNMM")
    assert ex.icao_code == "DNMM"


def test_no_aerodrome_in_the_text_changes_nothing():
    """A follow-up ('and the ILS?') carries the last aerodrome; the guard has
    nothing to compare against and must not touch it."""
    ex = _bs("and the ILS approach?", intent="chart_retrieval",
             icao_code="DNKN", procedure_type="ILS")
    assert ex.icao_code == "DNKN"



def test_approach_unit_frequency_never_becomes_a_plate():
    """Confirmed by the 40-aerodrome full run: 'PHSIA approach frequency' was
    answered with an RNAV chart. The model's 'unit' answer keeps it a
    frequency question whatever intent came back."""
    for intent in ("aerodrome_fact", "frequency_retrieval", "chart_retrieval"):
        ex = _bs("PHSIA approach frequency", intent=intent,
                 approach_request="unit", aerodrome_name="PHSIA")
        assert ex.intent != "chart_retrieval", (intent, ex.intent)


def test_prompt_separates_typed_approach_unit_frequency_and_radar():
    """Measured 2026-10-21 at all 40 aerodromes: 'X VOR approach' came back as
    the ATC unit, approach-frequency questions offered a needless choice, and
    'approach radar' went to a plate. The prompt now states each case; the
    model decides, nothing here reads wording."""
    import agent, re
    s = re.sub(r"\s+", " ", agent._SYSTEM)
    assert "An approach TYPE before 'approach' always means the procedure" in s
    assert "So is a question that asks for a frequency, call sign or how to contact it" in s
    assert "Approach RADAR control and radar procedures are procedure_lookup" in s


def test_prompt_refuses_requests_that_are_not_aip_questions():
    """'book me a flight to Lagos' was accepted (false accept, measure_intent)."""
    import agent, re
    s = re.sub(r"\s+", " ", agent._SYSTEM)
    assert "(d) NOT AN AIP QUESTION" in s and "one of these four must be true" in s


def test_a_booking_refusal_is_not_overridden_for_naming_lagos():
    """(d) uses planning_or_flow, which _refusal_has_basis already accepts, so
    the 'refusal with no basis' override (6c) leaves it refused even though
    the text names exactly one Nigerian aerodrome."""
    ex = _bs("book me a flight to Lagos", intent="out_of_scope",
             out_of_scope_reason="planning_or_flow", aerodrome_name="Lagos")
    assert ex.intent == "out_of_scope"
