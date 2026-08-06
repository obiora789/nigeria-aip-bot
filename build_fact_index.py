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
import re
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


# AD 2.14 TABLE FURNITURE. The AIP reprints the lighting table's column-header
# row whenever the table spans a page, and the extractor stores it inside the
# value: as the entire value for 5 aerodromes (DNAN, DNBY, DNGO, DNMK, DNSU),
# and appended to real data for 3 more (DNBB, DNFB, DNMM — Lagos 18L ends in
# "RWY APCH LGT ... 1 2 3 4 5 6 7 8 9 10").
#
# ad214_extractor.py now strips this at ingestion, but that only takes effect
# on a full per-aerodrome re-ingest, which rewrites AD 2.1-2.23 in two tables
# for ten aerodromes — a large blast radius for a display defect. Stripping
# here instead means a routine `--force` rebuild of aip_facts alone cleans the
# index Vannie actually queries, with no PDF re-parse and no other extractor
# re-run.
#
# The header runs from "RWY APCH LGT" to the AIP's column-NUMBER row and stops
# there; real content follows it (DNMK's "05 23 Note: PAPI RWY 23 U/S"), so
# cutting to end-of-string destroys data. That mistake was made once already.
_AD214_HEADER_RE = re.compile(
    r"\bRWY\s+APCH\s+LGT\b.*?\b1\s+2\s+3\s+4\s+5\s+6\s+7\s+8\s+9(?:\s+10)?\b",
    re.S | re.I)
_AD214_TITLE_RE = re.compile(
    r"\bDN[A-Z]{2}\s+AD\s*2\.14\s+APPROACH\s+AND\s+RUNWAY\s+LIGHTING\b", re.I)


def _strip_ad214_furniture(value: str) -> str:
    """Remove the AD 2.14 section title and column-header row from a value.

    Returns the value unchanged when neither is present, so it is safe to call
    on every fact. A genuine published value such as DNAK's "Not available."
    survives intact."""
    t = _AD214_TITLE_RE.sub(" ", value or "")
    t = _AD214_HEADER_RE.sub(" ", t)
    return re.sub(r"\s{2,}", " ", t).strip(" ,;.")


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
        # Extractors name the value column differently: AD 2.3 uses "hours",
        # most others use "detail". Looking only for "detail" silently
        # dropped EVERY AD 2.3 fact — the subsection was missing from the
        # index entirely, with no error to notice.
        value = None
        for key in ("detail", "hours", "value", "text", "content", "remarks"):
            value = _clean(rec.get(key))
            if value:
                break
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
    #
    # UNIT-SUFFIXED KEYS ARE MERGED, NOT SPLIT. Extractors name dual-unit
    # fields as <base>_<unit> ("elevation_m": 199.0, "elevation_ft": 653.0).
    # Turning each key into its own label produced two facts,
    # "elevation m" -> "199.0" and "elevation ft" -> "653.0", with the UNIT IN
    # THE LABEL and the fact_value a bare number.
    #
    # Two real consequences, both confirmed on DNKS:
    #   * responder.facts_reply() shows fact_value verbatim, so an elevation
    #     query could return "653.0" with no unit. A unitless altitude is
    #     exactly the wrong-value output this project exists to prevent.
    #   * retrieval saw two facts of identical meaning competing for one
    #     query, so "how high is Kashimbila" had two equally-correct answers
    #     that were ONE value (199.0 m x 3.28084 = 652.9 ft).
    #
    # Merging also satisfies config.SYNTHESIS_SYSTEM rule 2, which already
    # requires both units whenever the AIP publishes both.
    _UNIT_SUFFIX = re.compile(r"^(.*?)_(m|ft|km|nm|mhz|khz|deg|kg|lb|t|c|f)$", re.I)
    # Deterministic order: the upsert key is (icao, subsection, entity, label),
    # so an unstable join order would rewrite the same row on every rebuild.
    _UNIT_ORDER = {"m": 0, "ft": 1, "km": 2, "nm": 3, "mhz": 4, "khz": 5,
                   "deg": 6, "kg": 7, "lb": 8, "t": 9, "c": 10, "f": 11}
    _UNIT_DISPLAY = {"m": "m", "ft": "ft", "km": "km", "nm": "NM",
                     "mhz": "MHz", "khz": "kHz", "deg": "\u00b0",
                     "kg": "kg", "lb": "lb", "t": "t",
                     "c": "\u00b0C", "f": "\u00b0F"}
    grouped = {}
    for k, v in rec.items():
        if k in ("icao", "icao_code"):
            continue
        val = _clean(v)
        if not val or isinstance(v, (dict, list)):
            continue
        m = _UNIT_SUFFIX.match(k)
        if m:
            grouped.setdefault(m.group(1), []).append((m.group(2), val))
        else:
            grouped.setdefault(k, []).append((None, val))

    for base, parts in grouped.items():
        label = base.replace("_", " ")
        if len(parts) == 1 and parts[0][0] is None:
            value = parts[0][1]
        else:
            parts.sort(key=lambda p: _UNIT_ORDER.get((p[0] or "").lower(), 99))
            # Render units the way the AIP writes them. The key suffix is
            # lowercase ASCII ("ref_temp_c"), so joining it raw produced
            # "33.0 c" -- ambiguous in a document where a bare letter can be a
            # runway side, an airspace class or an aircraft category. These are
            # display forms for units that already exist in the key; nothing is
            # inferred or converted.
            value = " / ".join(f"{v} {_UNIT_DISPLAY.get(u.lower(), u)}" if u else v
                               for u, v in parts)
        out.append({"entity": "", "label": label, "fact_value": value,
                    "fact_text": f"{frame} {label}: {value}"})
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


# Subsections whose extractor is kind="text": they emit NO structured records,
# so they produce no facts from aip_structured and were entirely absent from
# the index. They exist only as whole-subsection chunks — AD 2.22 being the
# worst case, ~80,000 characters behind one vector, which is exactly the
# granularity problem the fact index exists to fix.
TEXT_SUBSECTIONS = {"2.10", "2.22", "2.23"}

# Their own numbered headings ("2.22.3.1.1 Holding procedure") are the natural
# unit boundary — the same structure procedures.py already relies on.
_HEADING_RE = re.compile(
    r"(?m)^\s*(\d\.\d+(?:\.\d+)+)\s+([A-Z][^\n]{0,80}?)\s*$")


# An obstacle row carries a coordinate — that is what distinguishes a real
# row from a table header or a wrapped continuation line.
_OBST_ROW_RE = re.compile(r"\d{6}(?:\.\d+)?[NS]\s")


def _obstacle_facts(frame, text):
    """One fact per OBSTACLE (AD 2.10), not one per heading."""
    out = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]{2,}", " ", line).strip()
        if not _OBST_ROW_RE.search(line) or len(line) < 20:
            continue
        name = re.split(r"\s+\d{6}", line)[0].strip(" .;:")
        if not name or len(name) > 60:
            name = line[:40]
        out.append({
            "entity": name,
            "label": "Obstacle",
            "fact_value": line,
            "fact_text": f"{frame} Obstacle {name}: {line}",
        })
    return out


def facts_from_text(icao, aero_name, subsection, text):
    """Split a text-kind subsection into one fact per numbered heading.

    AD 2.22's own structure does the work: every procedure already sits under
    a heading like "2.22.3.1.1 Holding procedure". Splitting there turns one
    80,000-character vector into ~40-80 focused units, each of which reads
    close to how a pilot would ask for it.

    Headings with no body are skipped (a heading alone answers nothing), and
    very long bodies are truncated for embedding — the fact_value keeps
    enough to be useful while staying a sensible unit to match against."""
    out = []
    sub_name = SUBSECTION_NAME.get(subsection, "")
    frame = f"{aero_name} ({icao}) AD {subsection} {sub_name}."
    if not text or not text.strip():
        return out

    # AD 2.10 is the exception: many obstacles sit under ONE heading
    # ("2.10.1 In approach and take-off areas"), so heading-splitting
    # collapsed every obstacle at an aerodrome into a single fact — a mast, a
    # building and a billboard all sharing one embedding, and only 8 of 36
    # aerodromes represented at all. Each obstacle is its own hazard and must
    # be retrievable on its own.
    if subsection == "2.10":
        rows = _obstacle_facts(frame, text)
        if rows:
            return rows

    marks = list(_HEADING_RE.finditer(text))

    parent = ""      # nearest ancestor heading, e.g. the approach a hold belongs to
    for i, m in enumerate(marks):
        num = m.group(1)
        # PDF column extraction leaves runs of spaces inside headings
        # ("Instrument     approach      procedures"). No pilot types that,
        # and it would be embedded verbatim, so collapse it.
        title = re.sub(r"\s+", " ", m.group(2)).strip()
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = re.sub(r"[ \t]{2,}", " ", text[start:end]).strip()
        body = re.sub(r"\n{2,}", "\n", body)

        # A heading like "2.22.3.1" ("Instrument approach procedures based on
        # VOR/DME") is the parent of "2.22.3.1.1 Holding procedure". Without
        # carrying it down, EVERY approach contributes an identical
        # "Holding procedure" fact and a query for the ILS hold cannot be
        # told apart from the VOR one — the same misattribution risk the
        # structured extractors solve with per-entity tracking.
        depth = num.count(".")
        if depth <= 2:
            parent = ""
        elif depth == 3:
            head_line = body.split("\n", 1)[0][:70].strip(" .:")
            parent = f"{title} {head_line}".strip()

        if len(body) < 12:            # a heading with no real content
            continue
        if len(body) > 1200:          # keep each unit a sensible size
            body = body[:1200].rsplit(" ", 1)[0] + " …"

        context = f" {parent}." if (parent and depth > 3) else ""
        out.append({
            "entity": num,
            "label": title[:120],
            "fact_value": body,
            "fact_text": f"{frame}{context} {title}: {body}",
        })

    if not out:
        # No heading matched anything. Index the whole subsection as ONE
        # coarse fact rather than none: zero facts makes it INVISIBLE to
        # retrieval, which is exactly how AD 2.23 ended up absent from the
        # index entirely. Coarse but findable beats missing.
        body = re.sub(r"[ \t]{2,}", " ", text).strip()
        body = re.sub(r"\n{2,}", "\n", body)
        if len(body) >= 12:
            out.append({
                "entity": "",
                "label": (sub_name or f"AD {subsection}").title(),
                "fact_value": body[:1200],
                "fact_text": f"{frame} {body[:1200]}",
            })
    return out


def _db(build, attempts=4):
    """Run a Supabase call with retry + reconnect.

    `build` receives the current client and returns the finished query, e.g.

        _db(lambda c: c.table("aip_facts").select("id").eq("icao_code", i))

    Every database call goes through this. Earlier only the UPSERT was
    protected, so a TLS failure on a READ crashed the whole run with an
    unhandled traceback — the connection to Supabase is the flakiest part of
    this job and reads are just as exposed as writes.

    A broken SSL socket stays broken, so on a connection-level error the
    client is rebuilt before retrying; retrying on the same one reproduces
    "bad record mac" every time.

    Raises the last exception if every attempt fails, so callers can decide
    whether that is fatal or skippable."""
    last = None
    for attempt in range(attempts):
        try:
            return build(_client_ref()).execute()
        except Exception as exc:  # noqa: BLE001
            last = exc
            msg = str(exc)
            if "PGRST" in msg or "Could not find" in msg:
                raise                      # schema errors never recover
            if attempt == attempts - 1:
                break
            if any(k in msg for k in ("SSL", "EOF", "record mac", "Connect",
                                      "timed out", "reset", "decryption")):
                _reset_client()
            time.sleep(2 * (attempt + 1))
    raise last


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
        # SCOPE IS PART OF THE KEY. Without it every ENR entity collides:
        # DNP1 and DNP2 both have entity='' and label='Name', so they looked
        # like duplicates of one another and 57 areas collapsed to a handful of
        # numbered "item 1 / item 2" rows. The upsert key is
        # (scope_kind, scope_id, subsection, entity, label), so deduplication
        # must use the same key or it removes rows the database would have
        # accepted — silently, and in the direction of LOSING data.
        #
        # For AD rows scope_id is the ICAO, so behaviour there is unchanged:
        # this function was always called per-aerodrome.
        by_key.setdefault((f.get("scope_kind", "AD"), f.get("scope_id", ""),
                           f["subsection"], f["entity"], f["label"]),
                          []).append(f)

    out = []
    for (_sk, _sid, sub, entity, label), group in by_key.items():
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



# ---------------------------------------------------------------------------
# ENR ingestion
# ---------------------------------------------------------------------------
# ENR entities are NOT aerodromes, so they cannot be reached by the loop above,
# which iterates STANDARD_36 and reads aip_structured by icao_code. A waypoint
# (TEMSA), an airway (UT467) and a danger area (DND45) each have their own
# identity and no ICAO — which is exactly why 152 pages of ENR had no path into
# the index and "Where is TEMSA?" was answered "not in the Nigerian AIP".
#
# One SCOPE per entity, not per section: DND45's facts are keyed
# (ENR_AREA, DND45), so retrieval confined to that scope can never return
# DND46's vertical limits. The same per-entity guarantee runway ends get.
_ENR_SOURCES = {
    # subsection -> (module, class, scope_kind)
    "5.1": ("enr51_extractor", "ENR51Extractor", "ENR_AREA"),
}

# Which record fields become facts, and the label a pilot sees. Order matters:
# it is the order they appear in a reply. Fields absent from a record are
# skipped, never emitted as empty — an area with no published activation hours
# must not gain a blank one.
_ENR_FACT_FIELDS = [
    ("family", "Type of area"),
    ("name", "Name"),
    ("upper_limit", "Upper limit"),
    ("lower_limit", "Lower limit"),
    ("lateral_limits", "Lateral limits"),
]


def _enr_facts_from_record(rec: dict, subsection: str) -> list:
    """One record -> its atomic facts, each carrying the entity's own scope."""
    scope_kind = rec.get("scope_kind") or "ENR_AREA"
    scope_id = (rec.get("scope_id") or "").strip()
    if not scope_id:
        return []
    frame = f"{scope_id} ENR {subsection}"
    out = []
    for key, label in _ENR_FACT_FIELDS:
        val = _clean(rec.get(key))
        if not val:
            continue
        out.append({
            "scope_kind": scope_kind, "scope_id": scope_id,
            "icao_code": scope_kind,        # trigger keeps this consistent
            "subsection": subsection, "entity": "", "label": label,
            "fact_value": val,
            "fact_text": f"{frame} {label}: {val}",
        })
    # Coordinates are ONE fact, not one per vertex: a polygon is meaningless
    # split across rows, and a pilot asking where an area is needs the whole
    # boundary or none of it.
    coords = rec.get("coordinates") or []
    if coords:
        joined = " ".join(coords)
        out.append({
            "scope_kind": scope_kind, "scope_id": scope_id,
            "icao_code": scope_kind,
            "subsection": subsection, "entity": "", "label": "Coordinates",
            "fact_value": joined,
            "fact_text": f"{frame} Coordinates ({len(coords)} points): {joined}",
        })
    return out


def build_enr(subsections, pdf_path, dry_run=False, limit=40):
    """Extract, embed and upsert ENR facts. Mirrors the AD path deliberately —
    same embedding call, same batch sizes, same retry-with-reconnect — so the
    failure modes already fixed there (SSL drops, false-success reporting,
    silent overwrite on duplicate keys) do not have to be rediscovered here."""
    import importlib
    import pypdfium2 as pdfium

    total = 0
    any_error = False
    for sub in subsections:
        if sub not in _ENR_SOURCES:
            print(f"ENR {sub}: no extractor registered — skipping")
            any_error = True
            continue
        mod_name, cls_name, scope_kind = _ENR_SOURCES[sub]
        try:
            cls = getattr(importlib.import_module(mod_name), cls_name)
        except Exception as exc:                      # noqa: BLE001
            print(f"ENR {sub}: cannot load {mod_name}.{cls_name} ({exc})")
            any_error = True
            continue

        # Page selection matches the extractor's own validator: the page's OWN
        # running header, anchored at the start. Searching anywhere admits the
        # table of contents, which once swept 500 unrelated pages into an
        # AD 2.22 extraction.
        hdr = re.compile(rf"^\s*ENR\s*{re.escape(sub)}\s*-\s*\d+")
        doc = pdfium.PdfDocument(pdf_path)
        pages = []
        for i in range(len(doc)):
            page = doc[i]
            tp = page.get_textpage()
            t = tp.get_text_range()
            tp.close()
            page.close()
            if hdr.match(t):
                pages.append(t)
        if not pages:
            print(f"ENR {sub}: no content pages found in {pdf_path}")
            any_error = True
            continue

        ex = cls()
        ex.segment_text = lambda _segs, _blob="\n".join(pages): _blob
        result = ex.extract("ENR", [1])
        issues = [i for i in ex.validate(result) if i.severity == "error"]
        if issues:
            # A validator error means a record is incomplete — for airspace a
            # pilot may need to avoid, an incomplete record is worse than none.
            print(f"ENR {sub}: {len(issues)} VALIDATION ERROR(S) — nothing written")
            for i in issues[:5]:
                print(f"    {i.field}: {i.message[:90]}")
            any_error = True
            continue

        facts = []
        for rec in result.records:
            facts.extend(_enr_facts_from_record(rec, sub))
        facts = _dedupe_keys(facts)
        print(f"\nENR {sub} — {len(result.records)} entities -> {len(facts)} facts")

        if dry_run:
            for f in facts[:limit]:
                print(f"    [{f['scope_kind']:9}] {f['scope_id']:8} "
                      f"{f['label']:16} | {f['fact_text'][:88]}")
            if len(facts) > limit:
                print(f"    ... +{len(facts) - limit} more")
            total += len(facts)
            continue

        written, failed, upsert_errors, lost = _embed_and_upsert(facts)
        status = f"    wrote {written}/{len(facts)} facts"
        if failed:
            status += f"  ({failed} embedding failures)"
        print(status)
        for e in dict.fromkeys(upsert_errors):
            print(f"      upsert error: {e}")
        for f in lost[:10]:
            print(f"      NOT WRITTEN: {f.get('scope_id')} / {f['label']}")
        if len(lost) > 10:
            print(f"      ... +{len(lost) - 10} more not written")
        if lost:
            print("      -> re-run the same command; upserts are idempotent, "
                  "so only the missing rows are added.")
        if written != len(facts) or upsert_errors or failed:
            any_error = True
        total += written

    return total, any_error



def _embed_and_upsert(facts):
    """Embed every fact and upsert it. Returns rows ACTUALLY WRITTEN.

    Shared by the AD and ENR paths deliberately. This code carries fixes that
    were each found the hard way and must not be rediscovered in a second copy:
      * a fact is NEVER stored without an embedding — it would be invisible to
        vector search while still looking indexed;
      * upserts run in batches of 10, not 100: a 1536-float embedding is ~30KB
        of JSON and a 100-row request triggered "SSL: EOF occurred in violation
        of protocol" mid-transfer;
      * the client is rebuilt on SSL failure — a broken socket stays broken, so
        retry alone was insufficient;
      * the count returned is rows WRITTEN, not rows sent. An earlier version
        printed "indexed 125 facts" after every upsert had failed.
    """
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
                    chunk,
                    on_conflict="scope_kind,scope_id,subsection,entity,label"
                ).execute()
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
    #
    # The diagnostics are returned alongside the count, not swallowed. They
    # ARE the false-success protection: `failed` counts facts that never got
    # an embedding, `lost` names the exact rows that were not written, and
    # `upsert_errors` distinguishes a schema mismatch from a transient SSL
    # drop. A caller that only sees a number cannot tell a clean run from a
    # broken one.
    return written, failed, upsert_errors, lost


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--icao", nargs="*", help="limit to these aerodromes")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the facts that WOULD be indexed; write nothing")
    ap.add_argument("--limit", type=int, default=40,
                    help="rows to show per aerodrome in --dry-run")
    ap.add_argument("--force", action="store_true",
                    help="re-embed and rewrite even aerodromes already indexed")
    ap.add_argument("--enr", nargs="*", metavar="SUBSECTION",
                    help="index ENR subsections instead of aerodromes, "
                         "e.g. --enr 5.1")
    ap.add_argument("--pdf", default="Complete_AIP2026.pdf",
                    help="source PDF for --enr")
    args = ap.parse_args()

    if args.enr is not None:
        subs = args.enr or sorted(_ENR_SOURCES)
        print(f"build_fact_index — ENR mode ({', '.join(subs)})")
        total, any_error = build_enr(subs, args.pdf, args.dry_run, args.limit)
        print(f"\n{total} facts" + (" (DRY RUN — nothing written)" if args.dry_run else ""))
        if any_error:
            print("\nCompleted WITH ERRORS — see above. Exit code 1.")
        return 1 if any_error else 0

    from database import supabase
    from aip_structure import AERODROMES, STANDARD_36
    names = {i: n for i, n, _s, _e in AERODROMES}

    targets = [i for i, _n, _s, _e in AERODROMES if i in STANDARD_36]
    if args.icao:
        want = {i.upper() for i in args.icao}
        targets = [i for i in targets if i in want]

    print("build_fact_index v8  (fixes AD 2.3/2.10/2.23 gaps; retries all db calls)")
    total = 0
    any_error = False
    for icao in targets:
        try:
            resp = _db(lambda c: c.table("aip_structured")
                       .select("subsection, record")
                       .eq("icao_code", icao)
                       .order("subsection"))
        except Exception as exc:  # noqa: BLE001
            # One aerodrome's read failing must not abort the whole run —
            # the remaining 35 are independent, and resume makes a re-run
            # cheap. This previously crashed with a raw traceback.
            print(f"\n{icao}: READ FAILED after retries — skipping ({str(exc)[:90]})")
            any_error = True
            continue
        rows = resp.data or []
        facts = []
        for row in rows:
            sub = str(row.get("subsection") or "").strip()
            rec = row.get("record") or {}
            if isinstance(rec, str):
                rec = json.loads(rec)
            for f in facts_from_record(icao, names.get(icao, icao), sub, rec):
                if sub.startswith("2.14"):
                    # Strip the reprinted column-header row before it reaches
                    # the index. See _strip_ad214_furniture(). A fact left
                    # empty by the strip is DROPPED: it was table markup, not a
                    # published value, and indexing it produced the "Notes"
                    # facts that made "notes at Umueri" answerable at all.
                    f["fact_value"] = _strip_ad214_furniture(f.get("fact_value"))
                    f["fact_text"] = _strip_ad214_furniture(f.get("fact_text"))
                    if not f["fact_value"]:
                        continue
                facts.append(dict(f, icao_code=icao, subsection=sub,
                                  scope_kind="AD", scope_id=icao))

        # The text-kind subsections (2.10, 2.22, 2.23) have no structured
        # records, so they contribute nothing above. Pull their text from
        # aip_knowledge_base and split it on its own numbered headings.
        try:
            kb = _db(lambda c: c.table("aip_knowledge_base")
                     .select("aip_section, content, source_page, source_chunk")
                     .eq("reference_tag", icao))
            by_sec = {}
            for r in (kb.data or []):
                sec = (r.get("aip_section") or "").replace("AD ", "").strip()
                if sec in TEXT_SUBSECTIONS:
                    by_sec.setdefault(sec, []).append(r)
            for sec, chunks in by_sec.items():
                chunks.sort(key=lambda r: (r.get("source_page") or 0,
                                           r.get("source_chunk") or 0))
                blob = "\n".join(c.get("content", "") for c in chunks)
                facts.extend([
                    dict(f, icao_code=icao, subsection=sec,
                         scope_kind="AD", scope_id=icao)
                    for f in facts_from_text(icao, names.get(icao, icao), sec, blob)
                ])
        except Exception as exc:  # noqa: BLE001
            print(f"    (could not read text subsections: {exc})")

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
                existing = _db(lambda c: c.table("aip_facts")
                               .select("id", count="exact")
                               .eq("icao_code", icao), attempts=2)
                have = existing.count or 0
                if have >= len(facts):
                    print(f"    already indexed ({have} facts) — skipping. "
                          f"Use --force to rebuild.")
                    continue
                if have:
                    print(f"    {have} already present, filling in the rest")
            except Exception:  # noqa: BLE001
                pass          # can't check -> just proceed and upsert

        written, failed, upsert_errors, lost = _embed_and_upsert(facts)
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
