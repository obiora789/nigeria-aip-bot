"""
test_procedures_typo.py — an AIP punctuation typo must not hide a complete
procedure; a genuinely cut-off one must still be refused.

No network. Run with:  pytest tests/test_procedures_typo.py -q

Confirmed live (2026-10-21): "What is the ILS approach in Kano" -> RWY 06 gave
only the plate. why_no_procedure.py showed the holding text WAS stored; the
dangling-comma check rejected it because the AIP prints "(14000 ft),." and the
full stop was stripped before the check ran.
"""
import os

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

# Header, holding and letdown are DNKN's stored AD 2.22 text as printed by
# why_no_procedure.py, including its ",." typo. The missed-approach sentence
# is a fixture (the output was truncated before it).
_DNKN_06 = (
    "2.22.3.1 Instrument approach procedures for RWY 06 base d on VOR ILS/DME "
    "(CAT I, II). 2.22.3.1.1 Holding procedure – Initial approach altitude "
    "4 000ft QNH. – left hand pattern; – inbound heading 062°M; – outbound "
    "heading 242°M. Rate one turn, 1 (one) minute leg at or below FL 140 "
    "(14000 ft), 1½ (one and half) minutes leg above FL 140 (14000 ft),. "
    "2.22.3.1.2 Letdown procedure Descend within the race track pattern. On "
    "the outbound heading 242°M, descend to 3200ft QNH. At 7NM KAN DME turn "
    "left to intercept the IKN Localizer, at 4.3NM KAN DME (5NM IKN DME) "
    "descend to DA/H. 2.22.3.1.3 Missed approach procedure On missed "
    "approach, maintain runway heading passing 3000ft, turn left to join the "
    "hold at KAN VOR at 4000ft or as instructed by ATC. "
)


def test_kano_rwy06_ils_holding_survives_the_aip_typo():
    import procedures
    r = procedures.extract(_DNKN_06, "06", "ILS")
    assert r is not None, "a complete procedure was refused over ',.'"
    assert set(r["sections"]) == {"Holding", "Letdown", "Missed Approach"}
    assert "inbound heading 062°M" in r["sections"]["Holding"]
    assert "(14000 ft)" in r["sections"]["Holding"]


def test_a_genuinely_cut_off_section_is_still_refused():
    """DNET's real failure: the letdown ends mid-sentence on a comma because an
    interleaved fragment took the rest. That must still go to the plate."""
    import procedures
    cut = _DNKN_06.replace(
        "descend to DA/H. 2.22.3.1.3",
        "On the outbound track 032°M, 2.22.3.1.3")
    assert procedures.extract(cut, "06", "ILS") is None


def test_the_table_row_signature_is_unchanged():
    import procedures
    assert procedures._looks_corrupted("join the hold 05/ Circl 2610(540) A 1700 1800")
    assert not procedures._looks_corrupted("join the hold at 4000ft.")
