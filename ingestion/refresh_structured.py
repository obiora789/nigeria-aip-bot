#!/usr/bin/env python3
"""
refresh_structured.py — re-run AD 2 extractors and rewrite ONLY aip_structured.

Car 3 (values as printed). vectorise_aip_v3.py rewrites everything and embeds
every chunk; when an extractor only gains fields (AD 2.2 "elevation_m_text",
AD 2.18 "lines"), the retrieval chunks do not change and need no new
embeddings. This rewrites the structured records alone: no OpenAI calls, no
aip_knowledge_base writes, nothing deleted first.

SAFE BY DEFAULT
  Without --write it only compares the fresh records with what is stored and
  reports, per aerodrome and subsection:
    same      every stored field has the same value; new fields may be added
    CHANGED   a stored field's value would change (listed) — investigate first
    count     the number of records would change
  With --write it refuses to write while anything is CHANGED or a count
  differs, unless --allow-changes is given (a new AIRAC amendment).

  A subsection whose validation fails is skipped and its stored rows kept, as
  in vectorise_aip_v3.py. Rows are upserted first; only then are stored rows
  past the new count removed, so a failed write never leaves a gap.

USAGE (from the repo folder; PDF_PATH comes from .env, as for the other scripts)
    python ingestion/refresh_structured.py 2.2 2.18              # dry run
    python ingestion/refresh_structured.py 2.2 2.18 --write
    python ingestion/refresh_structured.py 2.2 --only DNIB DNAK --pdf ~/Downloads/Complete_AIP2026.pdf
"""
import sys as _sys, pathlib as _pl  # repo layout: bot at the top, scripts in folders
_R = next(d for d in _pl.Path(__file__).resolve().parents if (d / "main.py").exists())
_sys.path[1:1] = [str(p) for p in (_R, _R / "checks", _R / "ingestion", _R / "ingestion" / "validate")
                  if str(p) not in _sys.path]
import argparse
import importlib
import os
import sys

STRUCT_TABLE = "aip_structured"
# AD 2.22 is page-driven (see vectorise_aip_v3.run_aerodrome) and not handled here.
SUBSECTIONS = ["2.1", "2.2", "2.3", "2.4", "2.5", "2.6", "2.7", "2.8", "2.9",
               "2.10", "2.11", "2.12", "2.13", "2.14", "2.15", "2.16", "2.17",
               "2.18", "2.19", "2.20", "2.21", "2.23"]


def extractor_for(sub):
    n = sub.replace(".", "")
    return getattr(importlib.import_module(f"ad{n}_extractor"), f"AD{n}Extractor")()


def _diff(old, new, where, out):
    """Every way `new` differs from `old`, except fields `new` adds. Nested
    the same way: a frequency inside AD 2.18's list that gains "value_text"
    is an addition, not a change."""
    if isinstance(old, dict) and isinstance(new, dict):
        for key, val in old.items():
            if key not in new:
                out.append(f"{where}{key}: would be dropped")
            else:
                _diff(val, new[key], f"{where}{key}.", out)
    elif isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        for i, (o, n) in enumerate(zip(old, new)):
            _diff(o, n, f"{where}[{i}].", out)
    elif old != new:
        out.append(f"{where.rstrip('.')}: {old!r} -> {new!r}")


def compare(old_records, new_records):
    """(status, details). Only fields the stored record already has are
    compared: a field that is new in the fresh record is an addition."""
    if len(old_records) != len(new_records):
        return "count", [f"{len(old_records)} stored -> {len(new_records)} fresh"]
    details = []
    for i, (old, new) in enumerate(zip(old_records, new_records)):
        _diff(old, new, f"record {i}: ", details)
    return ("CHANGED" if details else "same"), details


def stored(supabase, icao, sub):
    resp = (supabase.table(STRUCT_TABLE).select("record_index,record")
            .eq("icao_code", icao).eq("subsection", sub)
            .order("record_index").execute())
    return [r["record"] for r in (resp.data or [])]


def write(supabase, icao, sub, records):
    rows = [{"icao_code": icao, "subsection": sub, "record_index": i, "record": rec}
            for i, rec in enumerate(records)]
    supabase.table(STRUCT_TABLE).upsert(
        rows, on_conflict="icao_code,subsection,record_index").execute()
    (supabase.table(STRUCT_TABLE).delete()
     .eq("icao_code", icao).eq("subsection", sub)
     .gte("record_index", len(records)).execute())


def main(argv=None):
    import config  # noqa: F401  loads .env, so PDF_PATH is set as for the bot
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("subsections", nargs="+", help="e.g. 2.2 2.18")
    ap.add_argument("--only", nargs="*", default=None, help="ICAO codes")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--allow-changes", action="store_true",
                    help="write even when stored values change (new AIRAC)")
    ap.add_argument("--pdf", default=os.getenv("PDF_PATH"))
    a = ap.parse_args(argv)

    bad = [s for s in a.subsections if s not in SUBSECTIONS]
    if bad:
        sys.exit(f"not a segment-driven AD 2 subsection: {bad}")
    if not a.pdf or not os.path.exists(os.path.expanduser(a.pdf)):
        sys.exit("PDF not found — set PDF_PATH or pass --pdf")

    import pdfplumber
    from database import supabase
    from aip_structure import AERODROMES, STANDARD_36
    from classify_page import classify_page
    from segment_page import segment_aerodrome

    extractors = [(s, extractor_for(s)) for s in a.subsections]
    only = {c.upper() for c in a.only} if a.only else None
    plan, tally = [], {"same": 0, "CHANGED": 0, "count": 0, "skipped": 0}

    with pdfplumber.open(os.path.expanduser(a.pdf)) as pdf:
        for icao, name, start, end in AERODROMES:
            if icao not in STANDARD_36 or (only and icao not in only):
                continue
            words = {}
            for p in range(start, end + 1):
                page = pdf.pages[p - 1]
                words[p] = [(w["x0"], w["top"], w["x1"], w["bottom"], w["text"])
                            for w in page.extract_words() if w["text"].strip()]
                page.flush_cache()
            segments = segment_aerodrome(icao, words, classify_page)
            for sub, ex in extractors:
                segs = [s for s in segments if s.subsection == sub]
                if not segs:
                    print(f"  {icao} {sub}: no segment — skipped, stored rows kept")
                    tally["skipped"] += 1
                    continue
                result = ex.extract(icao, segs)
                errors = [i.message for i in ex.validate(result) if i.severity == "error"]
                if errors or not result.records:
                    print(f"  {icao} {sub}: validation failed — skipped, stored rows kept: "
                          f"{errors or 'no records'}")
                    tally["skipped"] += 1
                    continue
                status, details = compare(stored(supabase, icao, sub), result.records)
                tally[status] += 1
                print(f"  {icao} {sub}: {status}")
                for d in details[:8]:
                    print(f"      {d}")
                plan.append((icao, sub, result.records, status))

    print(f"\nsame {tally['same']}  CHANGED {tally['CHANGED']}  "
          f"count {tally['count']}  skipped {tally['skipped']}")
    if not a.write:
        print("Dry run — nothing written. Add --write to store.")
        return 0
    if (tally["CHANGED"] or tally["count"]) and not a.allow_changes:
        print("Not writing: stored values would change. Check the lines above; "
              "for a new AIRAC amendment re-run with --allow-changes.")
        return 1
    for icao, sub, records, _ in plan:
        write(supabase, icao, sub, records)
    print(f"Written: {len(plan)} subsection(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
