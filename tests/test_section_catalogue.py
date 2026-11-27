"""The AD 2 section list the extraction model reads is DATA about the AIP: each
entry must say what that section's printed rows contain. Gold 27 Nov: all 11
taxiway-lighting questions went to AD 2.14 because 2.14 listed bare
"centreline/edge" lights and 2.15 did not mention taxiways, while the AIP
prints taxiway lighting in AD 2.15 row 3 ("TWY edge and centre line lighting")."""
from schemas import AIPQueryExtraction


def _catalogue():
    return AIPQueryExtraction.model_fields["ad2_subsection"].description


def _entry(sub):
    text = _catalogue()
    start = text.index(f"| {sub} ")
    return text[start:text.index("|", start + 2)]


def test_taxiway_lighting_is_listed_under_2_15_not_2_14():
    assert "TAXIWAY edge and centre line lighting" in _entry("2.15")
    assert "taxiway" not in _entry("2.14").lower()
    assert "secondary power" in _entry("2.15") and "ABN/IBN" in _entry("2.15")


def test_runway_lights_in_2_14_say_runway():
    e = _entry("2.14")
    for lights in ("runway centre line", "runway edge", "runway end", "PAPI"):
        assert lights in e
