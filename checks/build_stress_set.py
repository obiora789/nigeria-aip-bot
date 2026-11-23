#!/usr/bin/env python3
"""
build_stress_set.py — generate a phrasing-stress evaluation set whose ground
truth comes from the AIP, never from Vannie's current behaviour.

WHY THIS EXISTS
---------------
vannie_test_set.csv was written by looking at what the bot did and recording
what looked right. That cannot catch a bug where the code and the expectation
share the same wrong belief. A confirmed example: main._AERODROME_DATA_RE
claimed "transition altitude|level" for AD 2.2, and
test_aerodrome_data_query_detected ASSERTED that it must. So 93/93 passed while
the bot answered "what is the transition level for Abuja" with the aerodrome
elevation. The suite was protecting the bug, because both sides were written
from the same assumption.

The fix is to derive expectations from the document instead. aip_structured is
exactly that: every row is a field the 23 per-subsection extractors read out of
the AIP, each validated against all 36 aerodromes. A row gives

    (icao_code, subsection, entity, label, value)

which is simultaneously the correct ANSWER and the correct SECTION -- both
established without asking the bot anything. Had this set existed, the
transition-altitude row would have carried subsection='2.17', and the bot
returning AD 2.2's elevation would have failed on the first run.

WHAT IT PRODUCES
----------------
A CSV, one row per (fact x phrasing):

    id, icao, subsection, entity, label, expected_value, question, corruption

  expected_value  -- from the AIP. What a correct answer must contain.
  subsection      -- from the AIP. Where the answer must be routed.
  corruption      -- which surface-form mutation was applied, so failures can
                     be grouped by mutation class rather than counted in bulk.

Score two things separately, because they fail for different reasons and have
different fixes:
  ROUTING  : did the reply cite `subsection`?      -> a classifier/router problem
  VALUE    : did the reply contain expected_value? -> a retrieval/scoping problem

CORRUPTIONS ARE THE POINT
-------------------------
Regexes fail on surface form -- a space, a plural, a typo. That is how real
pilots type. "let down" instead of "letdown" defeated three independent
keyword guards simultaneously and produced a spliced Maiduguri procedure. So
every fact is asked several ways, including deliberately mistyped ways, and the
mutations below are the ones observed in this project's own logs, not invented.

USAGE
-----
    python checks/build_stress_set.py --out stress_set.csv            # from Supabase
    python checks/build_stress_set.py --from-json facts.json --out s.csv
Then run it through your existing harness and group failures by `corruption`.
"""
import sys as _sys, pathlib as _pl  # repo layout: bot at the top, scripts in folders
_R = next(d for d in _pl.Path(__file__).resolve().parents if (d / "main.py").exists())
_sys.path[1:1] = [str(p) for p in (_R, _R / "checks", _R / "ingestion", _R / "ingestion" / "validate")
                  if str(p) not in _sys.path]
import argparse
import csv
import json
import random
import re
import sys

# --- phrasing templates ----------------------------------------------------
# Keyed by a token found in the AIP's own field label, so this map extends by
# reading the document rather than by guessing pilot vocabulary. {label} is the
# AIP's label verbatim, {place} the aerodrome, {entity} e.g. "RWY 18L"/"CTR".
_TEMPLATES = {
    "transition": ["what is the {label} for {place}",
                   "{place} {label}",
                   "{label} {place}",
                   "whats {place}s {label}"],
    "limits":     ["what are the {label} for {place} {entity}",
                   "{entity} {label} at {place}",
                   "how big is the {place} {entity}"],
    "elevation":  ["what is the {label} of {place}",
                   "how high is {place}",
                   "{place} {label}"],
    "frequency":  ["what is the {entity} frequency at {place}",
                   "{place} {entity} freq",
                   "who do I call for {entity} at {place}"],
    "tora":       ["{label} for {entity} at {place}",
                   "how much runway do I have on {entity} at {place}"],
    "pcn":        ["what is the {label} for {entity} at {place}",
                   "can a heavy jet use {entity} at {place}"],
    "_default":   ["what is the {label} for {place}",
                   "{place} {label}",
                   "{label} at {place} {entity}"],
}

# --- surface-form corruptions ---------------------------------------------
# Each is a mutation class seen in this project's real failures.
def _c_none(q):        return q
def _c_lower(q):       return q.lower()
def _c_nospace(q):     # "letdown" -> "let down" and vice versa: the live bug
    return re.sub(r"\b(let)(down)\b", r"\1 \2", q, flags=re.I) or q
def _c_typo(q):        # transpose two letters in the longest word
    w = max(q.split(), key=len)
    if len(w) < 5: return q
    i = len(w)//2
    return q.replace(w, w[:i] + w[i+1] + w[i] + w[i+2:], 1)
def _c_droparticle(q): return re.sub(r"\b(the|a|of|for)\b\s*", "", q, flags=re.I)
def _c_terse(q):       return " ".join(q.split()[-3:])
def _c_plural(q):      return re.sub(r"\b(limit|frequency|distance)\b", r"\1s", q, flags=re.I)
def _c_question(q):    return q.rstrip("?") + "?"

_CORRUPTIONS = [("clean", _c_none), ("lowercase", _c_lower), ("spacing", _c_nospace),
                ("typo", _c_typo), ("no_articles", _c_droparticle),
                ("terse", _c_terse), ("plural", _c_plural), ("question_mark", _c_question)]

# City names for the aerodromes, so questions read the way pilots ask them.
try:
    import resolver
    resolver.load_index()
    _CITY = {ic: names[0].title() for ic, names in resolver.AERODROMES.items()}
except Exception:                                    # noqa: BLE001
    _CITY = {}


# Labels that are EXTRACTOR INTERNALS, not published AIP fields. A stress case
# built from one asks a question no pilot would ask, and then scores the bot as
# failing for not answering it. Measured: 13 of 54 VALUE_MISS rows in a real run
# were these two labels alone.
#
#   "multi facility flag" -> a boolean the extractor sets for its own use;
#                            expected_value is literally "False"
#   "Notes"               -> AD 2.14's fallback bucket, whose value is the
#                            table's column-header row
#
# Keyed on the LABEL, not on the value, so a genuine field that happens to hold
# "False" or short text is unaffected.
_INTERNAL_LABEL_RE = re.compile(
    r"^(notes?|status|record[_ ]index|.*\bflag\b.*|raw[_ ]text|general[_ ]notes)$",
    re.I)


def _is_internal_label(label: str) -> bool:
    return bool(_INTERNAL_LABEL_RE.match((label or "").strip()))


def _templates_for(label: str):
    low = (label or "").lower()
    for key, tmpl in _TEMPLATES.items():
        if key != "_default" and key in low:
            return tmpl
    return _TEMPLATES["_default"]


def describe():
    """Print the real columns of both candidate tables. Run this FIRST if
    anything below misbehaves -- it removes guesswork rather than replacing one
    assumption with another."""
    from database import supabase
    for tbl in ("aip_facts", "aip_structured"):
        try:
            r = supabase.table(tbl).select("*").limit(1).execute()
            rows = r.data or []
            print(f"{tbl:16} columns: {sorted(rows[0].keys()) if rows else 'TABLE EMPTY'}")
            if rows and tbl == "aip_structured":
                rec = rows[0].get("record")
                if isinstance(rec, dict):
                    print(f"{'':16} record keys: {sorted(rec.keys())}")
        except Exception as e:                        # noqa: BLE001
            print(f"{tbl:16} NOT READABLE: {type(e).__name__}: {e}")


def _page(query_fn):
    """Read a whole table in 1000-row pages (PostgREST caps a single select)."""
    rows, page, size = [], 0, 1000
    while True:
        batch = (query_fn().range(page * size, page * size + size - 1)
                 .execute().data) or []
        rows.extend(batch)
        if len(batch) < size:
            return rows
        page += 1


def _fetch_facts():
    """Preferred source: aip_facts, one atomic fact per row.

    Columns per sql/11a_fix_aip_facts.sql: icao_code, subsection, entity,
    label, fact_value, fact_text. This is already exactly the shape a stress
    case needs -- the answer AND the section, both read out of the AIP by the
    validated extractors, with no reference to what the bot currently does."""
    from database import supabase
    rows = _page(lambda: supabase.table("aip_facts")
                 .select("icao_code,subsection,entity,label,fact_value"))
    return [{"icao_code": r.get("icao_code"), "subsection": r.get("subsection"),
             "entity": r.get("entity") or "", "label": r.get("label") or "",
             "value": r.get("fact_value") or ""} for r in rows]


def _fetch_structured():
    """Fallback: aip_structured, whose columns are icao_code, subsection,
    record (a JSON blob) and record_index -- NOT entity/label/value.

    The record's keys differ per extractor, so each key becomes a label and
    each scalar its value. The entity is taken from whichever identifying key
    the record happens to carry; a record with none is still usable, just
    unscoped."""
    from database import supabase
    rows = _page(lambda: supabase.table("aip_structured")
                 .select("icao_code,subsection,record"))
    ENTITY_KEYS = ("runway", "rwy", "designator", "entity", "service",
                   "ident", "navaid", "type", "name", "airspace")
    out = []
    for r in rows:
        rec = r.get("record")
        if not isinstance(rec, dict):
            continue
        entity = next((str(rec[k]) for k in ENTITY_KEYS
                       if rec.get(k) not in (None, "")), "")
        for k, v in rec.items():
            if k in ENTITY_KEYS or v in (None, "", [], {}):
                continue
            if isinstance(v, (dict, list)):
                continue
            out.append({"icao_code": r.get("icao_code"),
                        "subsection": r.get("subsection"),
                        "entity": entity, "label": k.replace("_", " "),
                        "value": str(v)})
    return out


def _fetch_from_supabase(source="auto"):
    if source in ("auto", "facts"):
        try:
            rows = _fetch_facts()
            if rows:
                print(f"source: aip_facts ({len(rows)} rows)")
                return rows
            print("aip_facts is empty.")
        except Exception as e:                        # noqa: BLE001
            print(f"aip_facts unreadable ({type(e).__name__}: {e})")
        if source == "facts":
            return []
    rows = _fetch_structured()
    print(f"source: aip_structured ({len(rows)} flattened facts)")
    return rows


def build(rows, per_fact=3, seed=0):
    random.seed(seed)
    out, n = [], 0
    for r in rows:
        icao = (r.get("icao_code") or "").upper()
        label = (r.get("label") or "").strip()
        value = (r.get("value") or "").strip()
        sub = (r.get("subsection") or "").strip()
        entity = (r.get("entity") or "").strip()
        # A fact with no value cannot be scored; a fact with no section cannot
        # be routed. Skipping is honest -- inventing either would reintroduce
        # exactly the guesswork this file exists to remove.
        if not (icao and label and value and sub):
            continue
        if _is_internal_label(label):
            continue
        place = _CITY.get(icao, icao)
        tmpls = _templates_for(label)
        picks = random.sample(tmpls, min(per_fact, len(tmpls)))
        for t in picks:
            base = t.format(label=label.lower(), place=place,
                            entity=entity or "").replace("  ", " ").strip()
            cname, cfn = random.choice(_CORRUPTIONS)
            q = cfn(base).strip()
            if not q:
                continue
            n += 1
            out.append({"id": f"S{n:05d}", "icao": icao, "subsection": sub,
                        "entity": entity, "label": label,
                        "expected_value": value, "question": q,
                        "corruption": cname})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="stress_set.csv")
    ap.add_argument("--from-json", help="JSON array of aip_structured rows (offline)")
    ap.add_argument("--per-fact", type=int, default=3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--source", choices=["auto", "facts", "structured"], default="auto")
    ap.add_argument("--describe", action="store_true",
                    help="print the real table columns and exit")
    a = ap.parse_args()

    if a.describe:
        describe()
        return 0
    rows = (json.load(open(a.from_json)) if a.from_json
            else _fetch_from_supabase(a.source))
    print(f"aip_structured rows read : {len(rows)}")
    cases = build(rows, a.per_fact, a.seed)
    print(f"stress cases generated   : {len(cases)}")
    if not cases:
        print("No scoreable facts. Check that rows carry subsection/label/value.")
        return 1
    with open(a.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(cases[0].keys()))
        w.writeheader()
        w.writerows(cases)
    from collections import Counter
    print("\nby subsection :", dict(sorted(Counter(c["subsection"] for c in cases).items())))
    print("by corruption :", dict(Counter(c["corruption"] for c in cases)))
    print(f"\nwritten to {a.out}")
    print("\nScore ROUTING (did the reply cite `subsection`?) and VALUE (did it")
    print("contain `expected_value`?) separately -- a routing miss and a value")
    print("miss have different causes and different fixes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
