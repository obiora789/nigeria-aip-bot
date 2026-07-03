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

_HDR = re.compile(
    r"Instrument\s+approach\s+procedures?\s+for\s+RWY\s*(\d{2}[LRC]?)\s+based on\s+"
    r"([^\n0-9.]{2,80})", re.I)
_PROC = re.compile(r"(holding\s+procedure|letdown\s+procedure|missed\s+approach)", re.I)
_SECNUM = re.compile(r"\b\d+\.\d+(?:\.\d+){1,4}\b")
# cut a section body where a minima table, page furniture, or the next block begins
_TAIL = re.compile(
    r"(Approach\s+minima|OCA/H\s*\(ft\)|RWY\s+AID\s+OCA|AIRAC\s+AMDT|Circling\s+OCA|"
    r"Glide\s+path\s+inoperative|GP\s+INOP|Designator|Instrument\s+approach\s+procedure)",
    re.I)
_JUNK = re.compile(
    r"NIGERIAN AIRSPACE MANAGEMENT AGENCY|AD 2-DN[A-Z]{2}-\d+|\d{2} [A-Z]{3} \d{2}|"
    r"NIGERIA AIP|FLIGHT PROCEDURES INSTRUMENT APPROACH", re.I)
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
    return bool(key) and key in (typ or "").lower()


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
        if len(body) > 15:
            out[kind] = body
    return out


def extract(full_text: str, req_rwy: str, req_type: str = ""):
    """Return {'rwy','type','sections':{Holding,Letdown,Missed Approach}} for the
    unambiguously-matched approach, or None (caller falls back to the plate)."""
    if not req_rwy:
        return None
    full = re.sub(r"[ \t]+", " ", full_text or "")
    hs = list(_HDR.finditer(full))
    cands = []
    for i, h in enumerate(hs):
        if not _rwy_match(req_rwy, h.group(1)):
            continue
        if req_type and not _type_match(req_type, h.group(2)):
            continue
        start = h.end()
        end = hs[i + 1].start() if i + 1 < len(hs) else len(full)
        cands.append((h.group(1), h.group(2).strip(), full[start:end]))
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
