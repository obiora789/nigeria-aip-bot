#!/usr/bin/env python3
"""
check_procedures_all.py — for EVERY instrument approach published in AD 2.22,
does Vannie quote the procedure, or fall back to "read it from the plate"?
And when it falls back, exactly why?

Read-only. One database read per aerodrome, no model calls. Run from the repo
root with the venv active:

    python check_procedures_all.py              # all 40 aerodromes
    python check_procedures_all.py DNKN DNMM    # just these

Why this exists: Kano RWY 06 ILS (2026-10-21) fell back to the plate although
its holding, letdown and missed approach were all stored. The AIP prints
"(14000 ft),." and the cut-off check read that as a sentence ending on a
comma. The same typo, or a different extraction fault, can sit in any
aerodrome's AD 2.22; this finds every one.

For each approach header it reports one of:
  TEXT        Vannie quotes holding, letdown and missed approach.
  PLATE ...   Vannie sends the plate instead, with the reason:
                ambiguous      two headers match the same runway + type
                no <section>   that label was not found in the block
                short <s>      the section text is too short to be real
                table <s>      a minima-table row is mixed into the text
                cut-off <s>    the text ends on a comma: genuinely cut off
                typo <s>       ends ",." — complete, refused only by the old
                               check (fixed by patch_procedures.py)

It also shows the last words of every section that the patch rescues, so you
can confirm each one really is a complete sentence before deploying.
"""
import re
import sys
from collections import Counter

import procedures as P
import resolver
from database import get_section_text


def _type_of(header_type: str) -> str:
    t = header_type.lower()
    if re.search(r"\bils\b", t):
        return "ILS"
    if re.search(r"rnav|gnss|rnp", t):
        return "RNAV"
    if "ndb" in t:
        return "NDB"
    if "vor" in t:
        return "VOR"
    return ""


def _raw_tail(seg: str) -> str:
    seg = P._TAIL.split(seg)[0]
    seg = "\n".join(l for l in seg.splitlines() if not P._JUNK.search(l))
    return P._SECNUM.sub("", seg).strip().rstrip("•–- \n")


def _section_reasons(block: str):
    """Per-section verdict, mirroring P._sectionise() but saying WHY."""
    found, why = {}, {}
    ms = list(P._PROC.finditer(block))
    for i, m in enumerate(ms):
        s = m.group(1).lower()
        kind = ("Holding" if s.startswith("holding")
                else "Letdown" if s.startswith("letdown") else "Missed Approach")
        if kind in found or kind in why:
            continue
        seg = block[m.end():(ms[i + 1].start() if i + 1 < len(ms) else len(block))]
        body = P._clean(seg)
        tail = _raw_tail(seg)
        if len(body) <= 15:
            why[kind] = "short"
        elif P._TABLE_ROW_RE.search(body):
            why[kind] = "table"
        elif tail.endswith(","):
            why[kind] = "cut-off"
        elif body.strip().endswith(","):
            why[kind] = "typo"            # refused by the old check only
            found[kind] = body
        else:
            found[kind] = body
    for k in P._ORDER:
        if k not in found and k not in why:
            why[k] = "no"
    return found, why


def check(icao):
    text = get_section_text(icao, "AD 2.22") or ""
    if not text.strip():
        return icao, [], "no AD 2.22 text stored"
    full = re.sub(r"[ \t]+", " ", text)
    hs = list(P._HDR.finditer(full))
    rows = []
    for i, h in enumerate(hs):
        rwy = re.sub(r"\s+", "", h.group(1))
        htype = re.sub(r"\s+", " ", h.group(2)).strip()
        req = _type_of(htype)
        live = P.extract(text, rwy, req)           # what the INSTALLED code does
        matches = [x for x in hs
                   if P._rwy_match(rwy, re.sub(r"\s+", "", x.group(1)))
                   and (not req or P._type_match(req, x.group(2)))]
        start = h.end()
        nxt = hs[i + 1].start() if i + 1 < len(hs) else len(full)
        major = P._NEXT_MAJOR_RE.search(full, start, nxt)
        block = full[start:(major.start() if major else nxt)]
        found, why = _section_reasons(block)
        if len(matches) > 1:
            reason = "ambiguous"
        else:
            bad = [f"{v} {k}" for k, v in why.items() if v != "typo"]
            typos = [k for k, v in why.items() if v == "typo"]
            reason = "; ".join(bad) or ("typo " + ", ".join(typos) if typos else "")
        rows.append({"rwy": rwy, "type": htype, "req": req,
                     "live": "TEXT" if live else "PLATE", "reason": reason,
                     "typo_tails": {k: found[k][-70:] for k, v in why.items() if v == "typo"}})
    return icao, rows, None


def main():
    only = [a.upper() for a in sys.argv[1:]]
    resolver.load_index()
    icaos = only or sorted(resolver.VALID_ICAO)
    tally, rescued, problems = Counter(), [], []

    for icao in icaos:
        icao, rows, err = check(icao)
        if err:
            print(f"\n{icao}: {err}")
            tally["no text"] += 1
            continue
        if not rows:
            print(f"\n{icao}: no instrument-approach headers in AD 2.22")
            tally["no headers"] += 1
            continue
        print(f"\n{icao}")
        for r in rows:
            label = f"RWY {r['rwy']:<4} {r['type'][:34]:<34}"
            if r["live"] == "TEXT":
                note = "  (rescued by the patch)" if r["typo_tails"] else ""
                print(f"  TEXT   {label}{note}")
                tally["text"] += 1
            else:
                print(f"  PLATE  {label}  <- {r['reason'] or 'unknown'}")
                tally["plate"] += 1
                if r["reason"] and r["reason"].startswith("typo"):
                    tally["plate: typo only (patch fixes)"] += 1
                elif r["reason"] != "ambiguous":
                    problems.append((icao, r))
            if r["typo_tails"]:
                rescued.append((icao, r))

    print("\n" + "=" * 70)
    for k, v in tally.items():
        print(f"  {k:<34} {v}")

    if rescued:
        print("\nSECTIONS THE PATCH RESCUES — check each ends a complete sentence:")
        for icao, r in rescued:
            for k, tail in r["typo_tails"].items():
                print(f"  {icao} RWY {r['rwy']} {r['req']:<5} {k:<16} ...{tail!r}")

    if problems:
        print("\nPLATE FOR ANOTHER REASON — each needs a look (cut-off and table are")
        print("the safety checks working; 'no <section>' may be a label the matcher")
        print("does not recognise):")
        for icao, r in problems:
            print(f"  {icao} RWY {r['rwy']} {r['req']:<5} {r['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
