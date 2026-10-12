"""
test_live_and_charges.py — the live-query rule and the GEN 4 charges path.

No network, no API calls. Run with:  pytest test_live_and_charges.py -q

Two properties, both safety-relevant:

  LIVE QUESTIONS ARE REFUSED, whatever the corrections in agent._backstop do.
  "Live" means the ANSWER needs a NOTAM, ATIS, a weather report or a
  controller. It never means the question contains "now", "current" or
  "today": the AIP holds information of lasting character, and a frequency,
  a TA or the AD 2.22.2 runway-in-use statement is the same whatever the
  wording. Those are ANSWERED, and tested as such below.
  Confirmed live before this change: measure_intent.py reported
  'current METAR for Kano' -> aerodrome_fact, because step 5 rescued a refusal
  that step 6c would have kept. The fix enforces the rule once, after every
  correction, so no step can undo it and a step added later is covered too.

  PUBLISHED CHARGES ARE SHOWN, not refused — verbatim from GEN 4, with the
  amendment they are valid for and the body to ask for current rates.
"""
import os

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

from types import SimpleNamespace as N  # noqa: E402


def _ex(**kw):
    from schemas import AIPQueryExtraction
    base = dict(intent="aerodrome_fact", filter_part="AD")
    base.update(kw)
    return AIPQueryExtraction(**base)


def _bs(raw, **kw):
    import agent, resolver
    resolver.load_index()
    return agent._backstop(_ex(**kw), raw)


# --- asks_live: the one definition every caller uses ------------------------

def test_asks_live_reads_the_models_judgement():
    import agent
    assert agent.asks_live(_ex(needs_live_source=True))
    for reason in ("transient_status", "current_weather", "atc_clearance",
                   "planning_or_flow"):
        assert agent.asks_live(_ex(intent="out_of_scope", out_of_scope_reason=reason)), reason
    # refusals that are not about TIME are not live
    for reason in ("charges", "outside_nigeria", "other", None):
        assert not agent.asks_live(_ex(intent="out_of_scope", out_of_scope_reason=reason)), reason
    assert not agent.asks_live(_ex(needs_live_source=False))
    assert not agent.asks_live(None)


def test_asks_live_tolerates_extractions_without_the_new_fields():
    """Old test objects and a partial deploy carry neither field. They must
    read as not-live — the pre-change behaviour — rather than crash."""
    import agent
    assert agent.asks_live(N(intent="aerodrome_fact")) is False


# --- the METAR bypass ---------------------------------------------------------

def test_metar_is_not_rescued_by_step_5():
    """The confirmed live failure. Step 5 matched 'metar' in _AD_FIELD_AT_RE
    and rewrote a correct refusal to aerodrome_fact."""
    ex = _bs("current METAR for Kano", intent="out_of_scope",
             out_of_scope_reason="current_weather", needs_live_source=True)
    assert ex.intent == "out_of_scope", ex.intent


def test_no_word_list_decides_what_is_live():
    """GUARD. A regex of time and weather words ("current", "today", "latest",
    "wind", "runway in use"...) used to veto the rescues, so a published fact
    asked with one of those words stayed refused. Only the model's judgement
    of the ANSWER may decide."""
    import agent
    assert not hasattr(agent, "_LIVE_RE"), "the time-word regex is back"
    assert not hasattr(agent, "_PRICE_RE"), "the price-word regex is back"
    assert not hasattr(agent, "_is_genuinely_out_of_scope")


def test_published_fact_with_a_live_sounding_word_is_rescued():
    """'current ATIS frequency at Kano' is AD 2.18 data. If the model refuses
    it without a reason that puts the answer outside the AIP, the refusal has
    no basis — the word 'current' is not one."""
    for raw in ("current ATIS frequency at Kano", "Lagos tower frequency now",
                "latest RFFS category at Kano today"):
        for reason in (None, "other"):
            ex = _bs(raw, intent="out_of_scope", out_of_scope_reason=reason)
            assert ex.intent != "out_of_scope", (raw, reason, ex.intent)


def test_metar_classified_in_scope_by_the_model_is_still_refused():
    """The other way it leaked: the model never refused at all. needs_live_source is
    what catches it."""
    ex = _bs("current METAR for Kano", intent="aerodrome_fact", needs_live_source=True)
    assert ex.intent == "out_of_scope"
    assert ex.out_of_scope_reason is not None, "a refusal with no reason falls back to the generic string"


def test_step_5_still_rescues_the_static_fields_it_exists_for():
    """The veto must not cost the rescues step 5 was written for."""
    assert _bs("TAF validity at Lagos", intent="national_lookup").intent == "aerodrome_fact"
    assert _bs("ATIS hours at Kano", intent="out_of_scope").intent == "aerodrome_fact"


# --- the live rule runs after EVERY correction --------------------------------

def test_no_rescue_step_can_answer_a_live_question():
    """Seven corrections can turn out_of_scope into an answer. Each is given a
    live question it would otherwise rescue."""
    for raw, intent in [
            ("how many runways at Lagos are open right now", "out_of_scope"),     # step 6
            ("is the holding procedure at Kano active today", "out_of_scope"),    # step 6b
            ("what city is DNAA and is it open now", "out_of_scope"),             # step 3
            ("is it open now?", "general_greeting"),                              # step 7
            ("is runway 04 at Lagos usable right now", "out_of_scope")]:          # step 6c
        ex = _bs(raw, intent=intent, needs_live_source=True)
        assert ex.intent == "out_of_scope", (raw, ex.intent)


def test_live_rule_survives_an_early_return_from_the_corrections():
    """_apply_corrections returns early for a recognised direct routing. The
    rule is a wrapper precisely so that return cannot skip it."""
    import agent
    original = agent._apply_corrections
    agent._apply_corrections = lambda ex, raw: ex         # simulate the early exit
    try:
        ex = agent._backstop(_ex(intent="airspace_lookup", needs_live_source=True), "ABC to NANOS now?")
        assert ex.intent == "out_of_scope"
    finally:
        agent._apply_corrections = original


def test_live_rule_keeps_a_more_fundamental_reason():
    """'Is Accra airport open?' is live AND foreign. The foreign reason must
    survive, or the reply sends the pilot to Nigerian NOTAMs about Ghana."""
    ex = _bs("is Accra airport open", intent="out_of_scope",
             out_of_scope_reason="outside_nigeria", needs_live_source=True)
    assert ex.intent == "out_of_scope" and ex.out_of_scope_reason == "outside_nigeria"
    ex = _bs("current price of Jet A-1 at Lagos", intent="out_of_scope",
             out_of_scope_reason="charges", needs_live_source=True)
    assert ex.out_of_scope_reason == "charges"


def test_standing_rule_with_live_sounding_wording_is_not_refused():
    """AD 2.22 publishes the ATC clearance rule for VFR flights. 'Clearance'
    sounds live; the rule is not. When the model judges it lasting, nothing in
    the backstop may refuse it."""
    ex = _bs("what are the ATC clearance requirements for VFR flights at Lagos",
             intent="procedure_lookup", needs_live_source=False)
    assert ex.intent != "out_of_scope"


def test_runway_in_use_is_answered_from_ad_2_22_2():
    """The runway in use is published: every aerodrome's AD 2.22.2 carries a
    runway-in-use statement, and that statement is the answer — with or
    without "now" or "today" in the question. It must never be refused."""
    for raw in ("what is the runway in use for Kano",
                "which runway is in use at Lagos today",
                "what runway are they using at Lagos now"):
        ex = _bs(raw, intent="aerodrome_fact", needs_live_source=False,
                 ad2_subsection="2.22")
        assert ex.intent != "out_of_scope", raw
        # ...and a refusal the model gave without a live reason is rescued.
        ex = _bs(raw, intent="out_of_scope", out_of_scope_reason="other")
        assert ex.intent != "out_of_scope", raw


def test_prompt_states_that_words_do_not_make_a_question_live():
    """GUARD. The prompt and the schema must say, in so many words, that the
    runway in use is published and that a time word does not make a lasting
    fact live. An earlier version said the opposite and the model followed."""
    import agent, schemas, inspect
    assert "WORDS DO NOT MAKE A QUESTION LIVE" in agent._SYSTEM
    assert "The runway in use is IN SCOPE" in agent._SYSTEM
    assert "ALWAYS TIME" not in agent._SYSTEM
    assert "NOT the runway in use" in agent._SYSTEM
    # a bare topic is a request for the published entry, not a live incident
    assert "fuel spillage at Yola" in agent._SYSTEM
    src = inspect.getsource(schemas)
    assert "The runway in use is FALSE" in src
    assert "ALWAYS true" not in src
    assert "time_bound:" not in src
    assert "fuel spillage at Yola" in src


# --- charges ------------------------------------------------------------------

def test_published_charge_is_answered_even_if_the_model_refused():
    ex = _bs("what is the landing fee at Kano", intent="out_of_scope",
             out_of_scope_reason="charges", gen4_charge="aerodrome")
    assert ex.intent == "national_lookup" and ex.gen4_charge == "aerodrome"
    assert ex.out_of_scope_reason is None


def test_unpublished_price_stays_refused():
    ex = _bs("how much does Jet A-1 cost at Lagos", intent="out_of_scope",
             out_of_scope_reason="charges")
    assert ex.intent == "out_of_scope"


def test_foreign_charges_question_is_never_answered_from_gen_4():
    """Nigeria's schedule is not Heathrow's. Answering would be confident and
    about the wrong State."""
    ex = _bs("landing fee at Heathrow", intent="out_of_scope",
             out_of_scope_reason="outside_nigeria", gen4_charge="aerodrome")
    assert ex.intent == "out_of_scope" and ex.gen4_charge is None


def test_current_landing_fee_is_still_a_published_charge():
    """'The current landing fee' asks for the GEN 4.1 schedule. The reply
    states which amendment it is valid for; the word 'current' changes
    nothing."""
    ex = _bs("what is the current landing fee at Kano right now",
             intent="national_lookup", gen4_charge="aerodrome",
             needs_live_source=False)
    assert ex.intent == "national_lookup" and ex.gen4_charge == "aerodrome"


def test_live_wins_over_a_charges_reading():
    """Mechanics only: if the model does judge the answer live, the charges
    route must not answer it."""
    ex = _bs("is the landing fee being waived today", intent="national_lookup",
             gen4_charge="aerodrome", needs_live_source=True)
    assert ex.intent == "out_of_scope" and ex.gen4_charge is None


def test_section_mapping():
    import charges
    assert charges.section_for("aerodrome") == "GEN 4.1"
    assert charges.section_for("air_navigation") == "GEN 4.2"
    assert charges.section_for(None) is None and charges.section_for("x") is None


# A miniature of what get_subsection_text("NATIONAL", "GEN 4.1") returns:
# vectoriser prefix on the page's first chunk, a ~300-character overlap between
# consecutive chunks, chunks joined with "\n", page chrome left in.
_OVERLAP = ("No reduction of landing fees will be allowed by reason of the "
            "temporary unavailability of any airport services, assistance or "
            "other facilities.")
_STORED_41 = (
    "[Nigerian AIP 2026 | GEN | GEN 4.1 — Aerodrome Charges]\n\n"
    "GEN 4.1-1 30 OCT 25 NIGERIA AIP 4.1.1 General 4.1.1.1 The charges set out "
    "here-under are common to all airports managed by the Federal Airports "
    "Authority of Nigeria (FAAN). 4.1.3 Landing charges 4.1.3.1 The fees for "
    "the landing of aircraft will be assessed on the basis of the maximum All "
    "Up-Weight (AUW). " + _OVERLAP + "\n"
    + _OVERLAP + " 4.1.3.2 Standard charges will be assessed for all commercial "
    "international air transport services: Day landing – $0.00909 per kg of "
    "AUW Night landing – $0.01364 per kg of AUW 4.1.3.3 Standard charges will "
    "be assessed for commercial domestic air transport services: Day landing – "
    "N0.25/kg Night landing – N0.375/kg 4.1.4 Parking charges 4.1.4.1 Parking "
    "– $0.00114 per hour for each kg aircraft weight. NIGERIAN AIRSPACE "
    "MANAGEMENT AGENCY AIRAC AMDT 02/2025")


def test_charges_reply_shows_the_asked_block_once_and_verbatim():
    import charges
    out = charges.charges_reply("GEN 4.1", _STORED_41, "what is the landing fee at Kano")
    assert "4.1.3 Landing charges" in out
    assert "Day landing – $0.00909 per kg of AUW" in out          # international
    assert "Day landing – N0.25/kg" in out                         # domestic
    assert out.count("No reduction of landing fees") == 1, \
        "chunk overlap leaked: a repeated line in a fee schedule reads as two rates"
    assert "NIGERIAN AIRSPACE MANAGEMENT AGENCY" not in out, "page chrome left in the schedule"
    assert out.count("AIRAC AMDT 02/2025") == 1, "the stamp belongs in the caveat only"


def test_charges_reply_states_amendment_and_who_to_ask():
    import charges, config
    out = charges.charges_reply("GEN 4.1", _STORED_41, "landing fee")
    assert "AIRAC AMDT 02/2025" in out
    assert "valid for that amendment" in out
    assert "FAAN" in out and "NAMA" in out
    assert config.DISCLAIMER in out
    out42 = charges.charges_reply("GEN 4.2", "4.2.2 Air Navigation Service Charges x", "tnc")
    assert "NAMA" in out42 and "AIRAC AMDT 02/2017" in out42


def test_charges_reply_says_the_schedule_is_national_when_an_aerodrome_is_named():
    import charges
    out = charges.charges_reply("GEN 4.1", _STORED_41, "landing fee at Kano",
                                aerodrome_named=True)
    assert "not a separate schedule per aerodrome" in out


def test_charges_reply_never_quotes_a_rate_it_could_not_fetch():
    import charges
    out = charges.charges_reply("GEN 4.1", "", "landing fee")
    assert "won't quote a rate" in out
    assert "$" not in out.split("———")[0] and "N0." not in out


def test_unmatched_question_shows_the_whole_subsection():
    """A poor match must show MORE, never a different block."""
    import charges
    out = charges.charges_reply("GEN 4.1", _STORED_41, "zzzz")
    assert "4.1.1 General" in out and "4.1.3 Landing charges" in out \
        and "4.1.4 Parking charges" in out


def test_stored_amendment_stamp_wins_over_a_stale_table():
    """A re-ingested AIP must not be labelled with last cycle's amendment."""
    import charges
    newer = _STORED_41.replace("02/2025", "01/2027")
    assert "AIRAC AMDT 01/2027" in charges.charges_reply("GEN 4.1", newer, "landing fee")


def test_weight_factor_formula_is_restored_and_flagged():
    """GEN 4.2.2.1: P = √(MTOW / 50). The root and fraction bar are graphics,
    so the stored text reads 'P = Maximum take-off mass (Tons) 50'."""
    import charges
    stored = ("4.2.2 Air Navigation Service Charges 4.2.2.1 En-route Charge R = T x D x P "
              "P = Maximum take-off mass (Tons) 50 International flights: $70 per unit.")
    out = charges.charges_reply("GEN 4.2", stored, "en-route charge")
    assert "√( Maximum take-off mass (Tons) ÷ 50 )" in out
    assert "restored from the AIP page" in out
    # and it never fires on anything else
    assert "√" not in charges.charges_reply("GEN 4.1", _STORED_41, "landing fee")


# --- wiring guards (main.py's request path is not executed offline) -----------

def test_main_refuses_live_questions_before_toc_and_enumeration():
    import inspect, main
    src = inspect.getsource(main.process)
    live = src.find("if asks_live(ex):")
    assert live != -1, "no live gate in main.process"
    assert live < src.find("facts.is_ta_enumeration(text)"), \
        "the TA enumeration can answer a live question before the gate"
    assert live < src.find("toc.is_structure_question(text)"), \
        "the ToC can answer a live question before the gate"


def test_main_serves_charges_by_section():
    import inspect, main
    src = inspect.getsource(main.process)
    assert "charges.section_for(" in src and "charges.charges_reply(" in src
    assert 'get_subsection_text, "NATIONAL"' in src, \
        "charges must be fetched by section, never left to vector search"


def test_schema_asks_about_liveness_before_the_reason():
    """Structured output is generated in field order. The reason must come
    after needs_live_source, or a live question the model classified as
    in-scope has no reason by the time it decides it is live."""
    import schemas
    fields = list(schemas.AIPQueryExtraction.model_fields) \
        if hasattr(schemas.AIPQueryExtraction, "model_fields") \
        else list(schemas.AIPQueryExtraction.__annotations__)
    assert fields.index("intent") < fields.index("needs_live_source") \
        < fields.index("out_of_scope_reason") < fields.index("gen4_charge")


def test_measure_intent_controls_match_the_policy():
    import measure_intent as m
    assert "what is the landing fee at Kano" not in m.OUT_OF_SCOPE_CONTROLS, \
        "GEN 4.1 publishes landing fees — refusing them is the bug, not the control"
    assert "what is the landing fee at Kano" in m.BOUNDARY_IN_SCOPE
    assert "how much does Jet A-1 cost at Lagos" in m.OUT_OF_SCOPE_CONTROLS
    # Lasting facts asked with a live-sounding word are BOUNDARY, never controls.
    for q in ("what is the runway in use for Kano",
              "which runway is in use at Lagos today",
              "what runway are they using at Lagos",
              "what is Lagos tower frequency now",
              "Lagos tower hours today"):
        assert q in m.BOUNDARY_IN_SCOPE, q
        assert q not in m.OUT_OF_SCOPE_CONTROLS, q
    # The present state of a unit is live; its published hours are not.
    assert "is Lagos tower open now" in m.OUT_OF_SCOPE_CONTROLS
    assert not hasattr(m, "_LIVE_BY_POLICY_LABELS"), \
        "the 95 'Runway in use' stress rows are published and must be sampled"
