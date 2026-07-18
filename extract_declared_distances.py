"""
extract_declared_distances.py — populate aip_declared_distances from AD 2.13.

Run once (and after each AIRAC update) on a machine with the AIP PDF:
    python extract_declared_distances.py Complete_AIP2026.pdf

What it does, per aerodrome:
  • finds the AD 2.13 "Declared Distances" table (TORA/TODA/ASDA/LDA),
  • parses it into per-runway records with a decimal- AND spaced-thousands-aware
    number splitter ('3 610' -> 3610 ; '893.1 871.15' -> 893.1, 871.15),
  • VALIDATES that the counts align (N runways <-> N values for every metric),
  • upserts clean records to Supabase.

FAIL-SAFE: any aerodrome whose table doesn't parse into count-aligned per-runway
records is SKIPPED (logged, not stored). At query time those aerodromes have no
structured row, so the bot falls back to the refuse-to-source guard — never a
guessed value. Resolving attribution once here, with validation, is what makes
query-time lookups safe.

Requires pdfplumber (a dev/ingestion dependency, not needed by the running bot).
"""
import re
import sys

import pdfplumber

import config
from database import supabase


def _clean(c):
    return re.sub(r"\s+", " ", (c or "").replace("\n", " ")).strip()


def _nums(cell):
    """Split a cell into numbers. Join spaced thousands ('3 610'->'3610') only
    when there's no decimal point; when decimals are present the space is a
    delimiter ('893.1 871.15'->['893.1','871.15'])."""
    if "." not in cell:
        cell = re.sub(r"(?<=\d) (?=\d{3}\b)", "", cell)
    return re.findall(r"\d+(?:\.\d+)?", cell)


def parse_declared(page):
    """Return per-runway records for a page's AD 2.13 table, or None if it does
    not parse into count-aligned records."""
    for t in page.extract_tables():
        header = " ".join((x or "") for x in t[0]).upper()
        if "TORA" not in header or "LDA" not in header:
            continue
        for row in t[1:]:
            cells = [_clean(x) for x in row]
            rwys = re.findall(r"\b\d{2}[LRC]?\b", cells[0] or "")
            if not rwys:            # skip header + the '1 2 3' column-number row
                continue
            metrics = {}
            for key, idx in (("tora", 1), ("toda", 2), ("asda", 3), ("lda", 4)):
                metrics[key] = _nums(cells[idx]) if idx < len(cells) else []
            if not all(len(v) == len(rwys) for v in metrics.values()):
                return None         # count mismatch -> fail safe
            return [{"runway": rwys[j], "tora": metrics["tora"][j],
                     "toda": metrics["toda"][j], "asda": metrics["asda"][j],
                     "lda": metrics["lda"][j]} for j in range(len(rwys))]
    return None


def main(pdf_path):
    stored = skipped = 0
    skips = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if "TORA" not in text or "LDA" not in text or ". . ." in text[:250]:
                continue
            m = re.search(r"AD 2-(DN[A-Z]{2})-", text)
            if not m:
                continue
            icao = m.group(1)
            recs = parse_declared(page)
            if not recs:
                skipped += 1
                skips.append(icao)
                print(f"SKIP {icao}: table did not parse cleanly -> guard fallback")
                continue
            for r in recs:
                row = {"icao": icao, "runway": r["runway"], "tora": r["tora"],
                       "toda": r["toda"], "asda": r["asda"], "lda": r["lda"]}
                supabase.table("aip_declared_distances").upsert(row).execute()
            stored += 1
            print(f"OK   {icao}: " + ", ".join(
                f"{r['runway']}(TORA {r['tora']}/LDA {r['lda']})" for r in recs))
    print(f"\nStored {stored} aerodromes; {skipped} skipped (fall back to guard).")
    if skips:
        print("skipped:", ", ".join(skips))


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "Complete_AIP2026.pdf"
    main(path)
