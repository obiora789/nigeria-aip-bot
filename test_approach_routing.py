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


def test_approach_type_reads_the_models_field():
    import agent
    for pt, want in [("ILS", "ILS"), ("VOR/DME", "VOR"), ("RNAV (GNSS)", "RNAV"),
                     ("rnp", "RNP"), ("NDB", "NDB"), ("IAP", "GENERIC"),
                     ("instrument approach", "GENERIC"),
                     # 'Approach' is the ATC unit, like 'Tower' — never a procedure
                     ("APPROACH", None), ("Approach", None), ("SID", None), ("STAR", None),
                     ("Tower", None), ("ATIS", None), (None, None), ("", None)]:
        assert agent._approach_type(pt) == want, (pt, agent._approach_type(pt))


def test_typed_approach_asked_as_text_goes_to_the_plate_flow():
    """The exact live failure: 'What is the ILS approach in Kano'."""
    ex = _bs("What is the ILS approach in Kano", intent="procedure_lookup",
             procedure_type="ILS", aerodrome_name="Kano")
    assert ex.intent == "chart_retrieval", ex.intent
    assert ex.procedure_type == "ILS", "the named type must reach clarify"


def test_untyped_approach_goes_to_the_plate_flow_and_asks_the_type():
    """'What is the approach in Kano' — no type named. Routed to the plate
    flow with the type cleared, so clarify ASKS rather than searching for a
    chart type called 'IAP'."""
    for intent in ("procedure_lookup", "aerodrome_fact"):
        ex = _bs("What is the approach in Kano", intent=intent,
                 procedure_type="IAP", aerodrome_name="Kano")
        assert ex.intent == "chart_retrieval", (intent, ex.intent)
        assert ex.procedure_type is None, "a generic 'approach' must not reach clarify as a type"


def test_approach_unit_and_navaid_questions_are_untouched():
    """'Kano Approach' the ATC unit, an ILS frequency and an ILS category are
    not approach-procedure questions and must keep their own routes."""
    assert _bs("Kano approach frequency", intent="frequency_retrieval",
               procedure_type="Approach", aerodrome_name="Kano").intent == "frequency_retrieval"
    assert _bs("ILS frequency at Kano", intent="frequency_retrieval",
               procedure_type="ILS", aerodrome_name="Kano").intent == "frequency_retrieval"
    assert _bs("ILS category at Lagos", intent="aerodrome_fact",
               procedure_type="ILS", aerodrome_name="Lagos").intent == "aerodrome_fact"
    assert _bs("SID for Lagos runway 18R", intent="procedure_lookup",
               procedure_type="SID", aerodrome_name="Lagos").intent == "procedure_lookup"


def test_prompt_and_schema_name_the_approach_rule():
    """GUARD. procedure_lookup used to claim 'approach procedure text', which
    is how 'What is the ILS approach' landed there; AD 2.17's 'ATS unit call
    sign' gave the classifier nothing to separate the unit from the
    procedure."""
    import agent, schemas, inspect
    assert "ANY INSTRUMENT APPROACH at an aerodrome is chart_retrieval" in agent._SYSTEM
    assert "SID/STAR/approach/missed-approach procedure text" not in agent._SYSTEM
    src = inspect.getsource(schemas)
    assert "the ATC UNIT, never an approach procedure" in src
    assert "instrument approach " in src and "procedures (ILS, VOR, RNAV, RNP, NDB approaches)" in src
    assert "use 'IAP'" in src
    assert "'PHSIA approach frequency'" in agent._SYSTEM


def test_the_aerodrome_name_is_not_filtered_out_of_unscoped_facts():
    """GUARD. A filter that dropped AD 2.1 from unscoped facts searches was
    added for 'What is the approach in Kano' and then REMOVED: the stress set
    showed it turned 'Amassoma aerodrome name?' into a low-confidence refusal.
    The approach question is fixed by routing it to the plate flow instead."""
    import inspect, main
    src = inspect.getsource(main.process)
    assert '!= "2.1"' not in src, "the AD 2.1 filter is back — it hides real AD 2.1 answers"


def _with_synthesize_rules():
    """The offline harness has no synthesize.py; give agent the two rules it
    reuses, exactly as written there."""
    import re, sys, types
    if "synthesize" in sys.modules and hasattr(sys.modules["synthesize"], "_DECLARED_RE"):
        return None
    fake = types.ModuleType("synthesize")
    fake._MINIMA_RE = re.compile(r"\bminim", re.I)
    fake._DECLARED_RE = re.compile(
        r"\b(tora|toda|asda|lda|declared distance|take-?off run available|"
        r"take-?off distance available|accelerate[\s-]?stop distance|"
        r"landing distance available)", re.I)
    prev = sys.modules.get("synthesize")
    sys.modules["synthesize"] = fake
    return prev or "absent"


def _restore(prev):
    import sys
    if prev == "absent":
        sys.modules.pop("synthesize", None)
    elif prev is not None:
        sys.modules["synthesize"] = prev


def test_declared_distances_never_become_a_chart():
    """Stress set, 2026-10-20: 'lda at Uyo RWY 03' and 'toda at Zaria RWY 23'
    were answered with a plate; August answered them from AD 2.13."""
    prev = _with_synthesize_rules()
    try:
        for q, place in [("lda at Uyo RWY 03", "Uyo"), ("toda at Zaria RWY 23", "Zaria"),
                         ("tora for rwy 29 at asaba", "Asaba")]:
            ex = _bs(q, intent="chart_retrieval", aerodrome_name=place, runway=q.split()[-1])
            assert ex.intent != "chart_retrieval", (q, ex.intent)
            assert ex.ad2_subsection == "2.13", (q, ex.ad2_subsection)
        # Asked for by name, a chart is still a chart.
        ex = _bs("show me the LDA chart for Uyo", intent="chart_retrieval", aerodrome_name="Uyo")
        assert ex.intent == "chart_retrieval"
    finally:
        _restore(prev)


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
    """'approach minima at Kano' must stay on AD 2.22 verbatim (synthesize's
    minima rule), not be turned into a plate request."""
    import re, sys, types
    real = sys.modules.get("synthesize")
    if real is None or not hasattr(real, "_MINIMA_RE"):
        fake = types.ModuleType("synthesize")
        fake._MINIMA_RE = re.compile(r"\bminim", re.I)
        sys.modules["synthesize"] = fake
    try:
        ex = _bs("ILS approach minima at Kano", intent="procedure_lookup",
                 procedure_type="ILS", aerodrome_name="Kano")
        assert ex.intent == "procedure_lookup", ex.intent
    finally:
        if real is None:
            sys.modules.pop("synthesize", None)


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
    answered with an RNAV chart. 'Approach' there is the ATC UNIT. Whatever
    intent the model gives it, it must not reach the plate flow."""
    for intent in ("aerodrome_fact", "frequency_retrieval", "procedure_lookup"):
        ex = _bs("PHSIA approach frequency", intent=intent,
                 procedure_type="Approach", aerodrome_name="PHSIA")
        assert ex.intent != "chart_retrieval", (intent, ex.intent)
    ex = _bs("Kano ILS approach frequency", intent="frequency_retrieval",
             procedure_type="ILS", aerodrome_name="Kano")
    assert ex.intent == "frequency_retrieval"
