#!/usr/bin/env python3
"""
airac_check.py — read-only check of an AIP PDF before it is ingested.

Car 3. Run it on every new AIRAC PDF before anything is written, and on the
current PDF to confirm the baseline. Free: no OpenAI calls, no database. It
reads the PDF once with pypdfium2 (about 10 seconds for 1,073 pages).

  1 amendment   the newest "AIRAC AMDT nn/yyyy" printed in the PDF against
                config.AIRAC_CYCLE, and the pages stamped with it — every AIP
                page prints the amendment that last changed it, so those pages
                are what this amendment changed.
  2 boundaries  aip_structure.AERODROMES is hand-verified for one PDF. For each
                aerodrome: its start page prints "AD 2-<ICAO>-1" (or AD 3), the
                page before is not its page, its end page is its own and the
                page after is not. When one fails, the pages that print
                "AD 2-<ICAO>-1" in this PDF are listed — fix the table by hand,
                never by arithmetic (see aip_structure.py).
  3 AD 2.22     _span22.json against the spans build_span22.find_span finds in
                this PDF (needs the boundaries to be right first).
  4 navaids     the ENR 4.1 idents in this PDF against agent.ENR_NAVAIDS, the
                list the model is given.
  5 gold        every gold-set row that names a page: the page is found by its
                printed label, and every must_contain value is on it. Rows that
                fail were changed by the amendment and need a new expected value.

Exit 0 when every check passes, 1 otherwise.

USAGE (from the repo folder; PDF_PATH from .env)
    python ingestion/airac_check.py
    python ingestion/airac_check.py --pdf ~/Downloads/NewAIP.pdf | tee airac_check.txt
"""
import sys as _sys, pathlib as _pl  # repo layout: bot at the top, scripts in folders
_R = next(d for d in _pl.Path(__file__).resolve().parents if (d / "main.py").exists())
_sys.path[1:1] = [str(p) for p in (_R, _R / "checks", _R / "ingestion", _R / "ingestion" / "validate")
                  if str(p) not in _sys.path]
import argparse
import ast
import csv
import json
import os
import re
import sys
from collections import defaultdict

from aip_structure import (AERODROMES, HELIPORT_ICAO, SPECIMEN_ICAO, STANDARD_36,
                           get_own_reference)

_AMDT_RE = re.compile(r"AIRAC\s+AMDT\s+(\d{2})\s*/\s*(\d{4})")
_PART_RE = re.compile(r"^\s*(GEN|ENR|AD)\s*(\d+(?:\.\d+)?)", re.M)
_CHECKED = STANDARD_36 | HELIPORT_ICAO     # DNKK, GEN, NG-INDEX, NG-ENR6 have no AD header
# A page HEADED by an aerodrome's reference: its first line is "AD 2-DNZA-9".
_HEAD_RE = re.compile(r"^\s*AD\s*[23]\s*[-\u2013]?\s*(DN[A-Z]{2})\s*[-\u2013]?\s*\d{1,2}")


# ── reading ────────────────────────────────────────────────────────────────
def read_pages(pdf_path):
    """Text of every page, index 0 = PDF page 1."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf_path)
    out = []
    for i in range(len(doc)):
        page = doc[i]
        tp = page.get_textpage()
        out.append(tp.get_text_range())
        tp.close()
        page.close()
    doc.close()
    return out


def squash(s):
    """For "is this value on the page": no whitespace, one kind of dash and
    quote, case folded. Line breaks and column gaps differ between PDF text
    engines; the characters of a value do not."""
    s = (s or "").replace("’", "'").replace("‘", "'")
    s = re.sub(r"[‐-―−]", "-", s)
    return re.sub(r"\s+", "", s).casefold()


# ── 1 amendment ────────────────────────────────────────────────────────────
def check_amendment(pages, configured):
    stamps = defaultdict(set)                       # (year, nn) -> {page index}
    for i, t in enumerate(pages):
        for nn, yyyy in _AMDT_RE.findall(t):
            stamps[(int(yyyy), int(nn))].add(i)
    if not stamps:
        return False, ["no 'AIRAC AMDT nn/yyyy' found anywhere — is this the AIP?"], None
    year, nn = max(stamps)
    newest = f"AIRAC AMDT {nn:02d}/{year}"
    lines = [f"newest amendment printed: {newest} ({len(stamps[(year, nn)])} pages)",
             f"config.AIRAC_CYCLE:       {configured}"]
    ok = squash(newest) == squash(configured)
    if not ok:
        lines.append("  -> differs. Ingest this PDF, then set AIRAC_CYCLE to "
                     f"'{newest}' (Render env var and config.py default).")
    groups = defaultdict(list)
    for i in sorted(stamps[(year, nn)]):
        entry = next((e for e in AERODROMES if e[2] <= i + 1 <= e[3]), None)
        if entry:
            key = entry[0]
        else:
            m = _PART_RE.search(pages[i][:200])
            key = f"{m.group(1)} {m.group(2)}" if m else "other"
        groups[key].append(i + 1)
    lines.append(f"pages stamped {newest} (what it changed), by part:")
    for key in sorted(groups):
        p = groups[key]
        lines.append(f"  {key:10} {len(p):3} page(s): {_ranges(p)}")
    return ok, lines, newest


def _ranges(nums):
    out, start, prev = [], None, None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}" if start == prev else f"{start}-{prev}")
            start = prev = n
    if start is not None:
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ", ".join(out)


# ── 2 boundaries ───────────────────────────────────────────────────────────
def _ref(pages, icao, page_no):
    if 1 <= page_no <= len(pages):
        return get_own_reference(icao, pages[page_no - 1])
    return None


def check_boundaries(pages):
    problems = []
    for icao, name, start, end in AERODROMES:
        if icao not in _CHECKED:
            continue
        why = []
        if _ref(pages, icao, start) != 1:
            why.append(f"start page {start} does not print AD 2/3-{icao}-1")
        if _ref(pages, icao, start - 1) is not None:
            why.append(f"page {start - 1}, before the start, is also {icao}'s")
        # The last page may be a blank or a chart plate with no reference, and
        # the DNXX airstrip/heliport catalogue sits inside DNZA's and DNWI's
        # ranges by design (aip_structure.SPECIMEN_ICAO). What must not happen:
        # another aerodrome's own page before this range ends.
        p = end
        while p > start and _ref(pages, icao, p) is None:
            m = _HEAD_RE.match(pages[p - 1]) if p <= len(pages) else None
            if m and m.group(1) != icao and m.group(1) not in SPECIMEN_ICAO:
                why.append(f"page {p}, inside the range, is headed {m.group(0).strip()}")
                break
            p -= 1
        if _ref(pages, icao, end + 1) is not None:
            why.append(f"page {end + 1}, after the end, is also {icao}'s")
        if why:
            found = [i + 1 for i, t in enumerate(pages) if get_own_reference(icao, t) == 1]
            problems.append(f"{icao} ({name}) table says {start}-{end}: " + "; ".join(why)
                            + f". Pages printing {icao}-1 here: {found or 'none'}")
    return not problems, problems or [f"all {len(_CHECKED)} aerodrome/heliport ranges match their printed headers"]


# ── 3 AD 2.22 spans ────────────────────────────────────────────────────────
class _Doc:
    """The one method build_span22.find_span uses, over text already read."""
    def __init__(self, pages):
        self._pages = pages

    def load_page(self, i):
        text = self._pages[i]
        return type("P", (), {"get_text": lambda _self, *_a: text})()


def check_span22(pages, span_path):
    from build_span22 import find_span
    if not os.path.exists(span_path):
        return False, [f"{span_path} missing — run ingestion/build_span22.py"]
    stored = json.load(open(span_path))
    doc, diffs = _Doc(pages), []
    for icao, name, start, end in AERODROMES:
        if icao not in STANDARD_36:
            continue
        s22, end22, _note = find_span(doc, start, end)
        old = stored.get(icao)
        new = {"s22": s22, "end22": end22} if s22 is not None else None
        if old != new:
            diffs.append(f"{icao}: stored {old} -> this PDF {new}")
    return not diffs, diffs or [f"{len(stored)} stored spans match this PDF"]


# ── 4 navaids ──────────────────────────────────────────────────────────────
def prompt_navaids(agent_path):
    """agent.ENR_NAVAIDS read from the source, so this needs no OpenAI key."""
    tree = ast.parse(open(agent_path, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "ENR_NAVAIDS"
                                                for t in node.targets):
            return ast.literal_eval(node.value)
    raise RuntimeError("ENR_NAVAIDS not found in agent.py")


def check_navaids(pages, agent_path):
    from enr41_extractor import ENR41Extractor
    hdr = re.compile(r"^\s*ENR\s*4\.1\s*-\s*\d+")
    text = "\n".join(t for t in pages if hdr.match(t))
    if not text:
        return False, ["no ENR 4.1 pages found"]
    ex = ENR41Extractor()
    ex.segment_text = lambda _segs, _blob=text: _blob
    pdf = {r["scope_id"]: r for r in ex.extract("ENR", [1]).records}
    prompt = {ident: (typ, station) for ident, typ, station in prompt_navaids(agent_path)}
    lines = []
    for ident in sorted(set(pdf) - set(prompt)):
        r = pdf[ident]
        lines.append(f"in the PDF, not in agent.ENR_NAVAIDS: "
                     f"(\"{ident}\", \"{r.get('navaid_type')}\", \"{(r.get('station') or '').title()}\")")
    for ident in sorted(set(prompt) - set(pdf)):
        lines.append(f"in agent.ENR_NAVAIDS, not in the PDF: {ident} {prompt[ident]}")
    return not lines, lines or [f"{len(pdf)} ENR 4.1 idents match agent.ENR_NAVAIDS"]


# ── 5 gold ─────────────────────────────────────────────────────────────────
def find_label(pages, label):
    """PDF page numbers that print this page label ("AD 2-DNAA-2", "ENR 4.4-5")."""
    m = re.fullmatch(r"AD\s*[23]\s*-\s*([A-Z]{4})\s*-\s*(\d+)", label.strip())
    if m:
        icao, n = m.group(1), int(m.group(2))
        return [i + 1 for i, t in enumerate(pages) if get_own_reference(icao, t) == n]
    m = re.fullmatch(r"(GEN|ENR|AD)\s*(\d+(?:\.\d+)?)\s*-\s*(\d+)", label.strip())
    if m:
        rx = re.compile(rf"^\s*{m.group(1)}\s*{re.escape(m.group(2))}\s*-\s*{m.group(3)}(?!\d)", re.M)
        return [i + 1 for i, t in enumerate(pages) if rx.search(t)]
    return []


def _on_page(value, page_text):
    """The value's characters are on the page. Tables print the unit once in
    the column heading — AD 2.13 is "TORA (m)" over "1600" — so "1600 m" also
    counts when the bare number is there and the page has that heading."""
    if squash(value) in page_text:
        return True
    m = re.fullmatch(r"\s*([\d][\d\s.]*?)\s*(m|ft)\s*", value)
    return bool(m) and squash(m.group(1)) in page_text and f"({m.group(2)})" in page_text


def check_gold(pages, gold_path):
    rows = [r for r in csv.DictReader(open(gold_path, encoding="utf-8"))
            if r.get("page", "").strip() and r.get("verified") in ("pdf", "yes")]
    squashed, failed = {}, []
    for r in rows:
        where = find_label(pages, r["page"])
        if not where:
            failed.append(f"{r['id']}: page {r['page']} not found")
            continue
        text = "".join(squashed.setdefault(p, squash(pages[p - 1])) for p in where)
        missing = [v.strip() for v in r["must_contain"].split("|")
                   if v.strip() and not _on_page(v, text)]
        if missing:
            failed.append(f"{r['id']} ({r['page']}): no longer on the page: {missing}")
    head = f"{len(rows) - len(failed)}/{len(rows)} gold rows still match their page"
    return not failed, [head] + failed


# ── main ───────────────────────────────────────────────────────────────────
def main(argv=None):
    import config
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--pdf", default=os.getenv("PDF_PATH"))
    ap.add_argument("--span22", default="_span22.json")
    ap.add_argument("--gold", default=str(_R / "gold_set.csv"))
    a = ap.parse_args(argv)
    if not a.pdf or not os.path.exists(os.path.expanduser(a.pdf)):
        sys.exit("PDF not found — set PDF_PATH or pass --pdf")

    pages = read_pages(os.path.expanduser(a.pdf))
    print(f"{a.pdf}: {len(pages)} pages\n")
    results = []
    amd_ok, amd_lines, _ = check_amendment(pages, config.AIRAC_CYCLE)
    results.append(("1 amendment", amd_ok, amd_lines))
    b_ok, b_lines = check_boundaries(pages)
    results.append(("2 boundaries", b_ok, b_lines))
    if b_ok:
        results.append(("3 AD 2.22", *check_span22(pages, a.span22)))
    else:
        results.append(("3 AD 2.22", False, ["not checked — fix the boundaries first"]))
    results.append(("4 navaids", *check_navaids(pages, str(_R / "agent.py"))))
    results.append(("5 gold", *check_gold(pages, a.gold)))

    for title, ok, lines in results:
        print(f"{'PASS' if ok else 'FAIL'}  {title}")
        for ln in lines:
            print(f"      {ln}")
        print()
    bad = [t for t, ok, _ in results if not ok]
    print("All checks pass." if not bad else f"{len(bad)} check(s) need attention: {', '.join(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
