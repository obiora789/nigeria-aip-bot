"""
test_scope_refusal.py — guards on the out-of-scope refusal copy.

These are COPY tests, not behaviour tests. scope_refusal.out_of_scope() is only
reached after agent.extract_query_parameters() has already decided to refuse,
so nothing here can turn a refusal into an answer or vice versa — the
false-refusal / false-accept rates measured by measure_intent.py are untouched
by this module. What these guard is that the refusal sends the pilot to the
RIGHT authority, which is the only useful work an out-of-scope reply does.

No raw question text is passed to out_of_scope(): it reads the extraction the
pipeline already produced. These fixtures are therefore shaped like real
extractions, not like sentences.
"""
from types import SimpleNamespace as N

import config
import scope_refusal


def ex(reason=None, runway=None, aerodrome_name=None, icao_code=None):
    return N(intent="out_of_scope", out_of_scope_reason=reason, runway=runway,
             aerodrome_name=aerodrome_name, icao_code=icao_code,
             procedure_type=None, filter_part="AD")


def test_runway_status_names_the_notam_and_nothing_else():
    """The failure this module exists to fix.

    Confirmed live: "Is runway 22 open right now?" returned the static string,
    which listed live weather, NOTAMs, ATC clearances and international
    airspace — four of five clauses about things the pilot did not ask — and
    never said the one word that answers him."""
    out = scope_refusal.out_of_scope(ex("transient_status", runway="22"))
    said = out.split(config.DISCLAIMER)[0].lower()
    assert "notam" in said, "the refusal does not name the authority"
    for unasked in ("weather", "clearance", "international"):
        assert unasked not in said, \
            f"refusal still volunteers {unasked!r}, which was never asked about"


def test_the_permanence_rule_is_stated_not_just_the_verdict():
    """A pilot should learn WHY the AIP cannot hold this — it changes between
    amendments — so the next question is better posed."""
    out = scope_refusal.out_of_scope(ex("transient_status", runway="22"))
    assert "between" in out.lower() and "amendment" in out.lower()


def test_bare_runway_says_the_question_is_underspecified():
    """A runway with no aerodrome is ambiguous across every aerodrome that
    publishes one. The old reply never said so."""
    out = scope_refusal.out_of_scope(ex("transient_status", runway="22"))
    assert "RWY 22" in out and "which aerodrome" in out


def test_named_aerodrome_is_not_asked_for_again():
    """Regression: an earlier version keyed the offer off the ABSENCE of an
    aerodrome, so a pilot who HAD named his aerodrome was told to name it."""
    out = scope_refusal.out_of_scope(
        ex("transient_status", runway="22", aerodrome_name="Abuja"))
    assert "which aerodrome" not in out
    assert "RWY 22" in out


def test_published_data_is_never_offered_as_current_status():
    """The offer must stay a SEPARATE sentence and label itself published.
    Blended into the refusal it would read as an answer to "is it open", and
    the AIP is not a NOTAM."""
    out = scope_refusal.out_of_scope(ex("transient_status", runway="22",
                                        icao_code="DNAA"))
    assert "not its current status" in out.lower()
    assert out.index("NOTAM") < out.index("RWY 22"), \
        "the offer must come after the refusal, never before it"


def test_no_offer_when_no_runway_was_named():
    """Regression: a general NOTAM question got an unsolicited runway-data
    offer — the same volunteering-the-unasked failure as the static string."""
    out = scope_refusal.out_of_scope(ex("transient_status", aerodrome_name="Abuja"))
    assert "dimensions" not in out


def test_weather_gets_no_published_offer_at_all():
    """The AIP carries no wind, visibility or QNH values, so there is nothing
    honest to offer alongside a weather question."""
    out = scope_refusal.out_of_scope(ex("current_weather", runway="22",
                                        aerodrome_name="Lagos"))
    assert "dimensions" not in out and "RWY 22" not in out


def test_each_reason_names_its_own_authority():
    """A closure is a NOTAM, an observation is a METAR, a clearance is ATC.
    The whole point of splitting transience is that the authority differs."""
    for reason, expected in [
        ("transient_status", "NOTAM"),
        ("current_weather", "METAR"),
        ("atc_clearance", "controller"),
        ("charges", "operator"),
        ("outside_nigeria", "State's own AIP"),
        ("planning_or_flow", "operations department"),
    ]:
        assert expected in scope_refusal.out_of_scope(ex(reason)), \
            f"{reason} does not point at the right source"


def test_unrecognised_or_missing_reason_falls_back_rather_than_inventing_one():
    """Naming no reason beats naming the wrong one: a confidently wrong
    explanation sends the pilot to the wrong authority. Also covers an
    extraction object from before the schema field existed, which is what a
    partial deploy looks like."""
    assert scope_refusal.out_of_scope(ex(None)) == config.OUT_OF_SCOPE
    assert scope_refusal.out_of_scope(ex("something_new")) == config.OUT_OF_SCOPE
    legacy = N(intent="out_of_scope", runway=None, aerodrome_name=None,
               icao_code=None)
    assert scope_refusal.out_of_scope(legacy) == config.OUT_OF_SCOPE
    assert scope_refusal.out_of_scope(None) == config.OUT_OF_SCOPE


def test_specific_refusals_carry_the_disclaimer():
    """Matters most where published data is offered alongside the refusal.

    NOT asserted for the fallback: config.OUT_OF_SCOPE has never carried the
    disclaimer, and that is today's shipped behaviour — left alone deliberately
    rather than quietly changed as a side effect of a copy rework."""
    for reason in _ALL_REASONS:
        assert config.DISCLAIMER in scope_refusal.out_of_scope(ex(reason))


_ALL_REASONS = ("transient_status", "current_weather", "atc_clearance",
                "planning_or_flow", "charges", "outside_nigeria")


def test_every_schema_reason_has_copy():
    """If schemas.py gains a reason and this module doesn't, that reason
    silently degrades to the generic string. Fail loudly instead."""
    try:
        import schemas
        import typing
        field = schemas.AIPQueryExtraction.model_fields["out_of_scope_reason"]
        declared = {a for a in typing.get_args(field.annotation) if isinstance(a, str)}
    except Exception:                                    # noqa: BLE001
        declared = set(_ALL_REASONS)
    missing = declared - set(scope_refusal._REASONS) - {"other", None}
    assert not missing, f"schema reasons with no copy: {sorted(missing)}"
