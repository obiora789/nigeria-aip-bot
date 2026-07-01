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
    return {m.replace(",", "") for m in _NUM.findall(s or "")}


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
        resp = _client.beta.chat.completions.parse(
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


def verify_grounded_answer(ans: GroundedAnswer, context: str) -> Tuple[bool, List[str]]:
    """Deterministic anti-hallucination check. PASSES only if:
      - every number in each facts_used value appears in the source context;
      - every arithmetic step is valid AND its operands are in the source;
      - every number asserted in the answer text is either in the source or is a
        result of the shown arithmetic.
    Any violation -> (False, issues); the caller must then NOT show this answer."""
    ctx = _nums(context)
    issues: List[str] = []

    for f in ans.facts_used:
        for n in _nums(f.value):
            if n not in ctx:
                issues.append(f"ungrounded fact value {n} ({f.what})")

    computed = set()
    comp = (ans.computation or "").strip()
    if comp:
        found = _ARITH.findall(comp)
        if not found:
            issues.append("computation present but unparseable")
        for a, op, b, c in found:
            an, bn, cn = a.replace(",", ""), b.replace(",", ""), c.replace(",", "")
            for x in (an, bn):
                if x not in ctx:
                    issues.append(f"computation operand {x} not found in source")
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
        if n not in ctx and n not in computed:
            issues.append(f"answer asserts ungrounded number {n}")

    return (not issues, issues)


def synthesize_decision(question: str, results: List[AIPResult]) -> Tuple[str, object]:
    """Decide how to answer a text query. Returns (status, grounded_answer):
      'grounded'   -> a VERIFIED synthesized answer (show grounded_reply)
      'not_in_aip' -> the model found no answer in the excerpts (faithful abstain)
      'fallback'   -> show verbatim chunks (synthesis off / error / FAILED verify)
    Fails safe: an unverified answer never returns 'grounded'."""
    if not config.SYNTHESIS_ENABLED:
        return ("fallback", None)
    ans = generate_grounded_answer(question, results)
    if ans is None:
        return ("fallback", None)
    if not ans.answerable:
        return ("not_in_aip", None)
    context = "\n".join(r.content for r in results)
    ok, issues = verify_grounded_answer(ans, context)
    if ok:
        return ("grounded", ans)
    log.warning("synthesis verification FAILED -> verbatim fallback: %s", issues)
    return ("fallback", None)
