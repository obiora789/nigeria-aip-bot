"""
test_chart_titles.py — SID and STAR charts are never filed as approaches.

Measured 2026-11-21 (diagnose_chart_titles.py, AMDT 03/2026): 17 SID/STAR
pages were stored as 'RNAV Approach Chart' because "(RNAV|GNSS|RNP) ... RWY nn"
was tried first. The titles below are real (top of each page), completed
where the diagnostic output cut them at 110 characters.

No PDF, no network. Run with:  pytest tests/test_chart_titles.py -q
"""
import os

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

from chart_titles import classify_title  # noqa: E402

SIDS = {  # page: (title, runway expected)
    "DNAA p367": ("ABUJA/Nnamdi Azikiwe RNAV1 (GNSS) RWY 04 IKSEM 1S, IKVAD 1S,OXORU 1S "
                  "STANDARD INSTRUMENT DEPARTURE (SID) - ICAO", "04"),
    "DNAA p368": ("ABUJA/Nnamdi Azikiwe RNAV1 (GNSS) RWY 22 IKSEM 1Q, IKVAD 1Q,OXORU 1Q "
                  "STANDARD INSTRUMENT DEPARTURE (SID) - ICAO", "22"),
    "DNBE p482": ("BENIN/Benin (DNBE) STANDARD DEPARTURE CHART INSTRUMENT (SID) - ICAO "
                  "TRANSITION ALTITUDE 3500 FT RWY 05", "05"),
    "DNBY p516": ("AMASSOMA/Bayelsa Airport STANDARD DEPARTURE CHART - ICAO INSTRUMENT (SID)", ""),
    "DNKN p768": ("TRANSITIONAL ALTITUDE KANO/Mallam Aminu Kano STANDARD DEPARTURE CHART - "
                  "ICAO INSTRUMENT (SID) 5000", ""),
    "DNKS p796": ("07° 00'N STANDARD INSTRUMENT DEPARTURE (SID) - ICAO CONTOUR LEG", ""),
    # how the AD 2.24 index names them — the text the stored labels came from
    "index SID": ("Standard Instrument Departure Chart - ICAO RNAV1 (GNSS) RWY 04", "04"),
}
STARS = {
    "DNAA p361": ("ABUJA/Nnamdi Azikiwe RNAV1 (GNSS) RWY 04 STANDARD INSTRUMENT ARRIVAL "
                  "(STAR) - ICAO", "04"),
    "DNBE p484": ("BENIN/Benin (DNBE) RNAV (GNSS) RWY 05 / 23 BOTBO 1B, BOTBO 1A STANDARD "
                  "ARRIVAL CHART INSTRUMENT (STAR) - ICAO", "05"),
    "DNKN p771": ("TRANSITIONAL ALTITUDE KANO/Mallam Aminu Kano STANDARD ARRIVAL CHART - "
                  "ICAO INSTRUMENT (STAR) 5000FT", ""),
    "DNKS p797": ("STANDARD INSTRUMENT ARRIVAL (STAR) - ICAO AKLIS FL80", ""),
    "index STAR": ("Standard Arrival Chart - Instrument (STAR) - ICAO RNAV (GNSS) RWY 05 / 23", "05"),
}
APPROACHES = {  # unchanged by the fix
    "DNAK p396": ("INSTRUMENT APPROACH CHART - ICAO AKURE/Akure (DNAK) AERODROME ELEV 1108 FT "
                  "RNAV (GNSS) RWY 03", ("RNAV Approach Chart", "03")),
    "DNKN p777": ("KANO/Mallam Aminu Kano RNP RWY 06 (LNAV VNAV) APPROACH CHART",
                  ("RNAV Approach Chart", "06")),
    "DNIL p684": ("INSTRUMENT APPROACH CHART - ICAO ILORIN/Ilorin (DNIL) RNAV (GNSS) RWY 05",
                  ("RNAV Approach Chart", "05")),
    "index ILS": ("Instrument Approach Chart - ICAO ILS/DME RWY 18L", ("ILS Approach Chart", "18L")),
    "index VOR": ("Instrument Approach Chart - ICAO VOR/DME RWY 22", ("VOR Approach Chart", "22")),
}


def test_sid_pages_are_sid_charts_with_their_runway():
    for page, (title, rwy) in SIDS.items():
        assert classify_title(title) == ("SID Chart", rwy), page


def test_star_pages_are_star_charts_with_their_runway():
    for page, (title, rwy) in STARS.items():
        assert classify_title(title) == ("STAR Chart", rwy), page


def test_real_approaches_are_unchanged():
    for page, (title, want) in APPROACHES.items():
        assert classify_title(title) == want, page


def test_area_departure_and_arrival_charts_are_not_sids_or_stars():
    assert classify_title("AREA CHART - ICAO DEPARTURE AND TRANSIT ROUTES") == \
        ("Area Chart - Departure and Transit Routes", "")
    assert classify_title("AREA CHART - ICAO ARRIVAL AND TRANSIT ROUTES") == \
        ("Area Chart - Arrival and Transit Routes", "")


def test_extract_charts_uses_these_rules():
    # Found on the import path, not next to this file: the test and
    # extract_charts.py live in different folders after reorganise.py.
    import importlib.util
    src = open(importlib.util.find_spec("extract_charts").origin).read()
    assert "from chart_titles import TITLE_PATTERNS, classify_title" in src
    assert src.count("classify_title(") >= 3, "index names and both title passes"


def test_a_sid_chart_request_matches_sid_charts_not_approaches():
    import database
    assert database.chart_matches("SID", "SID Chart")
    assert not database.chart_matches("SID", "RNAV Approach Chart")
    assert not database.chart_matches("RNAV", "SID Chart")
    assert database.chart_matches("STAR", "STAR Chart")
