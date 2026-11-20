#!/usr/bin/env python3
"""
validate_procedures.py — human-readable proof for procedures.py, BEFORE
setting PROCEDURES_TEXT_ENABLED=1.

WHAT THIS CHECKS, AND WHY
--------------------------
procedures.extract() is deliberately conservative: it returns None (plate
only, no verbatim text) whenever a runway+type doesn't resolve to EXACTLY one
approach header, or when Holding/Letdown/Missed Approach don't all parse
cleanly. That's the right default — but it means "the code runs without
raising an exception" is NOT proof the feature is trustworthy. What actually
matters is whether the SECTIONED TEXT IT PRODUCES, for a REAL approach, in a
REAL aerodrome's own AD 2.22 text, correctly matches the source with no clause
dropped and no cross-approach mixing. That can only be judged by a human
reading it — this script's job is to make that reading as easy as possible.

METHOD
------
For each aerodrome, this does NOT guess plausible (runway, type) values —
it reuses procedures.py's OWN _HDR regex to find every genuine "Instrument
approach procedures for RWY NN based on TYPE" header actually present in
that aerodrome's own text, then calls extract() for each one found. This
means the script tests exactly the real approaches on file, nothing invented,
and nothing missed.

For every approach found:
  - PASS  -> extract() returned all three sections; the full verbatim text is
             printed for you to compare against the real PDF page.
  - PLATE -> extract() returned None (ambiguous header match, or an
             incomplete section) — confirms the existing safe fallback is
             what a pilot would see for this specific approach, not a crash
             or a silent wrong answer.

Read the PASS cases against the real AIP page for each aerodrome you check —
this script proves the code path runs cleanly, but only a human can confirm
the words are the same as the source.

USAGE
-----
    python validate_procedures.py                  # all 36 standard aerodromes
    python validate_procedures.py DNAA DNKN DNBB    # a subset
    python validate_procedures.py --summary-only    # coverage numbers only,
                                                       no full text dump
"""
import re
import sys

import procedures
from database import get_section_text
from aip_structure import AERODROMES, STANDARD_36


def _short_type(raw):
    """Canonical short type ('ILS'/'VOR'/'RNAV'/'NDB') — what the live bot's
    LLM extraction actually produces, rather than the raw header string.

    Using the raw header string here was a real bug in this script: it
    reported 46% coverage where the true figure was 77%, because raw types
    carry multiple internal spaces ("ILS/DME    (CAT   I, II)") while
    extract() whitespace-collapses before comparing, so they never matched."""
    t = (raw or "").lower()
    for k, v in (("rnav", "RNAV"), ("gnss", "RNAV"), ("rnp", "RNAV"),
                 ("ils", "ILS"), ("loc", "ILS"), ("vor", "VOR"), ("ndb", "NDB")):
        if k in t:
            return v
    return (raw or "").strip()


def _discover_approaches(full_text: str):
    """Every genuine (runway, type) pair this aerodrome's OWN AD 2.22 text
    declares, via procedures.py's own header regex — not a guessed list.
    Deduplicated, since a header could in principle repeat."""
    seen = set()
    out = []
    for m in procedures._HDR.finditer(full_text):
        # NORMALIZE exactly as the live bot does before querying. Without this
        # the validator under-reports badly: it discovered types from RAW text
        # (multi-space, e.g. "ILS/DME    (CAT   I, II)") and runways with
        # internal splits ("0 3"), then fed them back as the query — but
        # extract() whitespace-collapses internally before comparing, so those
        # never matched. That was a bug in THIS script, not in procedures.py:
        # it reported 46% coverage where the real figure was 77%.
        rwy = re.sub(r"\s+", "", m.group(1))
        typ = _short_type(m.group(2))
        key = (rwy.upper(), typ.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append((rwy, typ))
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    summary_only = "--summary-only" in sys.argv

    entries = [e for e in AERODROMES if e[0] in STANDARD_36]
    if args:
        wanted = set(a.upper() for a in args)
        entries = [e for e in entries if e[0] in wanted]

    total_approaches = 0
    total_pass = 0
    total_plate = 0
    no_approach_aerodromes = []
    no_text_aerodromes = []

    for icao, name, start, end in entries:
        full_text = get_section_text(icao, "AD 2.22")
        if not full_text.strip():
            no_text_aerodromes.append(icao)
            print(f"\n{'='*78}\n{icao} ({name}) — NO AD 2.22 TEXT FOUND\n{'='*78}")
            continue

        approaches = _discover_approaches(full_text)
        if not approaches:
            no_approach_aerodromes.append(icao)
            print(f"\n{'='*78}\n{icao} ({name}) — no instrument-approach headers "
                  f"in its own AD 2.22 text (may genuinely be VFR-only)\n{'='*78}")
            continue

        print(f"\n{'='*78}\n{icao} ({name}) — {len(approaches)} approach(es) found\n{'='*78}")

        for rwy, typ in approaches:
            total_approaches += 1
            result = procedures.extract(full_text, rwy, typ)
            if result is None:
                total_plate += 1
                print(f"\n  [PLATE] RWY {rwy}, {typ} — extract() returned None "
                      f"(ambiguous header match or an incomplete section — "
                      f"pilot sees the plate, not a spliced/partial procedure)")
                continue
            total_pass += 1
            print(f"\n  [PASS] RWY {rwy}, {typ}")
            if not summary_only:
                msg = procedures.format_message(f"{icao} ({name})", result)
                indented = "\n".join(f"    {line}" for line in msg.splitlines())
                print(indented)

    print(f"\n{'='*78}")
    print(f"SUMMARY")
    print(f"{'='*78}")
    print(f"Aerodromes checked:            {len(entries)}")
    print(f"  no AD 2.22 text at all:      {len(no_text_aerodromes)}  {no_text_aerodromes}")
    print(f"  no approach headers found:   {len(no_approach_aerodromes)}  {no_approach_aerodromes}")
    print(f"Approaches found:               {total_approaches}")
    print(f"  sectioned successfully:      {total_pass}")
    print(f"  fell back to plate-only:     {total_plate}")
    if total_approaches:
        pct = 100 * total_pass / total_approaches
        print(f"  coverage:                    {pct:.0f}%")
    print(f"{'='*78}")
    print("\nNext: read every [PASS] case above against the real PDF page for")
    print("that aerodrome — confirm no clause is missing and the three sections")
    print("(Holding / Letdown / Missed Approach) match the source exactly.")
    print("Only after that manual check should PROCEDURES_TEXT_ENABLED be set.")


if __name__ == "__main__":
    main()
