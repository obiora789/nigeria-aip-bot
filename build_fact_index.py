#!/usr/bin/env python3
"""
build_fact_index.py — field-level retrieval for AD 2.x.

THE PROBLEM THIS SOLVES
-----------------------
vectorise_aip_v3.py embeds ONE vector per (aerodrome, subsection). For
DNMM's AD 2.22 that is 79,871 characters — holding procedures, letdown,
missed approach, minima tables, radar procedures, VFR rules and PBN coding
tables — averaged into a single point in embedding space.

An average of everything is close to nothing. That is measurably why
retrieval kept failing:

    "what is the lateral limit for lagos ctr"  -> ENR 3.1  @ 59%
    "Abuja runway"                             -> AD 2.22 minima table @ 55%
    "what is the OCA/H for Lagos"              -> AD 2.17 airspace @ 46%

Nothing scored high because no chunk was focused. Every regex guard written
since exists to compensate for that — routing by keyword because retrieval
could not be trusted. Fix the granularity and the guards stop being load-
bearing.

WHAT THIS BUILDS
----------------
One row per FIELD, not per subsection. Each row is atomic and
self-describing — it names its aerodrome, its subsection, the entity it
belongs to (a runway end, a service, a navaid) and its label:

    DNMM | AD 2.17 | -       | Designation and lateral limits
        -> "Lagos (DNMM) AD 2.17 ATS airspace. Designation and lateral
            limits: CTR. A circle radius 20NM, centred on 'LAG' VOR..."

    DNMM | AD 2.12 | RWY 18L | Strength (PCN) and surface
        -> "Lagos (DNMM) AD 2.12 runway physical characteristics.
            RWY 18L. Strength (PCN) and surface: PCN 65/F/A/W/T asphalt"

    DNMM | AD 2.13 | RWY 18L | TORA
        -> "Lagos (DNMM) AD 2.13 declared distances. RWY 18L. TORA: 2745 m"

Embedding THAT text puts "lateral limit for lagos ctr" and the CTR lateral
limits fact in nearly the same place, because they are nearly the same
sentence. No keyword list is involved.

TWO PROPERTIES THIS PRESERVES
-----------------------------
  * MISATTRIBUTION STAYS IMPOSSIBLE. A fact carries its own entity label, so
    a runway's PCN cannot be served as another runway's — the entity is part
    of the retrieved unit, not something reconstructed afterwards.
  * VERIFICATION GETS EASIER, not harder. The answer is a stored value, not
    a synthesis over prose, so verify_grounded_answer's per-excerpt check has
    exactly one candidate.

USAGE
-----
    python build_fact_index.py --dry-run          # print facts, write nothing
    python build_fact_index.py --icao DNMM        # one aerodrome
    python build_fact_index.py                    # all 36, embed + upsert
"""
import argparse
import json
import os
import sys

# Human-readable subsection names, used to give each fact a natural-language
# frame the query vocabulary can match against.
SUBSECTION_NAME = {
    "2.1": "location indicator and name",
    "2.2": "geographical and administrative data",
    "2.3": "operational hours",
    "2.4": "handling services and facilities",
    "2.5": "passenger facilities",
    "2.6": "rescue and fire fighting services",
    "2.7": "seasonal availability and clearing",
    "2.8": "aprons, taxiways and check locations",
    "2.9": "surface movement guidance and markings",
    "2.10": "aerodrome obstacles",
    "2.11": "meteorological information",
    "2.12": "runway physical characteristics",
    "2.13": "declared distances",
    "2.14": "approach and runway lighting",
    "2.15": "other lighting and secondary power supply",
    "2.16": "helicopter landing area",
    "2.17": "ATS airspace",
    "2.18": "ATS communication facilities",
    "2.19": "radio navigation and landing aids",
    "2.20": "local aerodrome regulations",
    "2.21": "noise abatement procedures",
    "2.22": "flight procedures",
    "2.23": "additional information",
}


def _clean(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def facts_from_record(icao, aero_name, subsection, rec):
    """Explode ONE structured record into atomic facts.

    Each extractor writes a slightly different record shape, so this handles
    them by the keys actually present rather than by subsection number —
    which means a new extractor needs no change here as long as it follows
    one of the existing shapes."""
    out = []
    sub_name = SUBSECTION_NAME.get(subsection, "")
    frame = f"{aero_name} ({icao}) AD {subsection} {sub_name}."

    # --- shape A: canonical label/value (2.1-2.11, 2.15-2.17, 2.20, 2.21) ---
    if "field" in rec or "raw_label" in rec:
        label = _clean(rec.get("raw_label")) or _clean(rec.get("field")) or ""
        value = _clean(rec.get("detail"))
        if value:
            out.append({
                "entity": None, "label": label,
                "value": value,
                "text": f"{frame} {label}: {value}",
            })
        return out

    # --- shape B: per-runway declared distances (2.13) ---
    if "runway" in rec and any(k in rec for k in ("tora_m", "toda_m", "asda_m", "lda_m")):
        rwy = _clean(rec.get("runway")) or "?"
        for key, name in (("tora_m", "TORA"), ("toda_m", "TODA"),
                          ("asda_m", "ASDA"), ("lda_m", "LDA")):
            v = rec.get(key)
            if v is not None:
                out.append({
                    "entity": f"RWY {rwy}", "label": name,
                    "value": f"{v} m",
                    "text": f"{frame} RWY {rwy}. {name} "
                            f"({_DD_LONG[name]}): {v} m",
                })
        if _clean(rec.get("remarks")):
            out.append({
                "entity": f"RWY {rwy}", "label": "Remarks",
                "value": _clean(rec["remarks"]),
                "text": f"{frame} RWY {rwy}. Remarks: {_clean(rec['remarks'])}",
            })
        return out

    # --- shape C: per-runway with per-END detail (2.12, 2.14) ---
    if "designation" in rec:
        desig = _clean(rec.get("designation"))
        if desig:
            dims = None
            if rec.get("length_m") and rec.get("width_m"):
                dims = f"{rec['length_m']} x {rec['width_m']} m"
                out.append({
                    "entity": f"RWY {desig}", "label": "Dimensions",
                    "value": dims,
                    "text": f"{frame} RWY {desig}. Dimensions "
                            f"(length x width): {dims}",
                })
            for end, detail in (rec.get("end_detail") or {}).items():
                d = _clean(detail)
                if not d:
                    continue
                ent = "general" if end == "general_notes" else f"RWY {end}"
                out.append({
                    "entity": ent, "label": "Details",
                    "value": d,
                    "text": f"{frame} {ent}. {d}",
                })
        else:
            for end, detail in (rec.get("end_detail") or {}).items():
                d = _clean(detail)
                if d:
                    out.append({"entity": None, "label": "Notes", "value": d,
                                "text": f"{frame} {d}"})
        return out

    # --- shape D: per-service comms (2.18) ---
    if "service" in rec:
        svc = _clean(rec.get("service")) or "?"
        freqs = rec.get("frequencies") or []
        if freqs:
            joined = ", ".join(f"{f.get('value')}{f.get('unit','')}" for f in freqs)
            out.append({
                "entity": svc, "label": "Frequency",
                "value": joined,
                "text": f"{frame} {svc}. Frequency / channel to contact "
                        f"{svc}: {joined}",
            })
        if _clean(rec.get("raw_text")):
            out.append({
                "entity": svc, "label": "Full entry",
                "value": _clean(rec["raw_text"]),
                "text": f"{frame} {svc}. {_clean(rec['raw_text'])}",
            })
        return out

    # --- shape E: per-navaid (2.19) ---
    if "aid_type" in rec:
        aid = _clean(rec.get("aid_type")) or "?"
        ident = _clean(rec.get("ident")) or ""
        head = f"{aid} {ident}".strip()
        freq = _clean(rec.get("frequency"))
        unit = _clean(rec.get("freq_unit")) or ""
        if freq:
            out.append({
                "entity": head, "label": "Frequency",
                "value": f"{freq} {unit}".strip(),
                "text": f"{frame} {head}. Frequency: {freq} {unit}".strip(),
            })
        for key, name in (("hours", "Hours of operation"),
                          ("lat", "Latitude"), ("lon", "Longitude"),
                          ("elevation", "Elevation"), ("remarks", "Remarks")):
            v = _clean(rec.get(key))
            if v:
                out.append({
                    "entity": head, "label": name, "value": v,
                    "text": f"{frame} {head}. {name}: {v}",
                })
        return out

    # --- fallback: index whatever scalar fields exist -----------------------
    for k, v in rec.items():
        if k in ("icao", "icao_code"):
            continue
        val = _clean(v)
        if val and not isinstance(v, (dict, list)):
            label = k.replace("_", " ")
            out.append({"entity": None, "label": label, "value": val,
                        "text": f"{frame} {label}: {val}"})
    return out


_DD_LONG = {
    "TORA": "take-off run available",
    "TODA": "take-off distance available",
    "ASDA": "accelerate-stop distance available",
    "LDA": "landing distance available",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--icao", nargs="*", help="limit to these aerodromes")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the facts that WOULD be indexed; write nothing")
    ap.add_argument("--limit", type=int, default=40,
                    help="rows to show per aerodrome in --dry-run")
    args = ap.parse_args()

    from database import supabase
    from aip_structure import AERODROMES, STANDARD_36
    names = {i: n for i, n, _s, _e in AERODROMES}

    targets = [i for i, _n, _s, _e in AERODROMES if i in STANDARD_36]
    if args.icao:
        want = {i.upper() for i in args.icao}
        targets = [i for i in targets if i in want]

    total = 0
    for icao in targets:
        resp = (supabase.table("aip_structured")
                .select("subsection, record")
                .eq("icao_code", icao)
                .order("subsection").execute())
        rows = resp.data or []
        facts = []
        for row in rows:
            sub = str(row.get("subsection") or "").strip()
            rec = row.get("record") or {}
            if isinstance(rec, str):
                rec = json.loads(rec)
            facts.extend([
                dict(f, icao_code=icao, subsection=sub)
                for f in facts_from_record(icao, names.get(icao, icao), sub, rec)
            ])

        total += len(facts)
        print(f"\n{icao} ({names.get(icao,'')}) — {len(rows)} records -> {len(facts)} facts")
        if args.dry_run:
            for f in facts[:args.limit]:
                print(f"    [{f['subsection']:>5}] {(f['entity'] or '-'):<12} "
                      f"{f['label'][:26]:<26} | {f['text'][:96]}")
            if len(facts) > args.limit:
                print(f"    ... +{len(facts)-args.limit} more")
            continue

        # --- embed + upsert -------------------------------------------------
        from agent import embed_text            # existing embedding helper
        for f in facts:
            f["embedding"] = embed_text(f["text"])
        for i in range(0, len(facts), 100):
            batch = facts[i:i + 100]
            supabase.table("aip_facts").upsert(
                batch, on_conflict="icao_code,subsection,entity,label").execute()
        print(f"    indexed {len(facts)} facts")

    print(f"\n{'DRY RUN — nothing written. ' if args.dry_run else ''}"
          f"{total} facts across {len(targets)} aerodrome(s)")
    if args.dry_run:
        print("\nRun without --dry-run to embed and upsert into aip_facts.")


if __name__ == "__main__":
    main()
