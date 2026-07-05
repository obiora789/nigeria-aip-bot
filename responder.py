"""
responder.py — assembles the text a pilot sees.

Answers are EXTRACTIVE: the verbatim AIP content is shown, never paraphrased.
Every substantive reply carries a citation (AIP part/reference), the AIRAC cycle,
and the reference-aid disclaimer. Output is plain text (no Markdown) so AIP
characters — underscores, asterisks, parentheses in coordinates/frequencies —
can never break a formatter or be silently dropped.
"""
import re
from typing import List

import config
from models import Resolution, SearchOutcome

_TELEGRAM_LIMIT = 4096
_SAFE_LIMIT = 3800  # leave headroom


_ICAO_RE = re.compile(r"^DN[A-Z]{2}$")


# --- S5: runway-end disambiguation -----------------------------------------
def _rwy_num(r) -> str | None:
    m = re.match(r"\s*(\d{1,2})", str(r or "").strip())
    return f"{int(m.group(1)):02d}" if m else None


def _rwy_token(r):
    """(heading, side) e.g. '18L' -> ('18','L'), '04' -> ('04',''). Side is
    L/R/C when present. Used for strict parallel-runway matching."""
    m = re.match(r"\s*(\d{1,2})\s*([LRC])?", str(r or "").strip(), re.I)
    if not m:
        return (None, "")
    return (f"{int(m.group(1)):02d}", (m.group(2) or "").upper())


def _rwy_opposite(num: str) -> str:
    return f"{((int(num) + 18 - 1) % 36) + 1:02d}"


def runway_serves(requested, field) -> bool:
    """True if a chart's runway field covers the requested end. Side-aware: if the
    pilot names a side (18L) and the chart names the other side (18R) they do NOT
    match — 18L and 18R are separate parallel runways. If either omits the side,
    match on heading only. Handles a combined field like '18L/36R'."""
    rn, rs = _rwy_token(requested)
    if not rn or not field:
        return True
    for part in re.split(r"[\/,]", str(field)):
        fn, fs = _rwy_token(part)
        if fn != rn:
            continue
        if rs and fs and rs != fs:   # both sided, different side -> not this runway
            continue
        return True
    return False


def runway_warning(requested, field) -> str | None:
    """Warning for a chart whose runway is the OPPOSITE end of what was asked."""
    if requested and field and not runway_serves(requested, field):
        return (f"⚠ This chart is RWY {field}, not the requested RWY {requested}. "
                "Verify the correct runway end against the AIP.")
    return None


def _has_rwy(text_up: str, num: str) -> bool:
    return re.search(rf"(?:RWY|RUNWAY)\s*0*{int(num)}\b", text_up) is not None


def _runway_text_warning(requested, content: str) -> str | None:
    """Warn if displayed text names the opposite runway end but not the requested one."""
    req = _rwy_num(requested)
    if not req:
        return None
    up = content.upper()
    if _has_rwy(up, req):
        return None
    opp = _rwy_opposite(req)
    if _has_rwy(up, opp):
        return (f"⚠ The retrieved text references RWY {opp}, not the requested "
                f"RWY {req}. Verify the correct runway end against the AIP.")
    return None


def _cite(r, outcome: SearchOutcome) -> str:
    """Per-chunk citation, e.g. 'AD 2.18 / DNAA'. Falls back to the query filters.

    The vectoriser's section regex can mis-tag an aerodrome chunk with a
    cross-referenced ENR/GEN section (e.g. 'ENR 1.1' on a DNPO page). When the
    section's part prefix contradicts an aerodrome ICAO reference, trust the
    ICAO and drop the section rather than print a self-contradictory citation.
    """
    ref = (r.reference_tag or outcome.used_reference or "").strip()
    section = (r.aip_section or "").strip()
    if _ICAO_RE.match(ref) and ref != "DNKK" and section[:3].upper() in ("ENR", "GEN"):
        section = ""
    if not section:
        section = outcome.used_part or ""
    bits = [b for b in (section, ref) if b]
    return " / ".join(bits) if bits else "Nigeria AIP"


def _focus(content: str, needles: list, width: int = 360) -> str:
    """Collapse whitespace and return a focused window around the answer's values,
    so the source shows the supporting line — not a screen of flattened table."""
    text = re.sub(r"\s+", " ", (content or "").strip())
    if len(text) <= width:
        return text
    pos = [text.find(n) for n in needles]
    pos = [p for p in pos if p >= 0]
    if pos:
        lo = max(0, min(pos) - 90)
        hi = min(len(text), max(pos) + 210)
        if hi - lo > width:
            hi = lo + width
        return ("… " if lo > 0 else "") + text[lo:hi].strip() + (" …" if hi < len(text) else "")
    return text[:width].strip() + " …"


def _source_block(outcome: SearchOutcome, ans) -> str:
    """The ONE best-supporting AIP excerpt behind a synthesized answer, trimmed to
    the relevant window. Verifiability without the dump: a computed answer needs a
    single source line a pilot can check, not several full chunks (some of which
    only matched incidentally)."""
    values = [v.replace(",", "") for f in ans.facts_used
              for v in re.findall(r"\d[\d,]*(?:\.\d+)?", f.value)]

    def val_score(r):
        c = r.content.replace(",", "")
        return sum(1 for v in values if v in c)

    best = None
    if values:
        ranked = sorted(outcome.results, key=lambda r: (val_score(r), r.similarity),
                        reverse=True)
        if ranked and val_score(ranked[0]) > 0:
            best = ranked[0]
    if best is None:
        # Qualitative answer — rank by word overlap so the source truly supports it.
        target = (ans.answer + " " + " ".join(f.value for f in ans.facts_used)).lower()
        want = set(re.findall(r"[a-z]{4,}", target))
        ranked = sorted(
            outcome.results,
            key=lambda r: len(want & set(re.findall(r"[a-z]{4,}", r.content.lower()))),
            reverse=True)
        best = ranked[0] if ranked else (outcome.results[0] if outcome.results else None)
    if best is None:
        return ""
    pct = int(round(best.similarity * 100))
    return f"[AIP {_cite(best, outcome)} · {pct}% match]\n{_focus(best.content, values)}"


def grounded_reply(ans, outcome: SearchOutcome, res: Resolution) -> str:
    """Synthesized/computed answer followed by the verbatim AIP source it rests
    on. Only called after verify_grounded_answer() has passed."""
    head = ans.answer.strip()
    if (ans.computation or "").strip():
        head += f"\n(Computed: {ans.computation.strip()})"
    source = _source_block(outcome, ans)
    footer = (f"Synthesized from the AIP source below · {config.AIRAC_CYCLE}\n"
              f"{config.DISCLAIMER}")
    return f"{res.label}\n\n{head}\n\nSource (AIP, verbatim):\n{source}\n\n———\n{footer}"


def not_in_aip(res: Resolution) -> str:
    """Faithful abstention when the retrieved excerpts don't contain the answer."""
    return (f"{res.label}\n\nThat specific detail isn't stated in the AIP data I "
            f"retrieved, so I won't guess. It may not be published, or try naming the "
            f"exact field (e.g. RFFS category, declared distances, ATIS frequency).\n\n"
            f"———\nSource: Nigeria AIP · {config.AIRAC_CYCLE}\n{config.DISCLAIMER}")


def answer(outcome: SearchOutcome, res: Resolution, requested_runway=None,
           query: str = "") -> str:
    """Verbatim fallback (no verified synthesis). Show the SINGLE best chunk,
    trimmed to a focused window — not a multi-chunk dump. The chunk is the answer
    here, so we keep it verbatim, just scoped to the relevant region."""
    if not outcome.results:
        return not_found()
    # Rank by overlap with the query (+ aerodrome label) so the shown chunk is the
    # one that answers what was asked, not merely the top similarity hit.
    want = set(re.findall(r"[a-z]{4,}", f"{query} {res.label}".lower()))
    best = max(outcome.results,
               key=lambda r: (len(want & set(re.findall(r"[a-z]{4,}", r.content.lower()))),
                              r.similarity))
    pct = int(round(best.similarity * 100))
    needles = re.findall(r"\d[\d,]*(?:\.\d+)?", query) or list(want)[:6]
    snippet = _focus(best.content, needles, width=520)
    body = f"[AIP {_cite(best, outcome)} · {pct}% match]\n{snippet}"
    warn = _runway_text_warning(requested_runway, best.content)
    if warn:
        body = f"{warn}\n\n{body}"
    footer = f"Source: Nigeria AIP · {config.AIRAC_CYCLE}\n{config.DISCLAIMER}"
    return f"{res.label}\n\n{body}\n\n———\n{footer}"


def chart_intro(res, ex) -> str:
    """Caption for a chart-only reply. Chart pages are never shown as text — the
    deliverable is the plate image, so a pilot never sees flattened plate text."""
    head = f"{ex.procedure_type} " if getattr(ex, "procedure_type", None) else ""
    return (f"{head}chart for {res.label} · {config.AIRAC_CYCLE}\n"
            f"{config.DISCLAIMER}")


def chart_not_found(res, ex) -> str:
    head = f"{ex.procedure_type} " if getattr(ex, "procedure_type", None) else ""
    return (f"I don't have a {head}chart for {res.label} in the AIP "
            f"({config.AIRAC_CYCLE}). It may not be published under that name — "
            f"try a different procedure (ILS, RNAV, VOR) or a specific runway.")


def low_confidence(outcome: SearchOutcome) -> str:
    pct = int(round(outcome.max_similarity * 100))
    return (
        f"I couldn't find a confident match in the AIP (best was {pct}%). "
        "I won't guess on aeronautical data. Try rephrasing, or consult the "
        f"official AIP directly.\n\n{config.DISCLAIMER}"
    )


def not_found() -> str:
    return (
        "I couldn't find anything matching that in the published AIP. Please "
        f"consult the official AIP directly.\n\n{config.DISCLAIMER}"
    )


def ambiguous(res: Resolution) -> str:
    opts = ", ".join(res.ambiguous)
    return (
        f"{res.reason} Which one do you mean? {opts}. "
        "Reply with the ICAO code so I show the right aerodrome."
    )


def unresolved(res: Resolution) -> str:
    return (
        f"{res.reason} I only cover aerodromes published in the Nigerian AIP. "
        "If you have the ICAO code (starts with DN), send that.\n\n"
        f"{config.DISCLAIMER}"
    )


def error() -> str:
    return (
        "Something went wrong on my side and I won't risk an unverified answer. "
        "Please try again shortly, or consult the official AIP.\n\n"
        f"{config.DISCLAIMER}"
    )


def split_for_telegram(text: str) -> List[str]:
    """Split a long message on paragraph/line boundaries under Telegram's limit."""
    if len(text) <= _TELEGRAM_LIMIT:
        return [text]
    parts: List[str] = []
    remaining = text
    while len(remaining) > _SAFE_LIMIT:
        cut = remaining.rfind("\n\n", 0, _SAFE_LIMIT)
        if cut == -1:
            cut = remaining.rfind("\n", 0, _SAFE_LIMIT)
        if cut == -1:
            cut = _SAFE_LIMIT
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts
