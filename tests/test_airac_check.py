"""airac_check (car 3): each check passes on pages shaped like the current AIP
and fails on the change a new amendment would bring. Synthetic pages: no PDF,
no network."""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ingestion"))

import airac_check as ac                                     # noqa: E402
from aip_structure import AERODROMES                         # noqa: E402


def _pages():
    """1,073 pages; every aerodrome/heliport page headed with its own reference,
    as the AIP prints it; Zaria's range holding the DNXX catalogue and a blank."""
    pages = [""] * 1073
    for icao, _name, start, end in AERODROMES:
        if icao in ac._CHECKED:
            part = "3" if icao in ac.HELIPORT_ICAO else "2"
            for k, p in enumerate(range(start, end + 1)):
                pages[p - 1] = f"AD {part}-{icao}-{k + 1}\r\nNIGERIA AIP\r\n"
    pages[1023], pages[1024] = "AD 2-DNXX-1\r\nNIGERIA AIP\r\n", "INTENTIONALLY BLANK"
    return pages


def test_boundaries_pass_as_printed_and_fail_when_pages_shift():
    pages = _pages()
    assert ac.check_boundaries(pages)[0]
    shifted = pages[:333] + ["INTENTIONALLY BLANK"] + pages[333:-1]   # one page inserted before Abuja
    ok, lines = ac.check_boundaries(shifted)
    assert not ok and lines[0].startswith("DNAA") and "[335]" in lines[0]


def test_another_aerodromes_page_inside_a_range_fails():
    pages = _pages()
    pages[1023] = "AD 2-DNKN-1\r\nNIGERIA AIP\r\n"          # not the DNXX catalogue
    ok, lines = ac.check_boundaries(pages)
    assert not ok and any("DNZA" in ln and "AD 2-DNKN-1" in ln for ln in lines)


def test_amendment_names_the_new_cycle_and_the_pages_it_changed():
    pages = _pages()
    for p in (334, 335, 336):
        pages[p - 1] += "AIRAC AMDT 03/2026"
    ok, lines, newest = ac.check_amendment(pages, "AIRAC AMDT 03/2026")
    assert ok and newest == "AIRAC AMDT 03/2026"
    pages[340] += "16 JAN 27 AIRAC AMDT 01/2027"
    ok, lines, newest = ac.check_amendment(pages, "AIRAC AMDT 03/2026")
    assert not ok and newest == "AIRAC AMDT 01/2027"
    assert any(ln.strip().startswith("DNAA") and "341" in ln for ln in lines)


def test_gold_rows_fail_when_the_printed_value_changes(tmp_path):
    gold = tmp_path / "gold.csv"
    gold.write_text(
        "id,kind,question,must_contain,must_not_contain,section,page,source_quote,verified,notes\n"
        "G-1,answer,q,CAT 9,,AD 2.6,AD 2-DNAA-2,,pdf,\n"
        "G-2,answer,q,1600 m,,AD 2.13,AD 2-DNAA-3,,pdf,\n"
        "G-3,answer,q,115352N | 0041421E,,ENR 4.4,ENR 4.4-5,,pdf,\n"
        "G-4,refuse_live,q,,,,,,yes,\n", encoding="utf-8")
    pages = _pages()
    pages[334] += "1 AD category for fire fighting CAT 9"
    pages[335] += "TORA\r\n(m)\r\n13 1600 1600 1600 1600"        # unit only in the heading
    pages[266] = "ENR 4.4-5\r\nUSLAR 115352N 0041421E UL684"
    ok, lines = ac.check_gold(pages, str(gold))
    assert ok and lines[0] == "3/3 gold rows still match their page"
    pages[334] = pages[334].replace("CAT 9", "CAT 7")
    ok, lines = ac.check_gold(pages, str(gold))
    assert not ok and "G-1" in lines[1] and "CAT 9" in lines[1]
    assert ac._on_page("1600 m", ac.squash("1600 m")) and not ac._on_page("1600 m", ac.squash("1600"))


def test_span22_drift_is_reported(tmp_path):
    pages = _pages()
    pages[339] += "DNAA AD 2.22 FLIGHT PROCEDURES"
    pages[350] += "DNAA AD 2.23 ADDITIONAL INFORMATION"
    span = tmp_path / "span.json"
    span.write_text(json.dumps({"DNAA": {"s22": 340, "end22": 351}}))
    assert ac.check_span22(pages, str(span))[0]
    span.write_text(json.dumps({"DNAA": {"s22": 341, "end22": 351}}))
    ok, lines = ac.check_span22(pages, str(span))
    assert not ok and lines[0].startswith("DNAA")


def test_the_prompt_navaid_list_is_read_without_importing_agent():
    navaids = ac.prompt_navaids(str(ROOT / "agent.py"))
    assert len(navaids) == 41 and ("GB", "VOR/DME", "Gboko") in navaids
