"""
toc.py — deterministic AIP table-of-contents lookup.

A structure question ("which part of the AIP covers prohibited areas?") is a
meta-question about the document's organisation, not a retrieval question.
Answering it from the vector store is unreliable — it returns the CONTENT of the
topic, not its LOCATION. This module maps a topic to its AIP reference from the
published GEN/ENR/AD table of contents, so Vannie answers deterministically:
"prohibited/restricted areas -> ENR 5.1".
"""
import re

import config

# Structure/meta detector: the question asks WHERE something sits in the AIP, not
# what it says. Kept deliberately narrow — it must mention a part/section/chapter,
# 'the AIP', or 'where ... I find' — so 'where is the VOR at Lagos' (a real data
# question) does NOT trip it.
_STRUCTURE_RE = re.compile(
    r"\bwhich\s+(part|section|chapter)\b"
    r"|\bwhat\s+(part|section|chapter)\b"
    r"|\b(part|section|chapter)\s+of\s+the\s+aip\b"
    r"|\bwhere\s+in\s+the\s+aip\b"
    r"|\bwhere\s+(would|do|can|could|should)\s+i\s+find\b",
    re.I,
)

# (keywords) -> (reference, human title). Scanned in order; put specific first.
_TOC = [
    # ── ENR — En-route ────────────────────────────────────────────────────────
    (("prohibited", "restricted area", "restricted areas", "danger area",
      "danger areas"), "ENR 5.1", "Prohibited, Restricted and Danger Areas"),
    (("military exercise", "training area"), "ENR 5.2",
     "Military Exercise and Training Areas"),
    (("en-route obstacle", "enroute obstacle", "navigation obstacle"), "ENR 5.4",
     "Air Navigation Obstacles — En-route"),
    (("bird", "migration", "sensitive fauna"), "ENR 5.6",
     "Bird Migration and Areas with Sensitive Fauna"),
    (("airway", "ats route", "ats routes"), "ENR 3", "ATS Routes"),
    (("gnss", "gps",), "ENR 4.3", "GNSS"),
    (("radio navigation aid", "radio nav aid", "navaid", "navaids", "vor ", "ndb",
      "dme"), "ENR 4.1", "Radio Navigation Aids — En-route"),
    (("significant point", "waypoint designator", "name-code"), "ENR 4.4",
     "Name-code Designators for Significant Points"),
    (("fir", "uir", "tma", "cta", "control area", "flight information region"),
     "ENR 2.1", "FIR, UIR, TMA and CTA"),
    (("airspace classification", "airspace class", "class of airspace"), "ENR 1.4",
     "ATS Airspace Classification"),
    (("altimeter", "transition altitude", "transition level", "qnh"), "ENR 1.7",
     "Altimeter Setting Procedures"),
    (("holding procedure", "approach and departure procedure"), "ENR 1.5",
     "Holding, Approach and Departure Procedures"),
    (("flight plan", "flight planning"), "ENR 1.10", "Flight Planning"),
    (("visual flight rules", "vfr"), "ENR 1.2", "Visual Flight Rules"),
    (("instrument flight rules", "ifr"), "ENR 1.3", "Instrument Flight Rules"),
    (("interception",), "ENR 1.12", "Interception of Civil Aircraft"),
    (("unlawful interference", "hijack"), "ENR 1.13", "Unlawful Interference"),
    (("en-route chart", "enroute chart"), "ENR 6", "En-route Charts"),
    # ── GEN — General ─────────────────────────────────────────────────────────
    (("search and rescue", " sar"), "GEN 3.6", "Search and Rescue"),
    (("meteorolog", "met service", "weather service"), "GEN 3.5",
     "Meteorological Services"),
    (("communication service",), "GEN 3.4", "Communication Services"),
    (("air traffic service",), "GEN 3.3", "Air Traffic Services"),
    (("aeronautical chart",), "GEN 3.2", "Aeronautical Charts"),
    (("aerodrome charge", "heliport charge", "landing charge", "parking charge",
      "charges", "fees"), "GEN 4.1", "Aerodrome / Heliport Charges"),
    (("air navigation services charge", "ans charge"), "GEN 4.2",
     "Air Navigation Services Charges"),
    (("abbreviation",), "GEN 2.2", "Abbreviations Used in AIS Publications"),
    (("unit of measurement", "units of measurement", "measuring system"), "GEN 2.1",
     "Measuring System, Aircraft Markings, Holidays"),
    (("chart symbol",), "GEN 2.3", "Chart Symbols"),
    (("location indicator",), "GEN 2.4", "Location Indicators"),
    (("conversion table",), "GEN 2.6", "Conversion Tables"),
    (("sunrise", "sunset"), "GEN 2.7", "Sunrise / Sunset Tables"),
    (("customs", "immigration", "visa", "entry of aircraft",
      "departure of aircraft"), "GEN 1.2",
     "Entry, Transit and Departure of Aircraft"),
    (("differences from icao", "icao sarps", "icao differences"), "GEN 1.7",
     "Differences from ICAO SARPs"),
    (("designated authorit",), "GEN 1.1", "Designated Authorities"),
    # ── AD — Aerodromes ───────────────────────────────────────────────────────
    (("index to aerodrome", "list of aerodrome"), "AD 1.3",
     "Index to Aerodromes and Heliports"),
    (("aerodrome availability",), "AD 1.1", "Aerodrome Availability"),
    (("heliport",), "AD 3", "Heliports"),
    (("aerodrome data", "aerodrome information"), "AD 2", "Aerodromes"),
]


def is_structure_question(text: str) -> bool:
    return bool(_STRUCTURE_RE.search(text or ""))


def lookup(text: str):
    """Return (reference, title) for the first matching topic, else None."""
    low = f" {(text or '').lower()} "
    for keys, ref, title in _TOC:
        if any(k in low for k in keys):
            return ref, title
    return None


def answer(text: str):
    """Full reply for a structure question, or None if the topic isn't mapped."""
    hit = lookup(text)
    if not hit:
        return None
    ref, title = hit
    return (f"That's documented in AIP {ref} — {title}.\n\n"
            f"———\nSource: Nigeria AIP · {config.AIRAC_CYCLE}\n{config.DISCLAIMER}")
