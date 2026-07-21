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
import time
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
                "entity": "", "label": label,
                "fact_value": value,
                "fact_text": f"{frame} {label}: {value}",
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
                    "fact_value": f"{v} m",
                    "fact_text": f"{frame} RWY {rwy}. {name} "
                            f"({_DD_LONG[name]}): {v} m",
                })
        if _clean(rec.get("remarks")):
            out.append({
                "entity": f"RWY {rwy}", "label": "Remarks",
                "fact_value": _clean(rec["remarks"]),
                "fact_text": f"{frame} RWY {rwy}. Remarks: {_clean(rec['remarks'])}",
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
                    "fact_value": dims,
                    "fact_text": f"{frame} RWY {desig}. Dimensions "
                            f"(length x width): {dims}",
                })
            for end, detail in (rec.get("end_detail") or {}).items():
                d = _clean(detail)
                if not d:
                    continue
                ent = "general" if end == "general_notes" else f"RWY {end}"
                out.append({
                    "entity": ent, "label": "Details",
                    "fact_value": d,
                    "fact_text": f"{frame} {ent}. {d}",
                })
        else:
            for end, detail in (rec.get("end_detail") or {}).items():
                d = _clean(detail)
                if d:
                    out.append({"entity": "", "label": "Notes", "fact_value": d,
                                "fact_text": f"{frame} {d}"})
        return out

    # --- shape D: per-service comms (2.18) ---
    if "service" in rec:
        svc = _clean(rec.get("service")) or "?"
        freqs = rec.get("frequencies") or []
        if freqs:
            joined = ", ".join(f"{f.get('value')}{f.get('unit','')}" for f in freqs)
            out.append({
                "entity": svc, "label": "Frequency",
                "fact_value": joined,
                "fact_text": f"{frame} {svc}. Frequency / channel to contact "
                        f"{svc}: {joined}",
            })
        if _clean(rec.get("raw_text")):
            out.append({
                "entity": svc, "label": "Full entry",
                "fact_value": _clean(rec["raw_text"]),
                "fact_text": f"{frame} {svc}. {_clean(rec['raw_text'])}",
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
                "fact_value": f"{freq} {unit}".strip(),
                "fact_text": f"{frame} {head}. Frequency: {freq} {unit}".strip(),
            })
        for key, name in (("hours", "Hours of operation"),
                          ("lat", "Latitude"), ("lon", "Longitude"),
                          ("elevation", "Elevation"), ("remarks", "Remarks")):
            v = _clean(rec.get(key))
            if v:
                out.append({
                    "entity": head, "label": name, "fact_value": v,
                    "fact_text": f"{frame} {head}. {name}: {v}",
                })
        return out

    # --- fallback: index whatever scalar fields exist -----------------------
    for k, v in rec.items():
        if k in ("icao", "icao_code"):
            continue
        val = _clean(v)
        if val and not isinstance(v, (dict, list)):
            label = k.replace("_", " ")
            out.append({"entity": "", "label": label, "fact_value": val,
                        "fact_text": f"{frame} {label}: {val}"})
    return out


_DD_LONG = {
    "TORA": "take-off run available",
    "TODA": "take-off distance available",
    "ASDA": "accelerate-stop distance available",
    "LDA": "landing distance available",
}


_CLIENT = None


def _client_ref():
    """Current Supabase client. Held indirectly so _reset_client() can swap it
    out mid-run without every call site caring."""
    global _CLIENT
    if _CLIENT is None:
        import database
        _CLIENT = database.supabase
    return _CLIENT


def _reset_client():
    """Rebuild the client after a TLS/connection failure.

    A broken SSL socket stays broken: retrying the same request on the same
    client reproduces "bad record mac" or "EOF in violation of protocol"
    every time. Only a fresh connection recovers, which is why simply
    retrying was not enough on a flaky link."""
    global _CLIENT
    try:
        from supabase import create_client
        import config
        _CLIENT = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
        print("      (connection rebuilt after TLS error)")
    except Exception as exc:  # noqa: BLE001
        print(f"      (could not rebuild connection: {exc})")


def _dedupe_keys(facts):
    """Make (subsection, entity, label) unique within an aerodrome.

    Subsections that repeat a record shape emit facts sharing a key — AD 2.10
    is the clearest case, where every obstacle carries the same field labels
    and there is no natural entity to separate them:

        ('2.10', '', 'Obstacle type') = 'Mast 120m'
        ('2.10', '', 'Obstacle type') = 'Building 80m'

    Two consequences, both bad. Postgres rejects the whole batch with
    21000 ("ON CONFLICT DO UPDATE command cannot affect row a second time"),
    and if it did not, the second obstacle would silently OVERWRITE the
    first — losing real data with no error at all.

    So: identical key AND identical value collapse to one row (a genuine
    duplicate). Identical key with DIFFERENT values get a numbered entity
    ('obstacle 1', 'obstacle 2'), so every value survives and stays
    individually addressable."""
    by_key = {}
    for f in facts:
        by_key.setdefault((f["subsection"], f["entity"], f["label"]), []).append(f)

    out = []
    for (sub, entity, label), group in by_key.items():
        if len(group) == 1:
            out.append(group[0])
            continue
        seen_values, distinct = set(), []
        for f in group:
            if f["fact_value"] not in seen_values:
                seen_values.add(f["fact_value"])
                distinct.append(f)
        if len(distinct) == 1:
            out.append(distinct[0])          # true duplicate
            continue
        for n, f in enumerate(distinct, 1):
            base = entity or "item"
            f["entity"] = f"{base} {n}"
            # keep the embedded sentence honest about which one this is
            f["fact_text"] = f["fact_text"].replace(
                f". {label}:", f". {base.title()} {n}. {label}:", 1)
            out.append(f)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--icao", nargs="*", help="limit to these aerodromes")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the facts that WOULD be indexed; write nothing")
    ap.add_argument("--limit", type=int, default=40,
                    help="rows to show per aerodrome in --dry-run")
    ap.add_argument("--force", action="store_true",
                    help="re-embed and rewrite even aerodromes already indexed")
    args = ap.parse_args()

    from database import supabase
    from aip_structure import AERODROMES, STANDARD_36
    names = {i: n for i, n, _s, _e in AERODROMES}

    targets = [i for i, _n, _s, _e in AERODROMES if i in STANDARD_36]
    if args.icao:
        want = {i.upper() for i in args.icao}
        targets = [i for i in targets if i in want]

    print("build_fact_index v5  (resumable; de-duplicated keys; rebuilds connection on TLS error)")
    total = 0
    any_error = False
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

        raw_count = len(facts)
        facts = _dedupe_keys(facts)
        total += len(facts)
        dedup_note = (f"  ({raw_count - len(facts)} duplicate key(s) merged)"
                      if raw_count != len(facts) else "")
        print(f"\n{icao} ({names.get(icao,'')}) — {len(rows)} records "
              f"-> {len(facts)} facts{dedup_note}")
        if args.dry_run:
            for f in facts[:args.limit]:
                print(f"    [{f['subsection']:>5}] {(f['entity'] or '-'):<12} "
                      f"{f['label'][:26]:<26} | {f['fact_text'][:96]}")
            if len(facts) > args.limit:
                print(f"    ... +{len(facts)-args.limit} more")
            continue

        # RESUME: skip aerodromes already fully indexed, BEFORE spending any
        # embedding calls. On a 36-aerodrome run over a flaky link a failure
        # partway through would otherwise mean re-embedding everything already
        # done. Upserts are idempotent, so re-running is always safe — this
        # just makes it cheap and fast.
        if not args.force:
            try:
                existing = (_client_ref().table("aip_facts")
                            .select("id", count="exact")
                            .eq("icao_code", icao).execute())
                have = existing.count or 0
                if have >= len(facts):
                    print(f"    already indexed ({have} facts) — skipping. "
                          f"Use --force to rebuild.")
                    continue
                if have:
                    print(f"    {have} already present, filling in the rest")
            except Exception:  # noqa: BLE001
                pass          # can't check -> just proceed and upsert

        # --- embed + upsert -------------------------------------------------
        # The embeddings endpoint accepts arrays, so batch: one call per 100
        # facts instead of one per fact (~4,500 round-trips across all 36).
        import config as _cfg
        from agent import client as _client
        from retry import retry_call as _retry

        embedded, failed = [], 0
        for i in range(0, len(facts), 100):
            batch = facts[i:i + 100]
            texts = [f["fact_text"].strip().replace("\n", " ") for f in batch]
            try:
                resp = _retry(_client.embeddings.create,
                              input=texts, model=_cfg.EMBEDDING_MODEL)
                for f, item in zip(batch, resp.data):
                    f["embedding"] = item.embedding
                    embedded.append(f)
            except Exception as exc:  # noqa: BLE001
                # Never store a fact without an embedding: it would be
                # invisible to vector search while still looking indexed.
                failed += len(batch)
                print(f"    embedding batch {i//100} FAILED ({exc}) — "
                      f"{len(batch)} facts skipped")

        written = 0
        upsert_errors = []
        lost = []
        # 10, not 100: each row carries a 1536-float embedding (~30KB as JSON),
        # so a 100-row upsert is a ~3MB request — large enough to trigger
        # "SSL: EOF occurred in violation of protocol" mid-transfer.
        UPSERT_BATCH = 10
        for i in range(0, len(embedded), UPSERT_BATCH):
            chunk = embedded[i:i + UPSERT_BATCH]
            for attempt in range(4):
                try:
                    _client_ref().table("aip_facts").upsert(
                        chunk, on_conflict="icao_code,subsection,entity,label").execute()
                    written += len(chunk)
                    break
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc)
                    if "PGRST204" in msg or "Could not find" in msg:
                        upsert_errors.append(f"SCHEMA MISMATCH — {msg[:120]}")
                        lost.extend(chunk)
                        break
                    if attempt == 3:
                        upsert_errors.append(msg[:160])
                        lost.extend(chunk)
                        break
                    # A TLS/connection failure leaves the socket unusable —
                    # retrying on the SAME client fails identically. Rebuild it.
                    if any(k in msg for k in ("SSL", "EOF", "record mac",
                                              "Connection", "timed out", "reset")):
                        _reset_client()
                    time.sleep(2 * (attempt + 1))

        # Report what was actually WRITTEN, never what was merely embedded —
        # an earlier version printed "indexed 125 facts" after every upsert
        # had failed, which made a completely broken run look successful.
        status = f"    wrote {written}/{len(facts)} facts"
        if failed:
            status += f"  ({failed} embedding failures)"
        print(status)
        for e in dict.fromkeys(upsert_errors):
            print(f"      upsert error: {e}")
        for f in lost[:10]:
            print(f"      NOT WRITTEN: [{f['subsection']}] "
                  f"{f.get('entity') or '-'} / {f['label']}")
        if len(lost) > 10:
            print(f"      ... +{len(lost)-10} more not written")
        if lost:
            print("      -> re-run the same command; upserts are idempotent, "
                  "so only the missing rows are added.")
        if upsert_errors or failed:
            any_error = True

    print(f"\n{'DRY RUN — nothing written. ' if args.dry_run else ''}"
          f"{total} facts across {len(targets)} aerodrome(s)")
    if args.dry_run:
        print("\nRun without --dry-run to embed and upsert into aip_facts.")
    elif not any_error:
        print("\nAll aerodromes indexed. Verify:")
        print("    select icao_code, count(*) from aip_facts "
              "group by icao_code order by icao_code;")
    if any_error:
        print("\nSOME FACTS WERE NOT WRITTEN — see the errors above. "
              "If you see PGRST204 / 'could not find the column', your "
              "aip_facts table predates the current sql/11_aip_facts.sql: "
              "drop and recreate it, then re-run.")
        sys.exit(1)


if __name__ == "__main__":
    main()
