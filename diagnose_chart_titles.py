#!/usr/bin/env python3
"""
diagnose_chart_titles.py — are SID/STAR charts stored as "RNAV Approach Chart"?

READ-ONLY and free: reads aip_charts and the AIP PDF (PDF_PATH in .env),
changes nothing, uploads nothing, calls no model.

Why (2026-11-21): the chart table has no 'SID Chart' row anywhere and one
'STAR Chart' (Abuja), yet the aerodromes that publish SIDs and STARs have far
more 'RNAV Approach Chart' pages than the others (Abuja 7, Kano 8, Benin 5,
Amassoma 5, Kashimbila 4 — most aerodromes have 2). In extract_charts.py the
RNAV pattern "(RNAV|GNSS|RNP) ... RWY nn" is tried BEFORE the SID and STAR
patterns, so a title like "RNAV SID RWY 18R" would be filed as an RNAV
approach for 18R. This prints each approach-labelled page's real title so
that can be confirmed or ruled out before anything is re-ingested.

    python diagnose_chart_titles.py              # every aerodrome
    python diagnose_chart_titles.py DNAA DNKN    # just these
"""
import os
import re
import sys

import fitz
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
_BOILER = re.compile(r"(?:AD\s*[23][-–]\w+[-–]\d+\s+)?NIGERIA\s+AIP"
                     r"(?:\s+NIGERIAN\s+AIRSPACE\s+MANAGEMENT\s+AGENCY)?", re.I)
_NOT_APPROACH = re.compile(r"\bSID\b|\bSTAR\b|DEPARTURE|ARRIVAL", re.I)


def title_of(page) -> str:
    """The same top-15% title zone extract_charts.detect_from_title reads."""
    top = page.rect.height * 0.15
    blocks = sorted((b for b in page.get_text("blocks") if b[1] <= top), key=lambda b: b[1])
    text = " ".join(" ".join(b[4].split()) for b in blocks)[:300]
    return _BOILER.sub("", text).strip()


def main():
    only = {a.upper() for a in sys.argv[1:]}
    sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
    rows = (sb.table("aip_charts")
              .select("icao_code, procedure_type, runway, source_page")
              .ilike("procedure_type", "%approach chart%")
              .order("icao_code").order("source_page")
              .execute().data or [])
    doc = fitz.open(os.getenv("PDF_PATH"))
    suspect = 0
    for r in rows:
        if only and r["icao_code"] not in only:
            continue
        page = doc[int(r["source_page"]) - 1]
        title = title_of(page)
        flag = "  <-- NOT AN APPROACH?" if _NOT_APPROACH.search(title) else ""
        suspect += bool(flag)
        print(f"{r['icao_code']} p{r['source_page']:<4} stored as {r['procedure_type']:<22} "
              f"RWY {r['runway'] or '-':<4} | {title[:110]}{flag}")
    print(f"\n{suspect} page(s) stored as an approach chart whose own title says "
          f"SID / STAR / departure / arrival.")


if __name__ == "__main__":
    main()
