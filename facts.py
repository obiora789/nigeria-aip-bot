"""
facts.py — safe handling of cross-aerodrome enumeration questions.

"Which aerodromes use a 5000 ft transition altitude?" asks Vannie to scan all ~40
aerodromes at once. That is NOT something the AIP text supports reliably: the
transition altitude sits in AD 2.17 for some aerodromes but only on the approach
PLATE (an image, excluded from the text index) for others, and the two-column
layout scatters the value from its label. An automated scrape provably misses
real aerodromes (e.g. Ado Ekiti / DNET) and can pick up non-aerodrome training
areas — so any enumerated list would be silently incomplete or wrong.

For a safety reference aid, a confident-but-wrong cross-aerodrome list is worse
than a refusal. So Vannie does NOT enumerate. It detects the question and answers
honestly: it verifies transition altitude one aerodrome at a time (which the
AD 2.17 retrieval path now does reliably) and asks the pilot to name the
aerodrome. This is a deliberate, documented boundary — not a retrieval failure.
"""
import re

import config

# "which aerodromes ... transition altitude / TA" (either order).
_TA_ENUM_RE = re.compile(
    r"which\s+aerodromes?\b.*\b(transition altitude|\bta\b)"
    r"|\b(transition altitude|\bta\b)\b.*\bwhich\s+aerodromes?", re.I)


def is_ta_enumeration(text: str) -> bool:
    return bool(_TA_ENUM_RE.search(text or ""))


def answer_ta_enumeration(text: str):
    """Honest boundary reply. Vannie won't produce a cross-aerodrome list it can't
    guarantee is complete; it offers the reliable per-aerodrome lookup instead."""
    return (
        "I can't give a guaranteed-complete list of every aerodrome at a given "
        "transition altitude. In this AIP the transition altitude is published in "
        "AD 2.17 for some aerodromes and only on the approach chart for others, so "
        "any list I assembled could silently miss one — and for a reference aid a "
        "wrong list is worse than none.\n\n"
        "What I can do reliably is look it up for a specific aerodrome: ask me "
        "\"what's the transition altitude at Abuja?\" (or any aerodrome) and I'll "
        "return the exact AD 2.17 value with its source.\n\n"
        f"---\nSource: Nigeria AIP - AD 2.17 (per aerodrome) - {config.AIRAC_CYCLE}\n"
        f"{config.DISCLAIMER}")
