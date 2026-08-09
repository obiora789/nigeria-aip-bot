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
    the relevant window.

    Cites deterministically from the fact's own declared source_excerpt — the
    same index verify_grounded_answer() checked the fact's value against — so
    the excerpt shown is provably the one the claim came from, not a separate
    after-the-fact guess. The old version re-ranked ALL retrieved chunks by
    number/word overlap with the answer AFTER generation, independent of what
    the model actually read; that heuristic could and did pick the wrong
    section (confirmed: a DNMM VFR-restrictions answer whose real source was
    AD 2.22.5.1 was cited as AD 2.20 because an AD 2.20 chunk scored higher on
    shared vocabulary). The word/number-overlap ranking below is now only a
    defensive fallback for the (should-be-impossible, since verify_grounded_answer
    requires it) case where no fact carries a valid source_excerpt.
    """
    results = outcome.results
    for f in ans.facts_used:
        idx = getattr(f, "source_excerpt", None)
        if idx and 1 <= idx <= len(results):
            best = results[idx - 1]
            values = [v.replace(",", "") for ff in ans.facts_used
                      for v in re.findall(r"\d[\d,]*(?:\.\d+)?", ff.value)]
            pct = int(round(best.similarity * 100))
            return f"[AIP {_cite(best, outcome)} · {pct}% match]\n{_focus(best.content, values)}"

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


_DD_METRICS = ("tora", "toda", "asda", "lda")


def _dd_rwy_match(spec: str, stored: str) -> bool:
    sm = re.match(r"\s*(\d{1,2})\s*([LRC])?", str(spec or ""), re.I)
    tm = re.match(r"\s*(\d{1,2})\s*([LRC])?", str(stored or ""), re.I)
    if not sm or not tm:
        return False
    if f"{int(sm.group(1)):02d}" != f"{int(tm.group(1)):02d}":
        return False
    ss, ts = (sm.group(2) or "").upper(), (tm.group(2) or "").upper()
    return not (ss and ts and ss != ts)   # '18' matches 18L & 18R; '18L' only 18L


def declared_distance_reply(res: Resolution, recs: list, requested_runway=None,
                            query: str = "") -> str:
    """Exact declared-distance answer from STRUCTURED data (validated at ingestion,
    so never misattributed). Answers the specific runway+metric if asked, else
    lists every runway's TORA/TODA/ASDA/LDA.

    A metric can genuinely be None (a real per-field gap in the source, e.g.
    DNKT publishes TODA/ASDA/LDA but not TORA) — shown as 'not published',
    never a bare 'None', so a null-over-guess gap reads as an honest fact
    about the source, not a formatting bug."""
    def _fmt(v):
        return f"{v} m" if v is not None else "not published"

    asked = [m for m in _DD_METRICS if re.search(rf"\b{m}\b", query or "", re.I)]
    metrics = asked or list(_DD_METRICS)
    footer = (f"\n\n———\nSource: Nigeria AIP · AD 2.13 · {config.AIRAC_CYCLE}\n"
              f"{config.DISCLAIMER}")

    if requested_runway:
        hits = [r for r in recs if _dd_rwy_match(requested_runway, r["runway"])]
        if len(hits) == 1:
            r = hits[0]
            vals = "\n".join(f"{m.upper()}: {_fmt(r[m])}" for m in metrics)
            return f"{res.label} — RWY {r['runway']}\n\n{vals}{footer}"
        if len(hits) > 1:            # e.g. '18' matched 18L and 18R -> show both
            recs = hits

    lines = [f"{res.label} — declared distances (AD 2.13)", ""]
    for r in recs:
        lines.append("RWY {}: TORA {} · TODA {} · ASDA {} · LDA {}".format(
            r["runway"], _fmt(r["tora"]), _fmt(r["toda"]),
            _fmt(r["asda"]), _fmt(r["lda"])))
    return "\n".join(lines) + footer


def _section_source_reply(res: Resolution, text: str, needles, section: str) -> str:
    """Read-the-source reply from a named AD section fetched BY NAME (not by
    similarity, which can surface the wrong section). Focused around the query's
    terms. Never asserts a single value — the pilot reads the exact figure. Used
    for dense multi-entity tables (navaids AD 2.19, comms AD 2.18) where per-entity
    values can't be split safely."""
    snippet = _focus(text, needles, width=800)
    return (f"{res.label}\n\n[AIP {section} / {res.icao}]\n{snippet}\n\n———\n"
            f"Source: Nigeria AIP · {section} · {config.AIRAC_CYCLE}\n{config.DISCLAIMER}")


def navaid_reply(res: Resolution, nav_text: str, query: str = "") -> str:
    """Focused AD 2.19 navaid reply — the pilot reads the exact figure for the
    navaid they asked about (multiple navaids share one block)."""
    needles = re.findall(r"\d{2}[LRC]?|d?vor|dme|ils|llz|localiz\w*|ndb|gp",
                         query or "", re.I)
    return _section_source_reply(res, nav_text, needles, "AD 2.19")


def comms_reply(res: Resolution, comms_text: str, query: str = "") -> str:
    """Focused AD 2.18 communications reply — the pilot reads the exact frequency
    for the service they asked about (Tower/Ground/Approach/ATIS share one block,
    with primary+secondary frequencies stacked, so a single value can't be split
    out safely)."""
    needles = re.findall(
        r"tower|twr|ground|gnd|approach|\bapp\b|departure|\bdep\b|atis|clearance|"
        r"delivery|apron|radar|director|\d{3}\.\d", query or "", re.I)
    return _section_source_reply(res, comms_text, needles, "AD 2.18")


def rwy_char_reply(res: Resolution, rc_text: str, query: str = "") -> str:
    """Focused AD 2.12 reply for an ASYMMETRIC field (bearing / threshold elevation
    / threshold coordinates), which differ per runway end — the pilot reads the
    value for the specific end rather than a synthesized one that could be the
    reciprocal end's."""
    needles = re.findall(r"\d{2}[LRC]?|bearing|elevation|elev|threshold|thr|"
                         r"coordinate|position", query or "", re.I)
    return _section_source_reply(res, rc_text, needles, "AD 2.12")


def runway_data_reply(res: Resolution, records: list, requested_runway=None,
                      query: str = "") -> str:
    """Exact runway-physical-characteristics answer from STRUCTURED data (AD
    2.12, resolved and validated at ingestion via per-entity tracking — never
    misattributed). This is the exact subsection the project's original
    misattribution incident happened on (one runway end's elevation spliced
    with another's slope data); the structured record keeps each end's data
    strictly separate, and this reply preserves that separation visually.

    Mirrors declared_distance_reply's established shape: a specific runway
    filters to just that physical runway if named, else every runway is listed.
    """
    footer = (f"\n\n———\nSource: Nigeria AIP · AD 2.12 · {config.AIRAC_CYCLE}\n"
              f"{config.DISCLAIMER}")

    recs = records
    if requested_runway:
        hits = [r for r in records if runway_serves(requested_runway, r.get("designation"))]
        if hits:
            recs = hits

    lines = [f"{res.label} — runway data (AD 2.12)", ""]
    for r in recs:
        desig = r.get("designation", "?")
        length = r.get("length_m")
        width = r.get("width_m")
        dims = f"{length} x {width} m" if length and width else "dimensions not available"
        lines.append(f"RWY {desig} — {dims}")

        end_detail = r.get("end_detail") or {}
        for end, detail in end_detail.items():
            if detail:
                lines.append(f"  [{end}] {detail}")
        lines.append("")

    body = "\n".join(lines).rstrip()
    return body + footer


def lighting_data_reply(res: Resolution, records: list, requested_runway=None,
                        query: str = "") -> str:
    """Exact approach/runway lighting answer from STRUCTURED data (AD 2.14,
    resolved via the same per-runway-end tracking as AD 2.12 — never
    misattributed). This subsection has NO safe symmetric subset (unlike AD
    2.12's shared length/width) — every field (PAPI angle, lighting type)
    can genuinely differ between a runway's two ends, so this is the
    structured-lookup path for ANY lighting query, not just asymmetric ones.

    A record with designation=None and end_detail={'general_notes': ...} is
    the confirmed genuine case where no lighting is published for any
    runway at this aerodrome at all — shown as-is, not treated as an error.
    """
    footer = (f"\n\n———\nSource: Nigeria AIP · AD 2.14 · {config.AIRAC_CYCLE}\n"
              f"{config.DISCLAIMER}")

    recs = records
    if requested_runway:
        hits = [r for r in records
                if r.get("designation") and runway_serves(requested_runway, r["designation"])]
        if hits:
            recs = hits

    lines = [f"{res.label} — approach and runway lighting (AD 2.14)", ""]
    for r in recs:
        desig = r.get("designation")
        end_detail = r.get("end_detail") or {}
        if desig is None:
            # general_notes case: no runway-end lighting published at all.
            note = end_detail.get("general_notes", "").strip()
            lines.append(note or "No lighting information published.")
            lines.append("")
            continue
        lines.append(f"RWY {desig}")
        for end, detail in end_detail.items():
            if detail:
                lines.append(f"  [{end}] {detail}")
        lines.append("")

    body = "\n".join(lines).rstrip()
    return body + footer


# A stored AD 2.x subsection is often a FLATTENED FIELD TABLE:
#   "DNMM AD 2.17 ATS airspace: Designation and lateral limits=CTR. A circle
#    radius 20NM...; Vertical limit=CTR: 1 500FT GND TMA: FL 145...;
#    Transition altitude=3 500 ft/1 067 m AMSL; Remarks=Transition level: FL 50"
#
# Shown raw that is one unreadable run-on line: confirmed live, a pilot asking
# "what is Lagos transition altitude" got all seven AD 2.17 fields in a single
# paragraph with the answer buried sixth. Worse, it invites MISREADING -- the
# CTR and TMA values sit adjacent with nothing separating them, which is the
# same juxtaposition hazard that made the AD 2.22 focus window unsafe.
#
# Splitting on the table's own "label=value;" structure and giving each field
# its own line is pure re-formatting: no value is altered, merged, rounded or
# synthesised, and each value stays welded to its own label. It is strictly
# safer than the run-on form as well as far more readable.
# A LABEL is a short run with no sentence punctuation, ending in '='. Digits
# and lowercase are allowed because different subsections label differently --
# AD 2.17 uses "Transition altitude", AD 2.13 uses "RWY 18L", AD 2.3 uses
# "customs and immigration". An earlier, narrower pattern required an initial
# capital and no digits, so it parsed AD 2.17 and silently fell back on both of
# the others.
#
# It stays conservative in the ways that matter: no '.' ',' ';' or ':' inside a
# label, a length bound, and _parse_field_table requires at least TWO pairs --
# so prose containing an incidental "x = y" is not mistaken for a table. A
# value may itself contain ';' or '=' (AD 2.4's "Jet A-1; AVGAS 100LL",
# AD 2.2's "ARP = midpoint of RWY 04/22"), because the split only fires before
# something that looks like a label.
_LABEL = r"[A-Za-z0-9][A-Za-z0-9 /()'\-]{2,60}"
_FIELD_SPLIT_RE = re.compile(rf";\s*(?={_LABEL}=)")
_FIELD_KV_RE = re.compile(rf"^\s*({_LABEL})=(.*)$", re.S)


# A chunk can carry an ENTITY prefix followed by several key=value pairs:
#   "36R TORA=2745 TODA=2745 ASDA=2805 LDA=2745"
# (confirmed: DNMM's real stored AD 2.13). Treating that as one label/value
# gave label "36R TORA" with value "2745 TODA=2745 ASDA=2805 LDA=2745" --
# three declared distances stranded inside a fourth's value, and a label that
# claims to be TORA while the text beside it also states TODA, ASDA and LDA.
# A pilot reading that could take any of the four numbers as the TORA.
# Expanding it into one field per pair keeps every value welded to its own
# label, which is the invariant the whole reply format exists to preserve.
_INNER_KV_RE = re.compile(r"([A-Za-z][A-Za-z0-9/()'\-]{1,20})=\s*([^=]*?)"
                          r"(?=\s+[A-Za-z][A-Za-z0-9/()'\-]{1,20}=|$)")


def _expand_entity_chunk(label: str, value: str):
    """[(label, value)] — expanded if `value` holds further key=value pairs."""
    inner = _INNER_KV_RE.findall(value or "")
    if len(inner) < 1 or "=" not in (value or ""):
        return [(label, value)]
    parts = label.rsplit(" ", 1)
    entity, first_key = (parts[0], parts[1]) if len(parts) == 2 else ("", label)
    out = [(f"{entity} {first_key}".strip(), inner[0][1].strip() if inner else value)]
    # The first pair's value is the text before the first inner key, which the
    # split above already captured as `value`'s head.
    head = value.split(inner[0][0] + "=", 1)[0].strip() if inner else value
    out = [(f"{entity} {first_key}".strip(), head or value)]
    for k, v in inner:
        out.append((f"{entity} {k}".strip(), v.strip().rstrip(";")))
    return out


def _parse_field_table(text: str):
    """[(label, value)] if `text` is a flattened field table, else None."""
    body = text or ""
    head = ""
    m = re.match(r"^([^:]{0,80}?:)\s*(.*)$", body, re.S)
    if m and "=" in m.group(2):
        head, body = m.group(1).strip(), m.group(2)
    pairs = []
    for chunk in _FIELD_SPLIT_RE.split(body):
        kv = _FIELD_KV_RE.match(chunk.strip())
        if kv:
            label = re.sub(r"\s+", " ", kv.group(1)).strip()
            value = re.sub(r"\s+", " ", kv.group(2)).strip().rstrip(";")
            if label and value:
                pairs.extend(_expand_entity_chunk(label, value))
    return (head, pairs) if len(pairs) >= 2 else None


def _rank_fields(pairs, query: str, place: str = ""):
    """Order fields by overlap with the pilot's words. Ranking only changes the
    ORDER things are shown in -- every field is still shown, so a bad ranking
    costs readability, never an omitted or wrong value."""
    def _stem(w):
        """Crude singularisation so "limits" matches the AIP's "limit".
        Bounded on purpose -- it normalises plurals, it is not a vocabulary."""
        if len(w) > 4 and w.endswith("ies"):
            return w[:-3] + "y"
        if len(w) > 3 and w.endswith("es") and not w.endswith("ses"):
            return w[:-2]
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            return w[:-1]
        return w

    def _words(t):
        return {_stem(w) for w in re.findall(r"[a-z]{3,}", (t or "").lower())}

    # Drop the aerodrome's own name from the ranking terms. It appears in the
    # published DATA as well as the question -- Lagos's AD 2.17 call-sign field
    # reads "Lagos Tower/EN, Lagos Approach/EN" -- so leaving it in made every
    # question mentioning Lagos rank the call-sign field top, whatever it was
    # actually about. Confirmed: five unrelated queries all led with call signs.
    # This is not a stop-word list; `place` is the aerodrome this reply is
    # already resolved to, so it stays correct for any aerodrome and any
    # question.
    _skip = {_stem(w) for w in re.findall(r"[a-z]{3,}", (place or "").lower())}
    qwords = [_stem(w) for w in re.findall(r"[a-z]{3,}", (query or "").lower())
              if _stem(w) not in _skip]
    # Runway/entity designators ("18R", "04", "18L") are alphanumeric and short,
    # so the word pattern above drops them entirely. That made "TORA for RWY
    # 18R" tie between the RWY 18L and RWY 18R fields and lead with 18L --
    # promoting one runway's declared distances under a question about the
    # other, which is the misattribution this project exists to prevent.
    designators = {d.upper() for d in re.findall(r"\b\d{2}[LRC]?\b", (query or "").upper())}
    terms = set(qwords)
    # Consecutive word pairs from the query. A PHRASE hit outranks any number
    # of single-word hits, because single words are ambiguous in exactly the
    # place it matters: "transition level" and "transition altitude" share the
    # word "transition", and the LEVEL is published under the generic label
    # "Remarks". Word-only scoring therefore led a "transition level" question
    # with the transition ALTITUDE -- a different value, and one a pilot could
    # act on. Phrase matching pins it to the field whose text actually says
    # "transition level".
    bigrams = {f"{a} {b}" for a, b in zip(qwords, qwords[1:])}

    def score(lbl_val):
        lbl, val = lbl_val
        hay = " ".join(_stem(w) for w in
                       re.findall(r"[a-z]{3,}", f"{lbl} {val}".lower()))
        phrase = sum(8 for bg in bigrams if bg in hay)
        lbl_desig = {d.upper() for d in re.findall(r"\b\d{2}[LRC]?\b", lbl.upper())}
        # A designator match is decisive: it names WHICH entity was asked about.
        desig = 20 * len(designators & lbl_desig)
        # ...and a designator MISMATCH is disqualifying. If the pilot said 18R,
        # the 18L field must never lead, however many other words it shares.
        if designators and lbl_desig and not (designators & lbl_desig):
            return -1
        lw, vw = _words(lbl), _words(val)
        return desig + phrase + (len(terms & lw) * 3) + len(terms & vw)

    ranked = sorted(pairs, key=score, reverse=True)
    if not ranked:
        return ranked, 0
    top = score(ranked[0])
    # A TIE means the question does not distinguish these fields. Promoting
    # either would assert a choice the pilot never made, so show no lead and
    # let them read the labelled list. Same principle as asking rather than
    # guessing -- applied to ordering.
    if len(ranked) > 1 and score(ranked[1]) == top:
        return ranked, 0
    return ranked, top


def _render_field_table(head, pairs, query: str, place: str = "") -> str:
    # DUPLICATE LABELS mean the section publishes several entries the stored
    # text does not distinguish. Confirmed in DNAA's real AD 2.19: "LLZ" twice
    # (109.3 and 111.9 MHz) and "GP ILS/DME" twice (332.0 and 331.1) -- one per
    # runway, with nothing saying which. Promoting either under "what is the
    # LLZ frequency at Abuja" would assert a runway the source never states,
    # which is the documented AD 2.19 misattribution hazard. So: never lead,
    # show every entry, and say plainly why.
    counts = {}
    for lbl, _ in pairs:
        counts[lbl] = counts.get(lbl, 0) + 1
    dupes = sorted(l for l, n in counts.items() if n > 1)

    ranked, top = _rank_fields(pairs, query, place)
    if dupes and any(l in dupes for l, _ in ranked[:1]):
        top = 0
    lines = []
    if head:
        lines.append(head.rstrip(":"))
        lines.append("")
    if top > 0:
        lbl, val = ranked[0]
        lines.append(f"▸ {lbl}")
        lines.append(f"   {val}")
        rest = [p for p in pairs if p != ranked[0]]
        if rest:
            lines.append("")
            lines.append("Also published in this section:")
            for lbl, val in rest:
                lines.append(f"• {lbl}: {val}")
    else:
        for lbl, val in pairs:
            lines.append(f"• {lbl}: {val}")
    if dupes:
        lines.append("")
        lines.append(f"Note: this aerodrome publishes more than one entry for "
                     f"{', '.join(dupes)} in one AIP table. Check the plate or "
                     f"the official AIP for which applies to your runway.")
    return "\n".join(lines)


def subsection_reply(res: Resolution, section: str, text: str,
                     query: str = "") -> str:
    """Verbatim reply from ONE deterministically-fetched AD 2.x subsection.

    Used when synthesis over that subsection didn't verify — the pilot still
    gets the RIGHT subsection's own words, focused on the query terms, rather
    than a vector-search guess that might be a different subsection entirely.
    That guarantee is what makes this a safe fallback rather than a degraded
    one: retrieval was exact even though synthesis declined."""
    table = _parse_field_table(text)
    if table:
        # A field table is rendered whole and structured. _focus() is for prose:
        # applied to a table it cuts mid-field and strands a value under the
        # wrong label.
        body = _render_field_table(table[0], table[1], query, res.label or "")
    else:
        needles = (re.findall(r"\d[\d,]*(?:\.\d+)?", query or "")
                   + re.findall(r"[a-z]{4,}", (query or "").lower())[:8])
        body = _focus(text, needles, width=700) if needles else text[:1400]
    footer = (f"\n\n———\nSource: Nigeria AIP · {section} · {config.AIRAC_CYCLE}\n"
              f"{config.DISCLAIMER}")
    head = f"{res.label} — {section}\n\n"
    return (head + body)[:_SAFE_LIMIT - len(footer)] + footer


def info_block_reply(res: Resolution, section: str, body: str) -> str:
    """Format clarify.info_block_answer()'s deterministic slice. Deliberately
    does NOT re-run the text through _focus() — the slice is already bounded
    to exactly one AD 2.22 heading (General / Runway in use / Radar
    Procedures / VFR minima / VFR flights), and re-trimming an already-precise
    answer risks cutting it short for no safety benefit."""
    footer = (f"\n\n———\nSource: Nigeria AIP · {section} · {config.AIRAC_CYCLE}\n"
              f"{config.DISCLAIMER}")
    return f"{res.label} — {section}\n\n{body}"[:_SAFE_LIMIT - len(footer)] + footer


def facts_reply(res: Resolution, facts: list, query: str = "") -> str:
    """Answer from field-level facts (aip_facts), shown VERBATIM.

    No synthesis is involved: each line is a stored value with its own entity
    and label, so there is no hallucination surface at all — the bot can only
    show what was extracted and validated at ingestion.

    Facts are grouped by entity (runway end, ATS service, navaid) so a
    multi-runway answer reads correctly and two runways' values can never be
    merged into one line — the entity is part of the retrieved unit, not
    something reconstructed here."""
    if not facts:
        return not_in_aip(res)

    sections = sorted({f.get("subsection") for f in facts if f.get("subsection")})
    # The part prefix follows the SCOPE, not a constant. "AD" is only correct
    # for aerodromes; a danger area lives in ENR 5.1 and citing it as "AD 5.1"
    # names a section that does not exist — a pilot checking the source against
    # the real AIP would not find it, which defeats the point of citing at all.
    part = "AD" if (getattr(res, "scope_kind", "AD") or "AD") == "AD" else "ENR"
    cite = f"{part} {sections[0]}" if len(sections) == 1 else f"{part} 2"

    grouped = {}
    for f in facts:
        grouped.setdefault((f.get("entity") or "").strip(), []).append(f)

    lines = [f"{res.label} — {cite}", ""]
    for entity, items in grouped.items():
        if entity:
            lines.append(f"{entity}:")
            for f in items:
                lines.append(f"  {f['label']}: {f['fact_value']}")
        else:
            for f in items:
                lines.append(f"{f['label']}: {f['fact_value']}")
        lines.append("")

    body = "\n".join(lines).rstrip()
    footer = (f"\n\n———\nSource: Nigeria AIP · {cite} · {config.AIRAC_CYCLE}\n"
              f"{config.DISCLAIMER}")
    return body[:_SAFE_LIMIT - len(footer)] + footer


def not_found() -> str:
    return (
        "I couldn't find anything matching that in the published AIP. Please "
        f"consult the official AIP directly.\n\n{config.DISCLAIMER}"
    )


def ambiguous(res: Resolution) -> str:
    opts = ", ".join(res.ambiguous)
    # A SCOPE ambiguity is not an aerodrome ambiguity, and asking for an ICAO
    # code would be useless advice: a navaid has no ICAO code. "ABC" means both
    # Abuja and the ABC VOR/DME, so the question has to name both readings.
    if getattr(res, "ambiguous_kind", "aerodrome") == "scope":
        return (
            f"{res.reason} Which did you mean — {opts}? "
            "Reply with the aerodrome name or ICAO code for the aerodrome, "
            "or say 'navaid' for the navigation aid."
        )
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
