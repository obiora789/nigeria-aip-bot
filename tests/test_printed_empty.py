"""A subsection the AIP prints with no entries is stored as printed, not
failed (car 3; found by rebuild_airac.sh check on 26 Nov: DNAK/DNFD/DNKS
AD 2.19, DNBB AD 2.13, DNFB AD 2.20). Anything else under the headings
still fails. Synthetic segments built from the real pages' words."""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ingestion"))

from segment_page import Segment, printed_empty          # noqa: E402

HEAD_219 = ["DNAK AD 2.19 RADIO NAVIGATION AND LANDING AIDS",
            "Type of aid, MAG ID Frequency Hours of Site of Elevation of Service Remarks",
            "VAR, Type of Operation transmitting DME volume",
            "1 2 3 4 5 6 7 8"]


def _seg(lines, sub="2.19", page=389, title="RADIO NAVIGATION AND LANDING AIDS"):
    words = []
    for row, line in enumerate(lines):
        x = 60.0
        for tok in line.split():
            words.append((x, 100.0 + 14 * row, x + 6 * len(tok), 108.0 + 14 * row, tok))
            x += 6 * len(tok) + 4
    return Segment("DNAK", sub, title, page, words, False, False)


def test_what_counts_as_printed_empty():
    assert printed_empty([_seg(HEAD_219)]) == ""                                 # DNAK, DNFD
    assert printed_empty([_seg(HEAD_219 + ["NIL NIL NIL NIL NIL NIL NIL NIL"])]) == "NIL"   # DNKS
    assert printed_empty([_seg(["DNFB AD 2.20 LOCAL TRAFFIC REGULATIONS"], "2.20", 616),
                          _seg(["To be developed."], "2.20", 617)]) == "To be developed."  # DNFB
    # A real row, or headings with no column-number row, is NOT empty.
    assert printed_empty([_seg(HEAD_219 + ["VOR/DME AK 114.3 MHz H24"])]) is None
    assert printed_empty([_seg(HEAD_219[:3])]) is None


def test_validate_warns_for_printed_empty_and_still_errors_otherwise():
    from ad219_extractor import AD219Extractor
    ex = AD219Extractor()
    r = ex.extract("DNAK", [_seg(HEAD_219)])
    issues = ex.validate(r)
    assert r.records == [] and r.empty_as_printed == ""
    assert [i.severity for i in issues] == ["warning"]
    assert r.text.startswith("DNAK AD 2.19 RADIO NAVIGATION AND LANDING AIDS\n")
    nil = ex.extract("DNKS", [_seg(HEAD_219 + ["NIL NIL NIL NIL NIL NIL NIL NIL"])])
    assert nil.text.endswith("\nNIL")
    garbled = ex.extract("DNAK", [_seg(HEAD_219[:3] + ["something unreadable"])])
    assert [i.severity for i in ex.validate(garbled)] == ["error"]


def test_ad213_and_ad220_use_the_same_rule():
    from ad213_extractor import AD213Extractor
    from ad220_extractor import AD220Extractor
    dd = AD213Extractor()
    r = dd.extract("DNBB", [_seg(["DNBB AD 2.13 DECLARED DISTANCES",
                                  "RWY TORA TODA ASDA LDA Remarks", "Designator",
                                  "(m) (m) (m) (m)", "1 2 3 4 5 6"], "2.13", 437,
                                 "DECLARED DISTANCES")])
    assert r.empty_as_printed == "" and [i.severity for i in dd.validate(r)] == ["warning"]
    lr = AD220Extractor()
    r = lr.extract("DNFB", [_seg(["DNFB AD 2.20 LOCAL TRAFFIC REGULATIONS"], "2.20", 616,
                                 "LOCAL TRAFFIC REGULATIONS"),
                            _seg(["To be developed."], "2.20", 617, "LOCAL TRAFFIC REGULATIONS")])
    assert r.empty_as_printed == "To be developed." and r.text.endswith("To be developed.")
    assert [i.severity for i in lr.validate(r)] == ["warning"]


def test_an_empty_subsection_drops_an_earlier_cycles_records(monkeypatch):
    from test_replace_in_place import FakeSupabase
    import vectorise_aip_v3 as v3
    fake = FakeSupabase()
    monkeypatch.setattr(v3, "supabase", fake)
    fake.db["aip_structured"] = [{"icao_code": "DNAK", "subsection": "2.19",
                                  "record_index": 0, "record": {"aid_type": "VOR/DME"}}]
    assert v3.struct_save("DNAK", "2.19", []) == 0
    assert fake.db["aip_structured"] == []
