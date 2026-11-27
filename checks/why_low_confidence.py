#!/usr/bin/env python3
"""
why_low_confidence.py — what the knowledge-base search sees for a question.

D30 (gold 27 Nov): Umueri (DNAN) and Amassoma (DNBY) resolve correctly, yet
their fire category / reference point / fuel questions end at "couldn't find a
confident match" (33-38%) before routing ever reaches the structured answers
the same questions get at every other aerodrome. This shows, per question,
the same steps main.py takes up to that gate:

  extraction  -> intent, aerodrome name, AD 2 subsection the model chose
  resolution  -> ICAO, label, the name hint added to the search text
  search text -> exactly what is embedded
  results     -> top matches (section, similarity) and the gate's verdict

Read-only. Cost: one extraction call and one embedding per question (well
under a cent for the default set).

    python checks/why_low_confidence.py
    python checks/why_low_confidence.py "What is the fire fighting category at Umueri?"
"""
import sys as _sys, pathlib as _pl  # repo layout: bot at the top, scripts in folders
_R = next(d for d in _pl.Path(__file__).resolve().parents if (d / "main.py").exists())
_sys.path[1:1] = [str(p) for p in (_R, _R / "checks", _R / "ingestion", _R / "ingestion" / "validate")
                  if str(p) not in _sys.path]
import sys

import config
import resolver
from agent import extract_query_parameters, get_embedding
from database import search_aip

# The four D30 failures, each beside the same question at an aerodrome that passes.
DEFAULT = [
    "What is the fire fighting category at Umueri?",
    "What is the fire fighting category at Amassoma?",
    "What is the fire fighting category at Asaba?",
    "umueri aerodrome reference point",
    "asaba aerodrome reference point",
    "What fuel types are available at Umueri?",
    "What fuel types are available at Asaba?",
]


def show(question):
    print(f"\n=== {question}")
    ex = extract_query_parameters(question)
    print(f"  extraction : intent={getattr(ex, 'intent', None)!r} "
          f"aerodrome={getattr(ex, 'aerodrome_name', None)!r} "
          f"icao={getattr(ex, 'icao_code', None)!r} "
          f"subsection={getattr(ex, 'ad2_subsection', None)!r}")
    res = resolver.resolve(ex)
    print(f"  resolution : icao={res.icao!r} label={res.label!r} "
          f"part={res.part!r} hint={getattr(res, 'aerodrome_hint', None)!r}")
    text = resolver.build_search_text(ex, res, question)
    print(f"  search text: {text!r}")
    emb = get_embedding(text)
    if emb is None:
        print("  (no embedding returned)")
        return
    out = search_aip(emb, res, getattr(ex, "procedure_type", "") or "",
                     getattr(ex, "runway", "") or "")
    verdict = (f"ABSTAINED ({out.reason})" if out.abstained else "passed the gate")
    print(f"  gate       : best {out.max_similarity:.3f} vs threshold "
          f"{config.SIMILARITY_THRESHOLD:.2f} -> {verdict} "
          f"[part={out.used_part} ref={out.used_reference}]")
    for r in (out.results or [])[:5]:
        snippet = " ".join((r.content or "").split())[:70]
        print(f"     {r.similarity:.3f}  {r.aip_section:<8} {snippet}")


def main(argv):
    for q in (argv or DEFAULT):
        show(q)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
