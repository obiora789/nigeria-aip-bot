"""
test_safety_batch.py — the defects confirmed by the 2026-11-19 audit, each
locked by a test that executes the code (no source-text checks).

  1. A two-letter navaid ident that is also an English word ("be", "ok",
     "go") replaced the aerodrome the pilot named.
  2. The grounded-answer checker passed a wrong field, a swapped unit and a
     flipped hemisphere.
  3. A failed database read inside the resolver became "I don't have 'X' in
     the Nigerian AIP".
  4. Long replies were cut at 3,800 characters with no marker.

No network. Run with:  pytest tests/test_safety_batch.py -q
"""
import os

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

from types import SimpleNamespace as N  # noqa: E402

# Two-letter ENR 4.1 idents named in resolver.py's own comments, plus BE from
# enr41_extractor.py ("a locator (ident BE, 272 kHz)").
TWO_LETTER = ["AK", "AO", "BA", "BE", "EK", "GB", "GO", "IL", "JJ", "JS", "KC", "MA", "OK", "ZA"]


def _ex(**kw):
    from schemas import AIPQueryExtraction
    base = {k: None for k in AIPQueryExtraction.model_fields}
    base.update(intent="aerodrome_fact", needs_live_source=False)
    base.update(kw)
    return AIPQueryExtraction.model_construct(**base)


class _Published:
    """Install a published-entities set with the two-letter idents."""

    def __enter__(self):
        import database, resolver
        self.saved = (resolver._PUBLISHED_ENTITIES, database.find_aip_scope)
        resolver._PUBLISHED_ENTITIES = {
            **{i: "ENR_NAVAID" for i in TWO_LETTER},
            **{v.upper(): "ENR_NAVAID" for v in resolver.VOR_IDENTS.values()},
            "TEMSA": "ENR_POINT"}
        database.find_aip_scope = lambda n: []
        return self

    def __exit__(self, *e):
        import database, resolver
        resolver._PUBLISHED_ENTITIES, database.find_aip_scope = self.saved
        return False


# ------------------------------------------------- 1. two-letter idents
def test_an_english_word_never_replaces_the_named_aerodrome():
    import agent
    with _Published():
        for q, icao, name in (
                ("What will the transition level be at Kano?", "DNKN", "Kano"),
                ("Is it ok to taxi on TWY A at Lagos", "DNMM", "Lagos"),
                ("go-around procedure at Abuja", "DNAA", "Abuja"),
                ("Ma'am, what is the elevation of Ilorin?", "DNIL", "Ilorin")):
            ex = agent._apply_corrections(_ex(icao_code=icao, aerodrome_name=name), q)
            assert ex.icao_code == icao, (q, ex.icao_code, ex.aerodrome_name)


def test_an_english_word_is_not_taken_as_the_subject_when_none_was_named():
    import agent
    with _Published():
        ex = agent._apply_corrections(_ex(), "is it ok to land with a tailwind?")
        assert ex.aerodrome_name in (None, ""), ex.aerodrome_name


def test_a_two_letter_ident_the_model_named_still_works():
    import agent
    with _Published():
        ex = agent._apply_corrections(_ex(aerodrome_name="BE"), "BE frequency")
        assert ex.aerodrome_name == "BE"


def test_longer_entity_names_still_override_as_before():
    """The measured ENR fix (raw-text entity override) is unchanged for
    names of three or more characters."""
    import agent
    with _Published():
        ex = agent._apply_corrections(_ex(icao_code="DNMM", aerodrome_name="Lagos"),
                                      "where is TEMSA near Lagos")
        assert ex.aerodrome_name == "TEMSA" and ex.icao_code is None


# --------------------------------------------- 2. grounded-answer checker
def _verify(answer, fact_value, what="Transition altitude"):
    from models import AIPResult
    from schemas import GroundedAnswer, GroundedFact
    import synthesize
    excerpt = AIPResult(content=(
        "DNMM AD 2.17 AIR TRAFFIC SERVICES AIRSPACE\n"
        "2 Vertical limits: GND/WATER to 1 500 ft AMSL\n3 Airspace classification: D\n"
        "5 Transition altitude: 3 500 ft/1 067 m AMSL\n"
        "Magnetic variation: 2°W (2020)"),
        similarity=0.9, aip_section="AD 2.17", reference_tag="DNMM")
    ans = GroundedAnswer(answerable=True, answer=answer,
                         facts_used=[GroundedFact(what=what, value=fact_value, source_excerpt=1)])
    return synthesize.verify_grounded_answer(ans, [excerpt])


def test_the_checker_still_passes_a_correct_answer():
    ok, issues = _verify("Lagos transition altitude is 3 500 ft AMSL (AD 2.17).",
                         "3 500 ft/1 067 m AMSL")
    assert ok, issues
    ok, issues = _verify("Magnetic variation is 2°W (2020).", "2°W (2020)", "Magnetic variation")
    assert ok, issues


def test_the_checker_blocks_a_number_from_another_field():
    ok, _ = _verify("Lagos transition altitude is 1 500 ft.", "3 500 ft/1 067 m AMSL")
    assert not ok


def test_the_checker_blocks_a_trivial_fact_unlocking_other_numbers():
    ok, _ = _verify("Transition altitude is 1 067 ft.", "D", "Classification")
    assert not ok


def test_the_checker_blocks_a_swapped_unit():
    ok, issues = _verify("Transition altitude is 3 500 m.", "3 500 ft/1 067 m AMSL")
    assert not ok and any("ft" in i for i in issues), issues


def test_the_checker_blocks_a_flipped_hemisphere():
    ok, _ = _verify("Magnetic variation is 2°E.", "2°W (2020)", "Magnetic variation")
    assert not ok


# ------------------------------------- 3. failed lookups are not "not in AIP"
def test_a_failed_lookup_is_not_answered_as_not_in_the_aip():
    import database, resolver
    saved = (resolver._PUBLISHED_ENTITIES, database.find_aip_scope)
    try:
        resolver._PUBLISHED_ENTITIES = {}

        def down(name):
            raise database.DataUnavailable("dropped")
        database.find_aip_scope = down
        try:
            res = resolver.resolve(_ex(aerodrome_name="TEMSA"))
        except database.DataUnavailable:
            return
        assert "don't have" not in (res.reason or ""), res.reason
        raise AssertionError(f"resolved during an outage: {res}")
    finally:
        resolver._PUBLISHED_ENTITIES, database.find_aip_scope = saved


def test_extraction_passes_a_database_outage_up():
    import agent, database
    real_client, real_backstop = agent.client, agent._backstop

    class C:
        def parse(self, **kw):
            return N(choices=[N(message=N(parsed=_ex(aerodrome_name="TEMSA")))])

    def down(ex, raw):
        raise database.DataUnavailable("dropped")
    agent.client = N(beta=N(chat=N(completions=C())))
    agent._backstop = down
    try:
        agent.extract_query_parameters("where is TEMSA")
        raise AssertionError("returned instead of raising")
    except database.DataUnavailable:
        pass
    finally:
        agent.client, agent._backstop = real_client, real_backstop


# ------------------------------------------------ 4. no silent truncation
def test_long_replies_are_not_cut_off():
    import config, responder
    from models import Resolution
    facts = [{"subsection": "2.10", "entity": f"Obstacle {i}", "label": "Position",
              "fact_value": f"0{i:05d}.00N 00{i:05d}.00E, {i} m"} for i in range(1, 120)]
    out = responder.facts_reply(Resolution(icao="DNMM", label="Lagos (DNMM)"), facts)
    assert "Obstacle 119" in out, "the last fact was cut off"
    assert out.rstrip().endswith(config.DISCLAIMER.rstrip())


# ------------------------------- 5. a named ENR airspace keeps its own record
def _redirect(res, query, scan=None):
    import main, resolver
    from models import Resolution
    calls = []
    saved = (resolver.resolve, resolver.match_name)

    def fake_resolve(ex):
        calls.append(ex)
        return Resolution(icao=ex.icao_code or "DNMM", label="aerodrome")
    resolver.resolve = fake_resolve
    resolver.match_name = lambda q: set(scan or [])
    try:
        out = main._airspace_redirect(
            _ex(intent="airspace_lookup", ad2_subsection="AD 2.17"), res, query)
    finally:
        resolver.resolve, resolver.match_name = saved
    return out, calls


def test_a_named_tma_or_sector_is_not_sent_to_the_aerodrome():
    """Measured 2026-11-20: 54/84 TMA and sector questions went to AD 2.17."""
    from models import Resolution
    for sid, q in (("LAGOS Terminal Control Area (TMA)", "LAGOS TMA vertical limits"),
                   ("KANO EAST SECTOR", "lateral limits of KANO EAST SECTOR")):
        res = Resolution(part="ENR", reference="AIRSPACE", is_national=True,
                         scope_kind="ENR_AIRSPACE", scope_id=sid,
                         aerodrome_hint=sid.split()[0].title())
        out, calls = _redirect(res, q, scan={"DNMM" if "LAGOS" in sid else "DNKN"})
        assert out is None and not calls, (q, out)


def test_a_control_zone_still_goes_to_the_aerodromes_ad217():
    """A CTR has no ENR record of its own: its limits are in AD 2.17."""
    from models import Resolution
    res = Resolution(part="ENR", reference="AIRSPACE", is_national=True,
                     label="Airspace / En-route (ENR)")
    out, _ = _redirect(res, "vertical limits of the kano control zone", scan={"DNKN"})
    assert out is not None and out.icao == "DNKN", out
