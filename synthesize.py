"""
synthesize.py — Vannie's grounded-synthesis layer (the role upgrade).

Vannie can now answer, compare, and COMPUTE — but the LLM may use ONLY the
retrieved AIP excerpts, and a deterministic verifier checks every number it
asserts against the source before anything reaches the pilot. If verification
fails, the caller falls back to verbatim chunk display. It fails SAFE: an
unverified synthesized answer is never shown.

Two functions:
  generate_grounded_answer(question, results) -> GroundedAnswer   (LLM call)
  verify_grounded_answer(answer, context)      -> (ok, issues)    (pure, deterministic)
"""
import logging
import re
from typing import List, Tuple

from openai import OpenAI

import config
import subsection_router
from retry import retry_call
from models import AIPResult
from schemas import GroundedAnswer

log = logging.getLogger("vannie.synthesize")
_client = OpenAI(api_key=config.OPENAI_API_KEY)

# A number token: integers with optional thousands commas and optional decimals.
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
# Simple binary arithmetic 'A op B = C' (operators - + x * /).
_ARITH = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*([-+x*/])\s*(\d[\d,]*(?:\.\d+)?)\s*=\s*(\d[\d,]*(?:\.\d+)?)")


def _nums(s: str) -> set:
    # The AIP writes thousands with a space ('3 610', '1 122'). Collapse those
    # so a value matches whether spaced or not, and so arithmetic the model does
    # on unspaced numbers ('3900 - 2745') verifies against spaced source text.
    s = re.sub(r"(\d)\s+(\d{3})(?!\d)", r"\1\2", s or "")
    return {m.replace(",", "") for m in _NUM.findall(s)}


def _fmt_context(results: List[AIPResult]) -> str:
    blocks = []
    for i, r in enumerate(results[: config.SYNTHESIS_CONTEXT_CHUNKS], 1):
        tag = r.aip_section or r.reference_tag or "AIP"
        blocks.append(f"--- Excerpt {i} [{tag}] ---\n{r.content.strip()}")
    return "\n\n".join(blocks)


def generate_grounded_answer(question: str, results: List[AIPResult]) -> GroundedAnswer | None:
    """Ask the model for a grounded answer over the excerpts. Returns None on error
    (caller then falls back to verbatim display)."""
    if not results:
        return None
    context = _fmt_context(results)
    user = (f"Question: {question}\n\n"
            f"AIP excerpts (the ONLY source you may use):\n{context}")
    try:
        resp = retry_call(
            _client.beta.chat.completions.parse,
            model=config.SYNTHESIS_MODEL,
            messages=[{"role": "system", "content": config.SYNTHESIS_SYSTEM},
                      {"role": "user", "content": user}],
            response_format=GroundedAnswer,
            temperature=0,
        )
        return resp.choices[0].message.parsed
    except Exception:  # noqa: BLE001
        log.exception("grounded generation failed")
        return None


def _norm(s: str) -> str:
    """Whitespace-collapsed, case-folded form for verbatim-substring checks. The
    AIP's own extracted text has irregular spacing (multi-space gaps, wrapped
    lines); this keeps a real verbatim quote matchable without allowing anything
    looser than that."""
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def verify_grounded_answer(ans: GroundedAnswer, results: List[AIPResult]) -> Tuple[bool, List[str]]:
    """Deterministic anti-hallucination AND anti-misattribution check.

    This checks each fact against its OWN cited excerpt (results[source_excerpt-1])
    rather than a flattened blob of every retrieved chunk. That distinction is
    the fix for the multi-entity misattribution class: the old version verified
    a number against the union of ALL retrieved excerpts, so a real value from
    Excerpt 3 verified successfully even when the reply's citation pointed at
    Excerpt 1's (wrong) section — the number-verifier "passing" was never proof
    the number came from where the answer said it did. Checking per-excerpt
    closes that gap for numbers, and — because every fact (numeric or prose) now
    requires a verbatim substring match against its cited excerpt, not just a
    digit-membership check — it also closes the second gap this surfaced: a
    purely prose claim with zero digits (e.g. a stated VFR restriction) used to
    skip verification entirely, since _nums() found nothing to check. A fact
    with no digits still must appear verbatim in its cited excerpt now.

    PASSES only if:
      - answerable=False (nothing to verify), OR
      - facts_used is non-empty, AND every fact cites a valid excerpt index,
        AND every fact's own numbers appear in THAT excerpt (not elsewhere),
        AND the fact's value itself appears verbatim (whitespace-collapsed) in
        THAT excerpt, AND every arithmetic step's operands come from a
        successfully-verified fact, AND every number in the final answer text
        is either from a verified fact or a shown computation result.
    Any violation -> (False, issues); the caller must then NOT show this answer.
    """
    issues: List[str] = []
    if not ans.answerable:
        return (True, issues)

    if not ans.facts_used:
        issues.append("answerable=True but facts_used is empty — nothing to "
                      "verify a prose or numeric claim against")
        return (False, issues)

    cited_nums = set()
    for f in ans.facts_used:
        idx = getattr(f, "source_excerpt", None)
        if not idx or not (1 <= idx <= len(results)):
            issues.append(f"fact '{f.what}' cites invalid excerpt #{idx}")
            continue
        excerpt = results[idx - 1].content
        exc_nums = _nums(excerpt)
        fact_nums = _nums(f.value)
        bad_nums = fact_nums - exc_nums
        if bad_nums:
            issues.append(f"fact '{f.what}' value {f.value!r} has number(s) "
                          f"{sorted(bad_nums)} not present in its cited "
                          f"excerpt #{idx}")
            continue
        if _norm(f.value) not in _norm(excerpt):
            issues.append(f"fact '{f.what}' value {f.value!r} is not verbatim "
                          f"in its cited excerpt #{idx}")
            continue
        # This excerpt has now EARNED trust: at least one of its facts verified
        # verbatim against it. Admit the excerpt's WHOLE number set (not just
        # the literal fact.value numbers) into cited_nums — this lets the final
        # answer restate an incidental identifier from that same trusted
        # excerpt (a runway designator like "18R" in "RWY 18R/36L is longer")
        # without requiring a separate facts_used line for every digit, while
        # still refusing any number from an excerpt no fact ever verified
        # against (that's what CASE 3 / the tampered-PCN case below catch).
        cited_nums |= exc_nums

    computed = set()
    comp = (ans.computation or "").strip()
    if comp:
        found = _ARITH.findall(comp)
        if not found:
            issues.append("computation present but unparseable")
        for a, op, b, c in found:
            an, bn, cn = a.replace(",", ""), b.replace(",", ""), c.replace(",", "")
            for x in (an, bn):
                if x not in cited_nums:
                    issues.append(f"computation operand {x} not from a "
                                  f"verified fact")
            try:
                av, bv, cv = float(an), float(bn), float(cn)
                o = "*" if op == "x" else op
                exp = {"+": av + bv, "-": av - bv, "*": av * bv,
                       "/": (av / bv if bv else None)}[o]
                if exp is None or abs(exp - cv) > 0.05:
                    issues.append(f"bad arithmetic: {a}{op}{b}={c}")
                else:
                    computed.add(cn)
            except ValueError:
                issues.append("computation operands are not numeric")

    for n in _nums(ans.answer):
        if n not in cited_nums and n not in computed:
            issues.append(f"answer asserts ungrounded number {n}")

    return (not issues, issues)


# Approach minima (CAT I/II/III decision heights, OCA/OCH, DA/DH) are among the
# highest-stakes values in the AIP and live in dense per-runway/per-category
# tables. The number-verifier cannot catch a value pulled from the RIGHT table but
# the WRONG row (e.g. RWY 04's CAT II DH attributed to RWY 22). So we NEVER
# synthesize these — we show the table verbatim and let the pilot read the exact
# row. Conservative by design for the values where misattribution is most dangerous.
_MINIMA_RE = re.compile(
    r"\bcat\s?(i{1,3}|1|2|3)\b|decision (height|altitude)|\bdh\b|\bda\b|"
    r"\boca\b|\boch\b|\bminima\b|minimum descent|\bmda\b", re.I)

# Approach PROCEDURES (holding/letdown/missed approach) are the other content we
# must never synthesize: the AD 2.22 text interleaves multiple approaches without
# reliable delimiters, so free synthesis can splice one approach's holding onto
# another's letdown (proven with DNPO) and assert values that disagree with the
# source (seen with DNBK). This is a SECOND, independent layer: even if routing
# fails to send an approach-procedure request to the chart flow, synthesis itself
# refuses and the caller defers to the plate.
_PROC_RE = re.compile(
    r"\b(holding|letdown|let-down|missed[\s-]*approach|approach procedure)\b", re.I)

# Navaid VALUE queries (a VOR/ILS/DME/LLZ/GP/NDB distance, frequency, position,
# ident). AD 2.19 stacks several navaids per aerodrome into one table block with
# misaligned fields — proven unparseable into clean per-navaid records even with
# table extraction — so synthesizing "the distance/frequency" grabs the wrong
# navaid's value (DNMM: localizer's 345 m returned for the VOR, which is 6.66 NM).
# The number-verifier can't catch this (the wrong value IS in the source). So we
# never synthesize a single navaid value — we show the block and the pilot reads
# the exact row.
_NAVAID_RE = re.compile(
    r"\b(d?vor|dme|ils|llz|localiz\w*|glide\s?path|glide\s?slope|\bgp\b|ndb|"
    r"tacan|nav\s?aids?|navaids?)\b", re.I)
# "navaid(s)" is unambiguous — it names no other AD 2.x field — so a general
# "what navaids are at Abuja" is a legitimate AD 2.19 request and fires on its
# own. The specific type words above still require a value word, because VOR/
# ILS/NDB also name approach TYPES (handled earlier by the procedure guard).
# The old pattern also had a plural bug: 'navaid\b' never matched "navaids".
_NAVAID_GENERIC_RE = re.compile(r"\bnav\s?aids?\b", re.I)
_NAVAID_VALUE_RE = re.compile(
    r"\b(distance|how far|frequenc\w+|\bfreq\b|position|coordinate\w*|located|"
    r"\bident\b|channel|elevation|bearing)\b", re.I)
# Declared distances (AD 2.13). We answer these from STRUCTURED per-runway data
# (validated at ingestion), never by synthesizing a value out of the paired
# "3610 3610" / "893.1 871.15" cells — which misattributes at asymmetric fields
# (Lagos 18L=2745 vs 18R=3900, Kano, DNFD…). The caller looks up the exact value;
# if the aerodrome has no structured row, it refuses to source (AD 2.13 verbatim).
_DECLARED_RE = re.compile(
    r"\b(tora|toda|asda|lda|declared distance|"
    r"take-?off run available|take-?off distance available|"
    r"accelerate[\s-]?stop distance|landing distance available)", re.I)

# ATS communications (AD 2.18). Tower/Ground/Approach/ATIS frequencies are stacked
# in one block (with primary+secondary per service and misaligned counts, like
# navaids), so synthesizing "the tower frequency" can return another service's
# value — a dangerous wrong frequency. We never synthesize one; we show AD 2.18
# focused and the pilot reads the exact frequency. Unambiguous service words fire
# on their own; 'approach'/'departure' (which also mean charts/procedures, handled
# before synthesis) only fire alongside an explicit frequency word.
_COMMS_SVC_RE = re.compile(
    r"\b(tower|twr|atis|clearance|delivery)\b",
    re.I)
# apron/ramp/ground are SHARED vocabulary, not comms-only: they also name
# AD 2.8 (apron surface/strength), AD 2.4 (ground handling), AD 2.9 (apron
# markings), AD 2.15 (apron floodlights) and AD 2.20 (parking area). Firing on
# them alone hijacked all five to comms — confirmed on 8 of 8 real phrasings.
# They now need an explicit frequency word, exactly like approach/departure.
_COMMS_AMBIG_RE = re.compile(
    r"\b(approach|\bapp\b|departure|\bdep\b|radar|director|\bfis\b|information|"
    r"centre|center|apron|ramp|ground|gnd)\b", re.I)
_COMMS_FREQ_RE = re.compile(r"\b(frequenc\w+|\bfreq\b|\bmhz\b|contact|call)\b", re.I)

# AD 2.12 runway physical characteristics split two ways. SYMMETRIC fields
# (length/width/dimensions, PCN/strength — one physical strip) are SAFE to
# synthesize and NOT caught here. ASYMMETRIC fields differ per runway END —
# true bearing (035° vs 215°), threshold elevation (DNAA 331 m vs 342 m),
# threshold coordinates — so synthesizing 'the' value can grab the wrong end.
# Threshold elevation must be distinguished from AERODROME elevation (AD 2.2,
# a single safe value): only a THRESHOLD/RWY-qualified elevation is caught.
_RWY_BEARING_RE = re.compile(r"\b(true|runway|rwy)?\s*bearing\b", re.I)
_THR_FIELD_RE = re.compile(
    r"\b(threshold|thr)\b.{0,20}\b(elevation|elev|coordinate\w*|position\w*|located)\b|"
    r"\b(elevation|elev|coordinate\w*|position\w*|located)\b.{0,20}\b(threshold|thr)\b|"
    r"\b(elevation|coordinate\w*|position\w*|located)\b.{0,12}\b(rwy|runway)\s*\d", re.I)

# General runway-data queries ("Abuja runway", "runways at Kano", "runway data
# for DNAA") have NO specific field asked at all — no bearing, no threshold
# field, and none of the SYMMETRIC fields (length/width/PCN/strength) that are
# deliberately left to general synthesis (test_rwy_char_guard_does_not_overfire
# already locks that in — this must not change that behaviour).
#
# Confirmed the actual gap directly: "Abuja runway" matched neither
# _RWY_BEARING_RE nor _THR_FIELD_RE, so it fell all the way through to
# generate_grounded_answer() over plain vector search — with nothing specific
# for the LLM to ground an answer on, the top-scoring chunk was a
# low-similarity (55%) match against AD 2.22's approach-minima table, shown
# verbatim as a low-confidence fallback. The runway designation the pilot
# actually wanted was never surfaced, despite AD 2.12 now being fully
# populated in aip_structured.
# Runway queries (AD 2.12) -> the exact structured record, which carries
# designation, length, width AND each end's own free text (surface, strength/
# PCN, coordinates, elevation, slope). Confirmed the original gap: "Abuja
# runway" matched no guard at all and fell through to plain vector search,
# returning a 55%-similarity match against AD 2.22's approach-minima table.
#
# This deliberately no longer excludes "symmetric" field words (length, width,
# PCN, strength, surface). That exclusion was written to preserve behaviour
# from before the structured AD 2.12 lookup existed, and it caused a real live
# failure: "What is the PCN for Lagos Runways" was pushed away from the exact
# data and into general synthesis, which abstained — while the PCN sat in the
# aip_structured record the whole time. The asymmetric guard
# (_RWY_BEARING_RE / _THR_FIELD_RE) is evaluated BEFORE this one, so
# per-end fields still route to the verbatim path unchanged.
_RWY_GENERAL_RE = re.compile(r"\b(runways?|rwys?)\b", re.I)

# AD 2.14 (approach and runway lighting) — the same misattribution shape as
# AD 2.12, but with NO safe symmetric subset to exclude. ad214_extractor.py's
# own docstring confirms every field in this table (APCH LGT type, THR LGT,
# PAPI angle and displacement, TDZ/centreline/edge/end/SWY lighting) is
# per-runway-end and can genuinely differ between a runway's two ends — there
# is no equivalent of AD 2.12's shared length/width that's safe to leave to
# general synthesis. A vague "Lagos runway lighting" query has the identical
# exposure "Abuja runway" had before that fix: nothing here routes to a
# structured lookup, so it would fall through to plain vector search and could
# surface an unrelated chunk entirely.
#
# Deliberately requires "runway"/"approach"/"threshold"/"touchdown"/
# "centreline"/"stopway" paired with "light(ing)", rather than bare
# "lighting" alone — AD 2.15 (ABN/IBN beacon, WDI, apron floodlights) and AD
# 2.9 (taxiway/markings lighting) also use the word "lighting" in a
# completely different, unrelated context; a bare match would misroute those
# queries into this guard. PAPI/VASIS are unambiguous on their own — no
# other subsection uses those terms.
_LIGHTING_RE = re.compile(
    r"\b(runway|rwy|approach|threshold|thr|touchdown|tdz|centreline|centerline|"
    r"stopway|swy)(?:\s*\d{1,2}[LRC]?)?\s*(?:zone\s*)?light(?:ing|s)?\b|"
    r"\blight(?:ing|s)?\b.{0,15}\b(runway|rwy|approach|threshold|touchdown)\b|"
    r"\b(papi|vasis)\b", re.I)

# Flight restrictions / authorisation requirements (night flying, altitude or
# speed limits, curfews, bans). These are frequently ONE item in a numbered or
# lettered list governed by an introductory clause ("unless authorised by...",
# "no pilot may operate a VFR flight:"). Free synthesis previously produced
# exactly the two-fold failure this guards against, confirmed on a real query
# ("What's the night-flying ban at Lagos?"): (1) it quoted the bare list item
# "(3) At night" stripped of its governing clause, inverting a condition that
# REQUIRES ATC authorisation into an apparent outright ban; (2) it cited the
# wrong section entirely (AD 2.20 Local Regulations) for text that was actually
# AD 2.22.5.1 (Flight Procedures — VFR within TMA), because the reply's source
# citation was reconstructed by word-overlap re-ranking rather than tied to the
# excerpt the claim actually came from. The verifier fix (per-excerpt fact
# checking, mandatory facts_used) closes the citation half generally; this
# guard removes the meaning-reversal risk for this field class specifically by
# never letting the LLM paraphrase/extract a single list item at all — the
# pilot reads the whole governing sentence themselves.
_RESTRICTION_RE = re.compile(
    r"\b(night[\s-]*flying|fly(?:ing)?\s+at\s+night|night\s+operations?|curfew|"
    r"banned|prohibited|not\s+allowed|restrict(?:ed|ion)s?|requires?\s+authoris|"
    r"authorisation|authorization)\b", re.I)


def synthesize_over_section(question: str, section_text: str,
                            section_name: str, icao: str = "") -> Tuple[bool, object, object]:
    """Run the EXISTING verified synthesis over exactly ONE subsection's text,
    fetched deterministically by name (not by similarity ranking).

    Returns (verified_ok, grounded_answer, single_result).

    This is the strongest form of the anti-misattribution guarantee in this
    codebase, because it removes retrieval error entirely rather than trying
    to detect it after the fact:

      * RETRIEVAL is exact. database.get_section_text(icao, "AD 2.17") returns
        that subsection and nothing else — possible only because
        vectorise_aip_v3.py stores one chunk per (aerodrome, subsection). No
        similarity score is involved, so there is no "the top chunk was
        actually AD 2.22's minima table" failure mode (the real, confirmed bug
        that motivated all of this).

      * VERIFICATION is unchanged, and now strictly stronger. Because the
        results list has exactly ONE element, every fact's source_excerpt can
        only be 1, and verify_grounded_answer() checks it against that single
        correct subsection. Cross-subsection bleed is not merely detected —
        it is unrepresentable.

    A False return is a genuine safety outcome, not an error: the caller shows
    the subsection's own verbatim text instead, which is still guaranteed to be
    the right subsection."""
    single = AIPResult(
        content=section_text,
        similarity=1.0,          # exact section match, not a similarity score
        chart_url=None,
        aip_section=section_name,
        reference_tag=icao or None,
    )
    ans = generate_grounded_answer(question, [single])
    if ans is None or not ans.answerable:
        return (False, ans, single)
    ok, issues = verify_grounded_answer(ans, [single])
    if not ok:
        log.warning("section synthesis verification FAILED (%s) -> verbatim: %s",
                    section_name, issues)
    return (ok, ans, single)


_AD2_SECTION_RE = re.compile(r"^AD\s*2\.\d{1,2}$", re.I)


def semantic_subsection(results: List[AIPResult]):
    """Which AD 2.x subsection did the RETRIEVER rank highest for this query?

    This is the semantic counterpart to subsection_router's keyword matching,
    and it needs no extra database call: because vectorise_aip_v3.py stores one
    chunk per (aerodrome, subsection), every result already carries the
    aip_section it came from, and its own similarity score. Grouping by section
    and taking the best score per section turns ordinary chunk-level similarity
    search into subsection-level routing.

    Why bother, when the results are already ranked? Because acting on the
    section rather than the chunks changes what synthesis SEES. Passing all
    results in lets a fact be drawn from one section while the answer cites
    another — the confirmed AD 2.20 / AD 2.22.5.1 misattribution. Routing to
    the winning section and fetching it whole means synthesis sees exactly one
    subsection, so source_excerpt can only ever be 1.

    It handles phrasings no keyword list anticipates ("what services are
    available at Enugu" -> AD 2.4) — but semantic matching can be confidently
    wrong in ways keywords cannot, so two guards apply:

      * a similarity FLOOR — a weak top match means the retriever has no real
        opinion, so don't manufacture one;
      * a MARGIN over the runner-up SECTION — if two subsections are near-tied,
        picking either is a coin flip. Declining returns None, and the caller
        falls through to ordinary synthesis over all results, which is the
        pre-existing behaviour rather than a degradation.

    Returns "AD 2.NN" or None."""
    if not results:
        return None
    best = {}
    for r in results:
        sec = (getattr(r, "aip_section", "") or "").strip()
        if not _AD2_SECTION_RE.match(sec):
            continue
        sim = float(getattr(r, "similarity", 0.0) or 0.0)
        if sim > best.get(sec, -1.0):
            best[sec] = sim
    if not best:
        return None
    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    top_sec, top_sim = ranked[0]
    if top_sim < config.SEMANTIC_SUBSECTION_MIN_SIM:
        return None
    if len(ranked) > 1 and (top_sim - ranked[1][1]) < config.SEMANTIC_SUBSECTION_MARGIN:
        return None
    return top_sec


def synthesize_decision(question: str, results: List[AIPResult]) -> Tuple[str, object]:
    """Decide how to answer a text query. Returns (status, grounded_answer):
      'grounded'   -> a VERIFIED synthesized answer (show grounded_reply)
      'not_in_aip' -> the model found no answer in the excerpts (faithful abstain)
      'fallback'   -> show verbatim chunks (synthesis off / error / FAILED verify /
                      safety carve-out for approach minima)
    Fails safe: an unverified answer never returns 'grounded'."""
    if not config.SYNTHESIS_ENABLED:
        return ("fallback", None)
    if _MINIMA_RE.search(question or ""):
        return ("fallback", None)      # never synthesize a decision height
    if _PROC_RE.search(question or ""):
        return ("approach_procedure", None)   # never synthesize procedures -> plate
    if _DECLARED_RE.search(question or ""):
        return ("declared_distance", None)     # structured lookup, else -> verbatim
    if _NAVAID_GENERIC_RE.search(question or "") or (
            _NAVAID_RE.search(question or "") and _NAVAID_VALUE_RE.search(question or "")):
        return ("navaid", None)        # never synthesize one navaid's value -> verbatim
    if (_COMMS_SVC_RE.search(question or "")
            or (_COMMS_AMBIG_RE.search(question or "")
                and _COMMS_FREQ_RE.search(question or ""))):
        return ("comms", None)         # never synthesize one service's freq -> verbatim
    if _RWY_BEARING_RE.search(question or "") or _THR_FIELD_RE.search(question or ""):
        return ("rwy_char", None)      # asymmetric AD 2.12 field -> verbatim per end
    if _LIGHTING_RE.search(question or ""):
        return ("lighting_data", None)  # AD 2.14 per-end field -> structured lookup
    if _RWY_GENERAL_RE.search(question or ""):
        return ("rwy_data", None)      # runway query -> exact AD 2.12 structured lookup
    if _RESTRICTION_RE.search(question or ""):
        return ("fallback", None)      # never synthesize a restriction/authorisation rule -> verbatim
    # Deterministic subsection routing — runs LAST, after every dedicated guard
    # above has declined. Those guards are more specific and already proven, so
    # they keep priority; this only claims queries that would otherwise fall
    # through to undirected vector search (the population that produced the
    # confirmed "Abuja runway -> AD 2.22 minima table at 55%" failure).
    # Returning the subsection id as the second element; main.py fetches that
    # exact section and synthesizes over it alone.
    sub = subsection_router.detect_subsection(question or "")
    if not sub and config.SEMANTIC_SUBSECTION_ENABLED:
        # Keyword routing declined. Try semantic subsection routing before
        # falling through to synthesis over ALL retrieved chunks — it catches
        # phrasings no keyword list anticipates, and still gives synthesis a
        # single subsection rather than a mixed context. Returns None on a
        # weak or near-tied match, in which case behaviour is unchanged.
        sub = semantic_subsection(results)
    if sub:
        return ("subsection", sub)
    ans = generate_grounded_answer(question, results)
    if ans is None:
        return ("fallback", None)
    if not ans.answerable:
        return ("not_in_aip", None)
    ok, issues = verify_grounded_answer(ans, results)
    if ok:
        return ("grounded", ans)
    log.warning("synthesis verification FAILED -> verbatim fallback: %s", issues)
    return ("fallback", None)
