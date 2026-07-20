"""
procedures.py — safe, verbatim approach-procedure sectioniser.

This only works because the column-aware re-ingest put AD 2.22 into reading order.
Given the full (ordered) AD 2.22 text for an aerodrome, it:
  1. scopes to the requested approach via its
     'Instrument approach procedures for RWY <nr> based on <type>' header — so a
     VOR request can never pick up an ILS block (the mixing hazard we hit before);
  2. splits that ONE block into Holding / Letdown / Missed Approach by their labels;
  3. shows the AIP's exact words, trimmed of minima tables and page furniture.

HARD SAFETY RULES:
  • it scopes by an explicit runway; no runway -> no result (caller shows plate);
  • exactly one approach block must match (0 = none, >1 = ambiguous -> plate);
  • all three sections must parse cleanly, else -> None (caller shows plate).
So it returns sectioned text ONLY when it is confident and unambiguous. Anything
less falls back to the plate. Never a partial or spliced procedure.
"""
import re

import config

# Tolerates real PDF-extraction artifacts confirmed against the actual AIP
# text (via diagnose_ad222.py, run against DNAA/DNKN/DNMM/DNPO/DNSO):
#   - "based on" sometimes splits mid-word into "base d on" (confirmed on
#     DNKN, DNSO) — the same class of character-split artifact found
#     repeatedly elsewhere in this project (e.g. AD 2.10's "17.11"/"4" split);
#   - the runway designator itself can split with internal whitespace —
#     "18 R" (DNMM), "0 3" (DNPO) — tolerated via \d\s*\d\s*[LRC]? rather
#     than the original rigid \d{2}[LRC]?.
# Confirmed via direct testing against real header text from all five
# aerodromes: 0 of 16 real headers matched the original strict pattern;
# 16 of 16 match this one. The captured runway group still contains any
# internal whitespace at this point — extract() normalizes it immediately
# after the match, before any comparison or storage (see below).
_HDR = re.compile(
    r"Instrument\s+approach\s+procedures?\s+for\s+RWY\s*(\d\s*\d\s*[LRC]?)\s+"
    r"base\s*d?\s+on\s+([^\n0-9.]{2,80})", re.I)
_PROC = re.compile(
    r"\d\.\d+(?:\.\d+){1,4}\s+(holding\s+procedures?|letdown\s+procedures?|"
    r"(?:on\s+)?missed\s+approach(?:\s+procedure)?)", re.I)

# Detects a corrupted section body from column-interleaved source text (a
# reading-order artifact from ad222_extractor.py, confirmed on DNKA and
# DNET's real output) — NOT something safely fixable by trimming _TAIL
# further, since the corruption is woven into the middle of the sentence,
# not just at its end. Two independent signatures, both verified against
# every real clean PASS result from the full 36-aerodrome run with zero
# false positives:
#   - a minima-table row (category letter + two 3-4 digit numbers) bleeding
#     into narrative text, e.g. "...join the hold\n05/ Circl 2610(540) A 1700";
#   - a body ending in a dangling comma, signalling the sentence was cut off
#     mid-thought by an interleaved fragment (confirmed: DNET's Letdown
#     ending "...On the outbound track 032°M,").
# Either signature means the extraction is untrustworthy for this specific
# approach — extract() returns None (plate) rather than show corrupted text
# with false confidence.
_TABLE_ROW_RE = re.compile(r"\b[A-D]\s+\d{3,4}(?:\s*\(\d{2,4}\))?\s+\d{3,4}\b")


def _looks_corrupted(body: str) -> bool:
    return bool(_TABLE_ROW_RE.search(body)) or bool(re.search(r",\s*$", body.strip()))
_SECNUM = re.compile(r"\b\d+\.\d+(?:\.\d+){1,4}\b")
# cut a section body where a minima table, page furniture, or the next block begins.
# Confirmed via validate_procedures.py's full 36-aerodrome run: "Airport
# Operating Minima..." boilerplate bled into the tail of 7+ real Missed
# Approach results (e.g. DNIM RWY35, DNKT RWY23, DNOG RWY23), and
# "Delta Operators.../Takeoff Minima..." pulled entire unrelated sections
# into DNPO RWY21 and DNBC RWY35 specifically. All three added below.
_TAIL = re.compile(
    r"(Approach\s+minima|OCA/H\s*\(ft\)|RWY\s+AID\s+OCA|AIRAC\s+AMDT|Circling\s+OCA|"
    r"Glide\s+path\s+inoperative|GP\s+INOP|Designator|Instrument\s+approach\s+procedure|"
    r"Airport\s+Operating\s+Minima|Delta\s+Operators?|Delta\s+Arrival|Delta\s+Departure|"
    r"Takeoff\s+Minima\s+for\s+R(?:un)?wa?y|\d\.\d+(?:\.\d+)*\s+OCA/H\b|Aircraft\s+category)",
    re.I)
_JUNK = re.compile(
    r"NIGERIAN AIRSPACE MANAGEMENT AGENCY|AD 2-DN[A-Z]{2}-\d+|\d{2} [A-Z]{3} \d{2}|"
    r"NIGERIA AIP|FLIGHT PROCEDURES INSTRUMENT APPROACH|^\s*FLIGHT\s+PROCEDURES\s*$", re.I)
_ORDER = ["Holding", "Letdown", "Missed Approach"]


def _rwy_match(req, blk) -> bool:
    rn, bn = re.match(r"(\d{2})([LRC]?)", req or ""), re.match(r"(\d{2})([LRC]?)", blk or "")
    if not (rn and bn) or rn.group(1) != bn.group(1):
        return False
    return not (rn.group(2) and bn.group(2) and rn.group(2) != bn.group(2))


def _type_match(req, typ) -> bool:
    key = {"vor": "vor", "vor/dme": "vor", "ils": "ils", "loc": "ils", "rnav": "rnav",
           "gnss": "rnav", "rnp": "rnav", "ndb": "ndb"}.get((req or "").lower().strip(),
                                                            (req or "").lower().strip())
    if not key:
        return False
    t = (typ or "").lower()
    # An ILS-named header IS the ILS approach, even when it also names the VOR
    # used for the procedure. Confirmed against the PDF on 9 aerodromes that
    # publish BOTH a pure VOR/DME approach and an ILS one whose header also
    # says VOR (DNSO 'VOR/DME' + 'VOR/DME ILS'; DNKN 'VOR DME' + 'VOR ILS/DME';
    # DNKT 'VOR/DME' + 'IKT ILS VOR/DME'; also DNBE, DNCA, DNGO, DNMN, DNYO).
    # Without this, a 'VOR' request matched both and fell back to the plate as
    # ambiguous — and worse, could have returned the ILS procedure to a pilot
    # who asked for VOR. A non-ILS request never means an ILS approach.
    if key != "ils" and re.search(r"\bils\b", t):
        return False
    return key in t


def _clean(body: str) -> str:
    body = _TAIL.split(body)[0]
    body = "\n".join(l for l in body.splitlines() if not _JUNK.search(l))
    body = _SECNUM.sub("", body)
    body = re.sub(r"\s*\n?\s*[\u2013\-]\s+", "\n\u2022 ", body)   # AIP dashes -> bullets
    body = re.sub(r"[ \t]{2,}", " ", body)
    body = re.sub(r"\n{2,}", "\n", body)
    return body.strip(" ;:.\n\u2022")


def _sectionise(text: str) -> dict:
    ms = list(_PROC.finditer(text))
    out = {}
    for i, m in enumerate(ms):
        s = m.group(1).lower()
        kind = ("Holding" if s.startswith("holding")
                else "Letdown" if s.startswith("letdown") else "Missed Approach")
        if kind in out:
            continue
        start = m.end()
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        body = _clean(text[start:end])
        if len(body) > 15 and not _looks_corrupted(body):
            out[kind] = body
    return out


_NEXT_MAJOR_RE = re.compile(
    r"\d\.\d+(?:\.\d+)*\s+(?:Approach\s+minima|Takeoff\s+Minima|"
    r"Airport\s+Operating\s+Minima|Radar\s+[Pp]rocedures|Procedures\s+for\s+VFR|"
    r"PBN\s+[Pp]rocedures?|PBN\s+PROCEDURE)", re.I)


def extract(full_text: str, req_rwy: str, req_type: str = ""):
    """Return {'rwy','type','sections':{Holding,Letdown,Missed Approach}} for the
    unambiguously-matched approach, or None (caller falls back to the plate)."""
    if not req_rwy:
        return None
    full = re.sub(r"[ \t]+", " ", full_text or "")
    hs = list(_HDR.finditer(full))
    cands = []
    for i, h in enumerate(hs):
        # Normalize the captured runway immediately — strip any internal
        # whitespace from artifacts like "18 R" / "0 3" (see _HDR's comment)
        # BEFORE it's compared against the pilot's request or stored in the
        # final result. Confirmed necessary: without this, _rwy_match's own
        # regex (which expects no internal whitespace) would still fail even
        # after widening _HDR — the bug would just move one step downstream.
        rwy_normalized = re.sub(r"\s+", "", h.group(1))
        if not _rwy_match(req_rwy, rwy_normalized):
            continue
        if req_type and not _type_match(req_type, h.group(2)):
            continue
        start = h.end()
        next_hdr_end = hs[i + 1].start() if i + 1 < len(hs) else len(full)
        # Also stop at the next major non-approach subsection, whichever
        # comes first — prevents the block from ever reaching Radar/VFR/PBN
        # content when this is the last approach header in the document.
        major = _NEXT_MAJOR_RE.search(full, start, next_hdr_end)
        end = major.start() if major else next_hdr_end
        cands.append((rwy_normalized, h.group(2).strip(), full[start:end]))
    if len(cands) != 1:                      # none, or ambiguous -> plate
        return None
    rwy, typ, block = cands[0]
    secs = _sectionise(block)
    if not all(k in secs for k in _ORDER):   # incomplete -> plate
        return None
    return {"rwy": rwy, "type": typ, "sections": secs}


def format_message(res_label: str, result: dict) -> str:
    body = "\n\n".join(f"{k} Procedure:\n{result['sections'][k]}" for k in _ORDER)
    typ = re.sub(r"\s+", " ", result["type"]).strip(" .")
    return (f"{res_label} — RWY {result['rwy']}, {typ} approach\n"
            f"Procedures (verbatim from AD 2.22):\n\n{body}\n\n"
            f"\u2014\u2014\u2014\nSource: Nigeria AIP \u00b7 AD 2.22 \u00b7 "
            f"{config.AIRAC_CYCLE}\n{config.DISCLAIMER}")
