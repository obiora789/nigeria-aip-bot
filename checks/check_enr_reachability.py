#!/usr/bin/env python3
"""
check_enr_reachability.py — Part B: can a pilot actually REACH the ENR data?

Part A (the coverage audit) proved the data is IN aip_facts: 427 entities,
every category matching. This proves something different and, on the evidence
of this project, more fragile: that asking about an entity in plain English
actually resolves to that entity's scope.

WHY THIS EXISTS
---------------
"Where is Kelak?" was refused as out_of_scope while KELAK sat correctly in
aip_facts the entire time. No coverage check could have caught that — the data
was never missing. The failure was in classification: the LLM guessed
out_of_scope, a membership check correctly identified KELAK, and a THIRD piece
of logic that only understood aerodromes overruled it.

Same class, earlier in the session: "What is the frequency of ABC?" and MIU
(both VOR idents), and "Where is TEMSA?" — all real, all published, all refused
or misrouted at the classification layer, never at retrieval.

WHAT THIS TESTS, AND WHAT IT DOESN'T
------------------------------------
Tests the CLASSIFICATION layer only: agent.extract_query_parameters() plus
_backstop(), then resolver.resolve(). It answers "would this query reach the
right scope?" — one OpenAI call per question, no embeddings, no vector search.

It does NOT test retrieval or rendering. That is deliberate: every reachability
bug actually found in this project has been a classification failure, and once
a query resolves to the correct scope, search_facts_scoped has been reliable.
Testing the full pipeline costs an embedding + vector search + possible
synthesis per question, for a layer that has not been the problem. If this
comes back clean, THAT is when a full-pipeline run earns its cost.

A PASS here means the query reached the right scope. It does not guarantee the
reply reads well — only that the bot went to the right place to build it.

USAGE
    # sample 12 entities per kind (default), 3 phrasings each
    python checks/check_enr_reachability.py

    # every entity — slower and more expensive, but exhaustive
    python checks/check_enr_reachability.py --all

    # one kind only
    python checks/check_enr_reachability.py --kind ENR_POINT

    # more phrasings per entity
    python checks/check_enr_reachability.py --phrasings 5

    # airspace field questions, through the live bot's AD 2.17 redirect
    python checks/check_enr_reachability.py --kind ENR_AIRSPACE --all --fields

Requires the real environment (OPENAI_API_KEY, SUPABASE_URL, SUPABASE_KEY) —
it queries live aip_facts for the entity list and calls the real classifier.
"""
import sys as _sys, pathlib as _pl  # repo layout: bot at the top, scripts in folders
_R = next(d for d in _pl.Path(__file__).resolve().parents if (d / "main.py").exists())
_sys.path[1:1] = [str(p) for p in (_R, _R / "checks", _R / "ingestion", _R / "ingestion" / "validate")
                  if str(p) not in _sys.path]
import argparse
import random
import sys
import time
from collections import defaultdict

try:
    import agent
    import resolver
    from database import supabase
except ImportError as e:  # noqa: BLE001
    print(f"Cannot import the bot modules: {e}")
    print("Run this from the repo root with the venv active.")
    sys.exit(2)


# Phrasings a pilot would plausibly use. Ordered most-natural first, so a
# --phrasings 1 run still tests the realistic case rather than a contrived one.
#
# "{e}" alone is deliberately included: a pilot typing just the identifier is
# extremely common, and it is also the HARDEST case for the classifier, since
# there is no surrounding context at all to signal that it is aviation data.
_PHRASINGS = [
    "Where is {e}?",
    "What is {e}?",
    "{e}",
    "Tell me about {e}",
    "{e} details",
]

# FIELD QUESTIONS (2026-11-19, audit item N1). The phrasings above ask WHERE
# or WHAT an entity is. A field question ("vertical limits", "class") is what
# triggers main._airspace_redirect, which can send an airspace question to an
# aerodrome's AD 2.17. --fields adds these for the kinds listed here.
_FIELD_PHRASINGS = {
    "ENR_AIRSPACE": [
        "{e} vertical limits",
        "What is the airspace class of {e}?",
        "lateral limits of {e}",
    ],
}

# Kinds whose ids are long descriptive strings rather than short identifiers.
# Asking "Where is ABUJA Terminal Control Area (TMA)?" is unnatural — a pilot
# would say "Abuja TMA". These get a shortened form so the test reflects real
# usage rather than a strawman the bot was never meant to handle.
_LONG_ID_KINDS = {"ENR_AIRSPACE", "ENR_FRA_DCT"}


def _navaid_short(name: str) -> str:
    """"ABUJA VOR/DME (ABC)" -> "ABC". A pilot uses the ident, not the
    expanded station name the route tables print."""
    if "(" in name and ")" in name:
        inner = name.split("(")[1].split(")")[0].strip()
        if inner.isalpha() and 2 <= len(inner) <= 4:
            return inner
    return name.strip()


def shorten(scope_kind: str, scope_id: str) -> str:
    """A realistic way to refer to this entity in conversation.

    This matters for fairness of the test. Asking "Where is ABUJA Terminal
    Control Area (TMA)?" is a strawman — nobody types that, and a failure on
    it would tell us nothing useful. The point is to test the phrasings a
    pilot actually uses, so a FAIL here means a real reachability gap rather
    than an artificially awkward input."""
    if scope_kind == "ENR_AIRSPACE":
        # "ABUJA Terminal Control Area (TMA)" -> "ABUJA TMA"
        if "(" in scope_id and ")" in scope_id:
            head = scope_id.split("(")[0].strip()
            abbr = scope_id.split("(")[1].split(")")[0].strip()
            # Every word before "Terminal", as resolver._airspace_aliases
            # does: "PORT HARCOURT ... (TMA)" -> "PORT HARCOURT TMA". Taking
            # only the first word asked about "PORT TMA", which nobody says
            # and nothing publishes (6 false FAILs, 2026-11-20).
            city = []
            for w in head.split():
                if w in ("Terminal", "Control", "Area"):
                    break
                city.append(w)
            return f"{' '.join(city) or head.split()[0]} {abbr}"
        # "KANO Flight Information Region" -> "KANO FIR"
        if "Flight Information Region" in scope_id:
            return f"{scope_id.split()[0]} FIR"
        # "KANO EAST SECTOR" is already how it is spoken.
        return scope_id
    if scope_kind == "ENR_FRA_DCT":
        # "KELAK-POLTO#2" -> "KELAK to POLTO". The #2 is our own collision
        # suffix for two real routings sharing endpoints — not something a
        # pilot types, so it is stripped.
        base = scope_id.split("#")[0]
        if "-" in base:
            a, _, b = base.partition("-")
            return f"{_navaid_short(a)} to {_navaid_short(b)}"
        return base
    return scope_id


def load_entities():
    """{scope_kind: [scope_id, ...]} straight from live aip_facts."""
    out = defaultdict(set)
    page, size = 0, 1000
    while True:
        res = (supabase.table("aip_facts")
               .select("scope_kind, scope_id")
               .like("scope_kind", "ENR%")
               .range(page * size, page * size + size - 1)
               .execute())
        batch = res.data or []
        for r in batch:
            if r.get("scope_kind") and r.get("scope_id"):
                out[r["scope_kind"]].add(r["scope_id"])
        if len(batch) < size:
            break
        page += 1
    return {k: sorted(v) for k, v in out.items()}


def reaches(question: str, want_kind: str, want_id: str):
    """(ok, detail) — did this question resolve to the intended scope?

    Accepts a scope-AMBIGUOUS result as a pass: a VOR ident that is also an
    aerodrome (ABC, LAG, MIU) is genuinely two things, and asking the pilot
    which they meant is the correct behaviour, not a failure."""
    try:
        ex = agent.extract_query_parameters(question)
    except Exception as e:  # noqa: BLE001
        return False, f"extract raised {type(e).__name__}"
    if ex is None:
        return False, "extraction returned None"

    if ex.intent == "out_of_scope":
        return False, "REFUSED as out_of_scope"

    try:
        res = resolver.resolve(ex)
    except Exception as e:  # noqa: BLE001
        return False, f"resolve raised {type(e).__name__}"

    # The step the live bot applies after resolve(): an airspace question
    # about an aerodrome's own airspace is answered from that aerodrome's
    # AD 2.17. Applying it here measures where the bot ACTUALLY goes.
    redirected = _apply_redirect(ex, res, question)
    if redirected is not None:
        res = redirected

    if getattr(res, "ambiguous", None):
        # Asking is correct when the name genuinely means two things.
        return True, f"asks (ambiguous: {', '.join(res.ambiguous)[:40]})"
    if getattr(res, "unresolved", False):
        return False, f"unresolved: {(res.reason or '')[:60]}"

    got_kind = getattr(res, "scope_kind", "") or ""
    got_id = getattr(res, "scope_id", "") or ""
    if got_kind == want_kind and got_id.upper() == want_id.upper():
        return True, f"{got_kind}/{got_id}"
    if got_id.upper() == want_id.upper():
        return True, f"{got_kind}/{got_id} (kind differs, id correct)"
    note = " (by the AD 2.17 redirect)" if redirected is not None else ""
    return False, f"went to {got_kind or '?'}/{got_id or '?'}{note}"


_REDIRECT = None


def _apply_redirect(ex, res, question):
    """main._airspace_redirect, imported on first use (main loads the whole
    bot). Returns the redirected Resolution, or None."""
    global _REDIRECT
    if _REDIRECT is None:
        import main as _main
        _REDIRECT = _main._airspace_redirect
    return _REDIRECT(ex, res, question)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", help="test one scope_kind only")
    ap.add_argument("--sample", type=int, default=12,
                    help="entities per kind (default 12)")
    ap.add_argument("--all", action="store_true",
                    help="every entity — slower, exhaustive")
    ap.add_argument("--phrasings", type=int, default=3,
                    help="phrasings per entity, 1-5 (default 3)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--fields", action="store_true",
                    help="also ask field questions (e.g. airspace vertical "
                         "limits), which exercise the AD 2.17 redirect")
    a = ap.parse_args()

    random.seed(a.seed)
    phrasings = _PHRASINGS[:max(1, min(a.phrasings, len(_PHRASINGS)))]

    print("Loading entities from live aip_facts...")
    entities = load_entities()
    if a.kind:
        entities = {k: v for k, v in entities.items() if k == a.kind}
        if not entities:
            print(f"No entities for kind {a.kind!r}")
            return 2

    total = sum(len(v) for v in entities.values())
    print(f"{total} entities across {len(entities)} kinds\n")

    failures = []
    counts = {}
    for kind in sorted(entities):
        ids = entities[kind]
        chosen = ids if a.all else random.sample(ids, min(a.sample, len(ids)))
        ok_n = bad_n = 0
        print(f"=== {kind} — testing {len(chosen)} of {len(ids)} ===")
        for scope_id in chosen:
            spoken = shorten(kind, scope_id)
            for tmpl in phrasings + (_FIELD_PHRASINGS.get(kind, []) if a.fields else []):
                q = tmpl.format(e=spoken)
                ok, detail = reaches(q, kind, scope_id)
                if ok:
                    ok_n += 1
                else:
                    bad_n += 1
                    failures.append((kind, scope_id, q, detail))
                    print(f"   FAIL  {q!r}  ->  {detail}")
                time.sleep(0.05)  # be gentle with the API
        counts[kind] = (ok_n, bad_n)
        n = ok_n + bad_n
        print(f"   {ok_n}/{n} reachable "
              f"({100 * ok_n // n if n else 0}%)\n")

    print("=" * 66)
    print(f"{'kind':16}{'ok':>6}{'fail':>6}{'rate':>8}")
    print("-" * 66)
    t_ok = t_bad = 0
    for kind in sorted(counts):
        ok_n, bad_n = counts[kind]
        n = ok_n + bad_n
        t_ok += ok_n
        t_bad += bad_n
        print(f"{kind:16}{ok_n:>6}{bad_n:>6}{(100*ok_n//n if n else 0):>7}%")
    print("-" * 66)
    n = t_ok + t_bad
    print(f"{'TOTAL':16}{t_ok:>6}{t_bad:>6}{(100*t_ok//n if n else 0):>7}%")

    if failures:
        print(f"\n{len(failures)} failing queries. Grouped by cause:")
        by_cause = defaultdict(list)
        for kind, sid, q, detail in failures:
            cause = ("out_of_scope refusal" if "out_of_scope" in detail
                     else "unresolved" if "unresolved" in detail
                     else "wrong scope" if "went to" in detail
                     else detail[:40])
            by_cause[cause].append(f"{kind}/{sid}: {q!r}")
        for cause, items in sorted(by_cause.items(),
                                   key=lambda kv: -len(kv[1])):
            print(f"\n  {cause} — {len(items)}")
            for it in items[:8]:
                print(f"     {it}")
            if len(items) > 8:
                print(f"     ... +{len(items) - 8} more")
        print("\nA cluster under one cause is a systematic bug worth fixing.")
        print("Scattered one-offs are usually phrasing, not a defect.")
        return 1

    print("\nAll sampled queries reached their intended scope.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
