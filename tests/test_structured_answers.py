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


def test_ad22_reply_shows_numbers_as_printed():
    """Gold 2026-11-26: 221.810 shown as 221.81 and 30.0° as 30 (D7)."""
    from responder import ad22_reply, overview_reply
    rec = {"elevation_m": 221.81, "elevation_ft": 727.714, "reference_temp_c": 30.0,
           "geoid_undulation_m": 27.9, "elevation_m_text": "221.810",
           "elevation_ft_text": "727.714", "reference_temp_text": "30.0",
           "geoid_undulation_text": "27.9"}
    out = ad22_reply(RES, rec)
    assert "221.810 m / 727.714 ft" in out and "30.0 °C" in out and "27.9 m" in out
    assert "221.810 m" in overview_reply(RES, [rec], [], [])
    # A record ingested before car 3 has no _text fields: the plain number.
    old = {k: v for k, v in rec.items() if not k.endswith("_text")}
    assert "221.81 m" in ad22_reply(RES, old) and "30 °C" in ad22_reply(RES, old)


ASABA_218 = [
    {"service": "TWR", "raw_text": "TWR Asaba Tower 118.500 MHz 0600 - 1800 119.400 MHz 0600 - 1800",
     "lines": ["TWR Asaba Tower 118.500 MHz 0600 - 1800", "119.400 MHz 0600 - 1800"],
     "frequencies": [{"value": 118.5, "value_text": "118.500", "unit": "MHZ"},
                     {"value": 119.4, "value_text": "119.400", "unit": "MHZ"}]},
    {"service": "ATIS", "raw_text": "ATIS Asaba Arrival Information 126.600 MHz 0600 - 1800",
     "frequencies": [{"value": 126.6, "unit": "MHZ"}]},     # pre-car-3 record
]


def test_ad218_reply_shows_each_service_as_printed():
    from responder import ad218_reply, overview_reply
    out = ad218_reply(Resolution(icao="DNAS", label="Asaba"), ASABA_218)
    assert "TWR Asaba Tower 118.500 MHz 0600 - 1800\n119.400 MHz 0600 - 1800" in out
    assert "ATIS Asaba Arrival Information 126.600 MHz" in out     # raw_text fallback
    assert "TWR=" not in out and "118.5MHZ" not in out
    # Each service is its own block: TWR's lines never run into ATIS's.
    assert out.index("119.400") < out.index("\n\nATIS")
    assert "Not in my stored copy" in ad218_reply(RES, [])
    card = overview_reply(RES, [], [], ASABA_218)
    assert "TWR: 118.500 MHZ, 119.400 MHZ" in card and "ATIS: 126.6 MHZ" in card


def test_main_routes_comms_to_the_structured_records():
    import inspect, main
    src = inspect.getsource(main.process)
    assert 'get_structured, res.icao, "2.18"' in src and "ad218_reply(res, crecs)" in src


def test_d31_sections_show_every_row_under_the_aips_label():
    """27 Nov: "katsina secondary power" got "DNKT AD 2.15: ABN/IBN=Available",
    the one-line chunk; the record holds every row."""
    from responder import record_section_reply
    katsina = Resolution(icao="DNKT", label="Katsina")
    out = record_section_reply(katsina, "2.15", {
        "abn_ibn": "Available", "ldi_anemometer": "Anenometer available in MET garden.",
        "twy_lighting": "Not Applicable", "secondary_power": "Available", "remarks": "NIL"})
    assert "Secondary power supply/switch-over time: Available" in out
    assert "LDI location and LGT / Anemometer location and LGT: Anenometer available in MET garden." in out
    met = record_section_reply(katsina, "2.11", {"hours_of_service": "H24 Not Applicable",
                                                 "taf_preparation": "KANO 30 Hours"})
    assert "Hours of service / MET Office outside hours: H24 Not Applicable" in met
    assert "Office responsible for TAF preparation / Period of validity: KANO 30 Hours" in met
    twy = record_section_reply(katsina, "2.8", {"taxiway_info": "Width: 30 M  Surface: Asphalt",
                                                "multi_facility_flag": False})
    assert "Taxiway width, surface and strength: Width: 30 M Surface: Asphalt" in twy
    assert "multi_facility" not in twy
    assert "Not in my stored copy — see AD 2.15." in record_section_reply(katsina, "2.15", {})


def test_main_routes_d31_sections_to_the_records():
    import inspect, main
    src = inspect.getsource(main.process)
    assert 'section in ("AD 2.8", "AD 2.11", "AD 2.15")' in src
    assert "record_section_reply(res, _num, _recs[0])" in src


def test_ad22_reply_shows_operator_lines_without_the_label():
    from responder import ad22_reply
    out = ad22_reply(RES, {"operator_lines": ["Federal Airports Authority of Nigeria",
                                              "P. O. Box 4532,", "Ikeja-Lagos"],
                           "operator_info": "Federal Airports Authority of Nigeria P. O. Box 4532, Ikeja-Lagos"})
    assert "Operator: \n   Federal Airports Authority of Nigeria\n   P. O. Box 4532,\n   Ikeja-Lagos" in out
    old = ad22_reply(RES, {"operator_info": "Name of aerodrome operator, address, X"})
    assert "Operator: Name of aerodrome operator, address, X" in old      # pre-D32 record
