"""AD 2.2 and per-runway-end answers come from the structured records.
Gold run 2026-11-25: reference temperature / ARP answered with the elevation
line, threshold elevation never shown. No network."""
from models import Resolution

RES = Resolution(icao="DNKN", label="Kano")
KANO_RWY = [
    {"designation": "06/24", "length_m": 3300, "width_m": 60,
     "end_detail": {"06": "06 061°08’ 3 300 x 60 THR 475.61 m/1 560.39 ft",
                    "24": "24 241°08’ 3 300 x 60 THR 458.92 m/1 505.63 ft"}},
    {"designation": "05/23", "length_m": 2450, "width_m": 45,
     "end_detail": {"05": "05 ... THR 470 m/1 542 ft", "23": "23 ... THR 466 m/1 529 ft"}},
]
LAGOS_RWY = [{"designation": "18L/36R", "length_m": 2745, "width_m": 45,
              "end_detail": {"18L": "18L THR 30.1 m", "36R": "36R THR 26.45.m"}},
             {"designation": "18R/36L", "length_m": 3900, "width_m": 60,
              "end_detail": {"18R": "18R THR 19.56 m", "36L": "36L THR 15.28 m"}}]


def test_one_runway_end_shows_only_that_end():
    from responder import runway_end_reply
    out = runway_end_reply(RES, KANO_RWY, "RWY 6")
    assert "475.61 m" in out and "458.92" not in out and "470 m" not in out


def test_parallel_runways_never_cross_sides():
    from responder import runway_end_reply
    lag = Resolution(icao="DNMM", label="Lagos")
    out = runway_end_reply(lag, LAGOS_RWY, "18R")
    assert "19.56" in out and "30.1" not in out
    both = runway_end_reply(lag, LAGOS_RWY, "18")        # unsided: every end, labelled
    assert "19.56" in both and "30.1" in both and "[18L]" in both and "[18R]" in both


def test_no_runway_named_shows_every_end_labelled():
    from responder import runway_end_reply
    out = runway_end_reply(RES, KANO_RWY, None)
    assert "[06]" in out and "[24]" in out and "475.61" in out and "458.92" in out


def test_ad22_reply_shows_every_stored_field():
    from responder import ad22_reply
    rec = {"arp_site_description": "120236N 0083126E. Centre of RWY 06/24",
           "elevation_m": 476.0, "elevation_ft": 1562.0, "reference_temp_c": 33.1,
           "magnetic_variation": "1° W (2025)", "traffic_type": "IFR/VFR"}
    out = ad22_reply(RES, rec)
    for s in ("120236N 0083126E", "476 m / 1562 ft", "33.1 °C", "1° W (2025)", "IFR/VFR"):
        assert s in out, s
    assert "AD 2.2" in out
    assert "Not in my stored copy" in ad22_reply(RES, {})


def test_ad26_reply_shows_the_category_as_printed():
    from responder import ad26_reply
    out = ad26_reply(RES, {"rff_category": 9, "rff_category_raw": "CAT 9",
                           "rescue_equipment": "3 crash tenders", "remarks": "NIL"})
    assert "AD category for fire fighting: CAT 9" in out and "3 crash tenders" in out
    assert "category 9" not in out.replace("fire fighting: CAT 9", "")
    assert "Not in my stored copy" in ad26_reply(RES, {})


def test_main_routes_these_to_the_structured_records():
    import inspect, main
    src = inspect.getsource(main.process)
    assert "runway_end_reply(res, rrecs, ex.runway" in src
    assert 'get_structured, res.icao, "2.2"' in src and "ad22_reply(res, ad22[0])" in src
    assert 'get_structured, res.icao, "2.6"' in src and "ad26_reply(res, ad26[0])" in src
