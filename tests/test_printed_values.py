"""Car 3: the extractors keep numbers as the AIP prints them, and
refresh_structured only adds fields unless told otherwise. No network, no PDF."""
import pathlib
import sys
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ingestion"))


def test_ad22_keeps_elevation_and_temperature_as_printed():
    from ad22_extractor import _parse_elevation, _parse_elevation_text
    row = "221.810 m (727.714 ft)/33.8° C (JUL)"
    assert _parse_elevation(row) == (221.81, 727.714, 33.8)
    assert _parse_elevation_text(row) == ("221.810", "727.714", "33.8")
    assert _parse_elevation_text("337.578 m (1 107.526 ft)/30.0° C (JUL)") == \
        ("337.578", "1 107.526", "30.0")
    assert _parse_elevation_text("8.32 m/ 27.3 ft") == ("8.32", "27.3", None)  # DNSU: no temperature


def _words(lines):
    """One word tuple (x0, top, x1, bottom, text) per token, one row per line."""
    out = []
    for row, line in enumerate(lines):
        x = 60.0
        for tok in line.split():
            out.append((x, 100.0 + 14 * row, x + 6 * len(tok), 108.0 + 14 * row, tok))
            x += 6 * len(tok) + 4
    return out


def test_ad218_keeps_frequencies_and_lines_as_printed():
    from ad218_extractor import AD218Extractor
    seg = SimpleNamespace(words=_words([
        "TWR Asaba Tower 118.500 MHz 0600 - 1800",
        "119.400 MHz 0600 - 1800",
        "ATIS Asaba Arrival Information 126.600 MHz 0600 - 1800"]),
        page_index=1, excluded=False, subsection="2.18")
    recs = AD218Extractor().extract("DNAS", [seg]).records
    twr, atis = recs
    assert [f["value_text"] for f in twr["frequencies"]] == ["118.500", "119.400"]
    assert [f["value"] for f in twr["frequencies"]] == [118.5, 119.4]
    assert twr["lines"] == ["TWR Asaba Tower 118.500 MHz 0600 - 1800", "119.400 MHz 0600 - 1800"]
    assert atis["lines"] == ["ATIS Asaba Arrival Information 126.600 MHz 0600 - 1800"]


def test_refresh_reports_additions_as_same_and_changes_as_changed():
    from refresh_structured import compare
    old = [{"elevation_m": 221.81}]
    assert compare(old, [{"elevation_m": 221.81, "elevation_m_text": "221.810"}])[0] == "same"
    status, details = compare(old, [{"elevation_m": 221.0}])
    assert status == "CHANGED" and "221.81" in details[0]
    assert compare(old, [{}])[0] == "CHANGED"                 # a dropped field
    assert compare(old, old + old)[0] == "count"
    # Nested: a frequency in a list gaining value_text is still an addition...
    f_old = [{"service": "TWR", "frequencies": [{"value": 118.5}]}]
    f_new = [{"service": "TWR", "lines": ["TWR ..."],
              "frequencies": [{"value": 118.5, "value_text": "118.500"}]}]
    assert compare(f_old, f_new)[0] == "same"
    # ...but a changed or missing frequency is not.
    assert compare(f_old, [{"service": "TWR", "frequencies": [{"value": 118.6}]}])[0] == "CHANGED"
    assert compare(f_old, [{"service": "TWR", "frequencies": []}])[0] == "CHANGED"


def test_fact_index_uses_the_printed_numbers():
    from build_fact_index import facts_from_record
    rec = {"icao": "DNIB", "elevation_m": 221.81, "elevation_ft": 727.714,
           "elevation_m_text": "221.810", "elevation_ft_text": "727.714",
           "reference_temp_c": 30.0, "reference_temp_text": "30.0"}
    facts = {f["label"]: f["fact_value"] for f in facts_from_record("DNIB", "Ibadan", "2.2", rec)}
    assert facts["elevation"] == "221.810 m / 727.714 ft"
    assert facts["reference temp"] == "30.0 °C"
    assert not any("text" in label for label in facts)
    comms = facts_from_record("DNAS", "Asaba", "2.18", {
        "service": "TWR", "frequencies": [{"value": 118.5, "value_text": "118.500", "unit": "MHZ"}]})
    assert comms[0]["fact_value"] == "118.500MHZ"


def test_ad214_cleanup_keeps_the_aips_full_stop():
    """26 Nov check: the cleanup stripped every value's final "." although its
    docstring promised DNAK's "Not available." survives intact."""
    from ad214_extractor import _strip_table_furniture
    from build_fact_index import _strip_ad214_furniture
    header = ("RWY APCH LGT THR LGT VASIS TDZ, RWY Centre RWY edge LGT RWY End SWY LGT "
              "Remarks Designator 1 2 3 4 5 6 7 8 9 10")
    for strip in (_strip_table_furniture, _strip_ad214_furniture):
        assert strip("DNAK AD 2.14 APPROACH AND RUNWAY LIGHTING Not available.") == "Not available."
        assert strip("21 SALS CAT I Note: THR lights RWY 21 U/S.") == "21 SALS CAT I Note: THR lights RWY 21 U/S."
        assert strip(f"DNAN AD 2.14 APPROACH AND RUNWAY LIGHTING {header} 06 24") == "06 24"
        assert strip(f"10 NIL Green White White {header}") == "10 NIL Green White White"


def test_operator_label_is_removed_and_the_value_kept():
    """D32: the label's words are removed line by line; "AFS:" with its colon
    is the address block's own sub-label and stays."""
    from ad22_extractor import _strip_operator_label
    umueri = [
        "Name of aerodrome operator, address, telephone, Chinua Achebe Cargo Airport".split(),
        "telefax numbers, email address, AFS address and, if Umueri".split(),
        "available, website address".split(),
        "Anambra State".split(), "AFS:".split()]
    assert _strip_operator_label(umueri) == [
        "Chinua Achebe Cargo Airport", "Umueri", "Anambra State", "AFS:"]
    katsina = [
        "AD Administration, address, telephone, telefax, telex, Postal address: Federal".split(),
        "AFS Katsina Airport".split(), "AFTN: DNKTYDYX".split()]
    assert _strip_operator_label(katsina) == [
        "Postal address: Federal", "Katsina Airport", "AFTN: DNKTYDYX"]
    assert _strip_operator_label([["Some", "other", "label", "value"]]) is None


def test_full_row_lines_keep_each_line_left_to_right():
    from extractor_base import SubsectionExtractor
    words = [(70, 100, 76, 108, "6"), (103, 100, 130, 108, "Name"), (309, 100, 340, 108, "Value"),
             (103, 114, 130, 122, "more"), (309, 114, 340, 122, "line2"),
             (70, 140, 76, 148, "7"), (103, 140, 130, 148, "Next")]
    rows = SubsectionExtractor.parse_full_row_lines(words)
    assert [[w[4] for w in ln] for ln in rows[0]] == [["Name", "Value"], ["more", "line2"]]
    assert [[w[4] for w in ln] for ln in rows[1]] == [["Next"]]
