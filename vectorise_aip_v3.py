#!/usr/bin/env python3
"""
vectorise_aip_v3.py — Layer-2-driven ingestion.

WHAT CHANGED FROM v2, AND WHY
-----------------------------
v2 walked the PDF page by page and character-chunked the text. For AD 2 that was
always a compromise: a chunk boundary could fall between a subsection header and
its own values, and a dense multi-entity table (runways, navaids, comms) could be
split so that one entity's number sat next to another entity's label. That is the
misattribution failure class this whole rebuild exists to eliminate.

v3 does not chunk AD 2 by characters at all. Layer 2 has already resolved each
subsection into a coherent, validated unit — one extractor per AD 2.NN, each
proven against all 36 aerodromes. v3's job is to take those units and store them,
not to re-derive structure from raw text.

Two destinations, deliberately separate:

  aip_knowledge_base  — one embedded chunk per (aerodrome, subsection). This is
                        the RETRIEVAL surface. Because a chunk is exactly one
                        subsection, a chunk can never straddle a header/value
                        boundary or mix two subsections' numbers.

  aip_structured      — the typed per-entity records (jsonb). This is the EXACT
                        LOOKUP surface. Answering "TORA for RWY 18L" from here is
                        a key lookup, not a similarity search over prose — which
                        is the only way to make that answer safe. Generalises the
                        aip_declared_distances approach to every subsection that
                        produces records.

ONE PASS, NOT 23
----------------
Loading a page's words and running classify_page + segment_aerodrome is the
expensive part. Running 23 separate extractor scripts pays that cost 23 times
over. Here each aerodrome's pages are loaded once, segmented once, then fanned
out to every extractor.

SCOPE: THIS FILE HANDLES AD 2 ONLY
----------------------------------
Layer 2 covers AD 2. GEN, ENR, AD 1.x and front matter have no extractors and
still need the generic page-walk — keep v2's non-AD-2 path (see ingest_generic
below). Deleting v2 outright would silently drop those parts of the AIP from the
index.

RUN ORDER
---------
    python extract_charts.py      # populates aip_charts (plate exclusion needs it)
    python run_validators.py      # gate — do not ingest on a red light
    python vectorise_aip_v3.py

Requires pdfplumber (ingestion-only dependency; the running bot does not need it).
"""
import gc
import json
import os
import re
import sys
import time

import pdfplumber
from openai import OpenAI

import config
from database import supabase

from aip_structure import AERODROMES, STANDARD_36
from classify_page import classify_page
from segment_page import segment_aerodrome

from ad21_extractor import AD21Extractor
from ad22_extractor import AD22Extractor
from ad23_extractor import AD23Extractor
from ad24_extractor import AD24Extractor
from ad25_extractor import AD25Extractor
from ad26_extractor import AD26Extractor
from ad27_extractor import AD27Extractor
from ad28_extractor import AD28Extractor
from ad29_extractor import AD29Extractor
from ad210_extractor import AD210Extractor
from ad211_extractor import AD211Extractor
from ad212_extractor import AD212Extractor
from ad213_extractor import AD213Extractor
from ad214_extractor import AD214Extractor
from ad215_extractor import AD215Extractor
from ad216_extractor import AD216Extractor
from ad217_extractor import AD217Extractor
from ad218_extractor import AD218Extractor
from ad219_extractor import AD219Extractor
from ad220_extractor import AD220Extractor
from ad221_extractor import AD221Extractor
from ad222_extractor import AD222Extractor
from ad223_extractor import AD223Extractor

client = OpenAI(api_key=config.OPENAI_API_KEY)

# PDF_PATH is an environment variable, read directly — NOT a config.py
# attribute. Confirmed by grepping vectorise_aip_v2.py: it does
# `PDF_PATH = os.getenv("PDF_PATH")` at module level and passes that straight
# to fitz.open(). Matched here rather than inventing a config.AIP_PDF_PATH
# that doesn't exist anywhere in this codebase.
PDF_PATH = os.getenv("PDF_PATH")

KB_TABLE = "aip_knowledge_base"
STRUCT_TABLE = "aip_structured"
PROGRESS_FILE = "vectorise_v3_progress.json"
SPAN22_FILE = "_span22.json"
RATE_PAUSE = 0.15
EMBED_CHAR_LIMIT = 7500          # get_embedding truncates at 8000; stay under it

# Segment-driven extractors, in document order. AD 2.22 is NOT here — it is
# page-driven (see run_aerodrome) because its PBN coding-table pages classify as
# CHART_PLATE and are therefore invisible to segment_aerodrome by design.
SEGMENT_EXTRACTORS = [
    ("2.1", AD21Extractor()),   ("2.2", AD22Extractor()),
    ("2.3", AD23Extractor()),   ("2.4", AD24Extractor()),
    ("2.5", AD25Extractor()),   ("2.6", AD26Extractor()),
    ("2.7", AD27Extractor()),   ("2.8", AD28Extractor()),
    ("2.9", AD29Extractor()),   ("2.10", AD210Extractor()),
    ("2.11", AD211Extractor()), ("2.12", AD212Extractor()),
    ("2.13", AD213Extractor()), ("2.14", AD214Extractor()),
    ("2.15", AD215Extractor()), ("2.16", AD216Extractor()),
    ("2.17", AD217Extractor()), ("2.18", AD218Extractor()),
    ("2.19", AD219Extractor()), ("2.20", AD220Extractor()),
    ("2.21", AD221Extractor()), ("2.23", AD223Extractor()),
]

# AD 2.22 splits on its own heading numbers. Chunking it at all is not a
# stylistic choice: a full 2.22 span runs well past the embedding character
# limit, so a single chunk would be silently truncated at embed time. Splitting
# on the source's own 2.22.N headings keeps every chunk a whole procedure unit.
_H22_SPLIT_RE = re.compile(r'(?m)^(?=\s*2\.22\.\d+\b)')


def get_embedding(text, retries=4):
    for attempt in range(retries):
        try:
            r = client.embeddings.create(
                input=text[:8000], model=config.EMBEDDING_MODEL)
            return r.data[0].embedding
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            wait = 2 ** attempt * 3
            print(f"  embed error ({e}) — retry in {wait}s", flush=True)
            time.sleep(wait)


def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"done": [], "kb_chunks": 0, "records": 0}


def save_progress(prog):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(prog, f, indent=2)


def load_span22():
    """{icao: (start_page, end_page)} for each aerodrome's AD 2.22 span, from
    the span survey. AD222Extractor needs this — it reads raw pages, not
    segments."""
    if not os.path.exists(SPAN22_FILE):
        return {}
    raw = json.load(open(SPAN22_FILE))
    return {icao: (d["s22"], d["end22"])
            for icao, d in raw.items() if d.get("s22")}


def load_pages(pdf, start, end):
    """Word tuples for a page range, flushing each page's cache as we go —
    these spans reach 44 pages on the busiest aerodromes."""
    out = {}
    for p in range(start, end + 1):
        page = pdf.pages[p - 1]
        out[p] = [(w['x0'], w['top'], w['x1'], w['bottom'], w['text'])
                  for w in page.extract_words() if w['text'].strip()]
        page.flush_cache()
    return out


def kb_save(icao, subsection, content, embedding, page_num, chunk_idx):
    """One retrieval chunk. aip_section carries the exact subsection ('AD 2.13'),
    which is what lets database.get_section_text() reassemble a whole subsection
    and what the refuse-to-source guards use to fetch a section BY NAME rather
    than by similarity.

    The conflict key is (reference_tag, aip_section, source_page, source_chunk)
    — aip_section is REQUIRED here, not optional. Confirmed a real, consequential
    bug without it: on short-document aerodromes (e.g. DNBB, 9 total pages),
    multiple different AD 2.x subsections legitimately start on the SAME
    physical page (AD 2.1 and AD 2.2 both begin on page 434). Without
    aip_section in the key, two genuinely different subsections' chunk0 rows
    collide on an identical (reference_tag, source_page, source_chunk) —
    and this collision happens WITHIN a single ingestion run, between two new
    rows, not against stale prior data, so deleting old rows first does not
    help. v2's original constraint (reference_tag, source_page, source_chunk)
    was safe under v2's design ONLY because it never produced more than one
    independent chunk per page in the first place — that assumption does not
    hold for v3's per-subsection chunking."""
    payload = {
        "aip_part": "AD",
        "aip_section": f"AD {subsection}",
        "reference_tag": icao,
        "content": content,
        "embedding": embedding,
        "chart_url": None,
        "source_page": page_num,
        "source_chunk": chunk_idx,
        "metadata": {"source": "AIP_2026", "layer": 2, "subsection": subsection},
    }
    try:
        supabase.table(KB_TABLE).upsert(
            payload,
            on_conflict="reference_tag,aip_section,source_page,source_chunk"
        ).execute()
    except Exception:
        try:
            supabase.table(KB_TABLE).insert(payload).execute()
        except Exception as e:  # noqa: BLE001
            print(f"  KB error {icao} {subsection} chunk{chunk_idx}: {e}", flush=True)


def struct_save(icao, subsection, records):
    """Typed per-entity records. Stored as jsonb so one table serves every
    subsection's differing shape without a migration per subsection. record_index
    preserves source order (runway ends, navaids and comms services are all
    order-significant in the source)."""
    if not records:
        return 0
    rows = [{
        "icao_code": icao,
        "subsection": subsection,
        "record_index": i,
        "record": rec,
    } for i, rec in enumerate(records)]
    try:
        supabase.table(STRUCT_TABLE).upsert(
            rows, on_conflict="icao_code,subsection,record_index").execute()
        return len(rows)
    except Exception as e:  # noqa: BLE001
        print(f"  struct error {icao} {subsection}: {e}", flush=True)
        return 0


def embed_payload(result):
    """What gets embedded vs what gets displayed are deliberately different.

    embed_text is the extractor's own retrieval-steering string (field terms a
    pilot would actually type — 'declared distances TORA TODA ASDA LDA'), which
    the header text alone would not surface. content is the faithful capture the
    pilot is shown. Embedding embed_text + content indexes on both the query
    vocabulary and the real values; content alone under-retrieves, embed_text
    alone loses the values."""
    content = result.text or result.embed_text or ""
    steer = result.embed_text or ""
    return (f"{steer}\n\n{content}" if steer and steer not in content
            else content)


def ingest_result(icao, subsection, result, first_page, prog):
    """Store one extractor's output. Everything except AD 2.22 fits in a single
    chunk — that is the point: the subsection IS the chunk, so no boundary can
    fall inside it."""
    kb_written = 0
    text = result.text or result.embed_text or ""
    if not text.strip():
        return 0, 0

    if subsection == "2.22":
        # Split on the source's own heading numbers. source_chunk MUST be
        # strictly monotonic: database.get_section_text() reassembles AD 2.22 by
        # sorting on (source_page, source_chunk), and procedures.py needs that
        # reassembly to be in exact source order before it can scope a single
        # approach. A non-monotonic index here silently scrambles a procedure.
        parts = [p for p in _H22_SPLIT_RE.split(text) if p.strip()]
        merged, buf = [], ""
        for part in parts:
            if len(buf) + len(part) <= EMBED_CHAR_LIMIT:
                buf += part
            else:
                if buf.strip():
                    merged.append(buf)
                buf = part
        if buf.strip():
            merged.append(buf)
        for idx, chunk in enumerate(merged):
            emb = get_embedding(f"{result.embed_text}\n\n{chunk}")
            kb_save(icao, subsection, chunk, emb, first_page, idx)
            kb_written += 1
            time.sleep(RATE_PAUSE)
    else:
        emb = get_embedding(embed_payload(result))
        kb_save(icao, subsection, text, emb, first_page, 0)
        kb_written = 1
        time.sleep(RATE_PAUSE)

    recs_written = struct_save(icao, subsection, result.records)
    return kb_written, recs_written


def run_aerodrome(pdf, icao, name, start, end, span22, prog):
    page_words = load_pages(pdf, start, end)
    segments = segment_aerodrome(icao, page_words, classify_page)

    kb_total = rec_total = 0
    warnings = []

    for subsection, extractor in SEGMENT_EXTRACTORS:
        segs = [s for s in segments if s.subsection == subsection]
        if not segs:
            warnings.append(f"{subsection}: no segment found")
            continue
        result = extractor.extract(icao, segs)
        # Validation is a GATE, not a filter — a hard error means the extractor
        # could not resolve this subsection safely, and a wrong record is worse
        # than a missing one. Skip and surface it rather than store it.
        errors = [i for i in extractor.validate(result) if i.severity == "error"]
        if errors:
            warnings.extend(f"{subsection}: SKIPPED — {i.message}" for i in errors)
            continue
        warnings.extend(f"{subsection}: {w}" for w in result.warnings)
        first_page = min(s.page_index for s in segs)
        kb, recs = ingest_result(icao, subsection, result, first_page, prog)
        kb_total += kb
        rec_total += recs

    # AD 2.22 — page-driven. Its PBN coding-table pages classify as CHART_PLATE
    # (their headers lack the "DNxx AD 2.NN" form), so segment_aerodrome never
    # sees them. They are ordinary text tables, so the extractor reads the raw
    # span pages directly. This is why span22 is required, not optional.
    if icao in span22:
        lo, hi = span22[icao]
        ex22 = AD222Extractor(page_span=span22)
        span_pages = {p: w for p, w in page_words.items() if lo <= p <= hi}
        result = ex22.extract(icao, span_pages)
        errors = [i for i in ex22.validate(result, span_pages)
                  if i.severity == "error"]
        if errors:
            warnings.extend(f"2.22: SKIPPED — {i.message}" for i in errors)
        else:
            kb, recs = ingest_result(icao, "2.22", result, lo, prog)
            kb_total += kb
            rec_total += recs
    else:
        warnings.append("2.22: no span in _span22.json — SKIPPED")

    del page_words
    gc.collect()
    return kb_total, rec_total, warnings


def process(only=None):
    span22 = load_span22()
    if not span22:
        print("WARNING: _span22.json missing — AD 2.22 will be skipped entirely.\n"
              "Run the AD 2.22 span survey first or approach procedures will not\n"
              "be answerable.", flush=True)

    prog = load_progress()
    done = set(prog["done"])
    entries = [e for e in AERODROMES if e[0] in STANDARD_36
               and (only is None or e[0] in only)]

    if not PDF_PATH:
        print("PDF_PATH environment variable is not set — nothing to ingest from.")
        sys.exit(2)

    all_warnings = []
    with pdfplumber.open(PDF_PATH) as pdf:
        for icao, name, start, end in entries:
            if icao in done:
                print(f"  skip {icao} (already done)", flush=True)
                continue
            kb, recs, warns = run_aerodrome(pdf, icao, name, start, end, span22, prog)
            prog["kb_chunks"] += kb
            prog["records"] += recs
            done.add(icao)
            prog["done"] = sorted(done)
            save_progress(prog)
            all_warnings.extend(f"{icao}: {w}" for w in warns)
            print(f"  {icao} ({name}): {kb} chunks, {recs} records", flush=True)

    print(f"\nAD 2 ingestion complete — {prog['kb_chunks']} chunks, "
          f"{prog['records']} structured records.", flush=True)
    if all_warnings:
        print(f"\n{len(all_warnings)} warning(s) — review before trusting the index:")
        for w in all_warnings:
            print(f"  {w}")
    print("\nNext: run the generic (GEN / ENR / AD 1.x) ingest — Layer 2 covers\n"
          "AD 2 only, and those parts of the AIP still need the page-walk path\n"
          "harvested from vectorise_aip_v2.py.", flush=True)


if __name__ == "__main__":
    only = set(a.upper() for a in sys.argv[1:]) or None
    print("This rewrites AD 2 rows in aip_knowledge_base and aip_structured.")
    print("Clear them first:")
    print("  DELETE FROM aip_knowledge_base WHERE aip_part = 'AD';")
    print("  DELETE FROM aip_structured;")
    if input("Confirmed cleared? (yes/no): ").strip().lower() == "yes":
        process(only)
    else:
        print("Aborted.")
