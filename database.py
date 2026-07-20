"""
database.py — Supabase access: text search (with fallback + correct gating) and
deterministic chart retrieval.

Fixes folded in vs. the original:
  * abstention is gated on MAX similarity across results, not response.data[0]
    (which was the highest-TIER row, not the most similar one);
  * the hard aip_part / reference_tag filters are neutralised by trying a small,
    safe set of (part, reference) combinations — reference for an aerodrome is
    NEVER relaxed off its ICAO, so we can't drift to another airport;
  * results come back structured, so the responder can cite and format properly.
"""
import logging
import os
import re
from typing import List, Optional, Tuple

from supabase import Client, create_client

import config
from models import AIPResult, ChartRef, Resolution, SearchOutcome

log = logging.getLogger("vannie.db")

supabase: Client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def _rpc_match(embedding: list, part: str, reference: str,
               procedure_type: str, runway: str) -> List[AIPResult]:
    try:
        resp = supabase.rpc("match_aip_text_advanced", {
            "query_embedding": embedding,
            "match_filter_part": part,
            "match_filter_reference": reference,
            "match_procedure_type": procedure_type or "",
            "match_runway": runway or "",
            "match_limit": config.MATCH_LIMIT,
        }).execute()
    except Exception:  # noqa: BLE001
        log.exception("match_aip_text_advanced failed (part=%s ref=%s)", part, reference)
        return []
    return [
        AIPResult(content=r.get("content", ""),
                  similarity=float(r.get("similarity", 0.0)),
                  chart_url=r.get("chart_url"),
                  aip_section=r.get("aip_section"),
                  reference_tag=r.get("reference_tag"))
        for r in (resp.data or [])
    ]


def _attempts(res: Resolution) -> List[Tuple[str, str]]:
    """Ordered (part, reference) combinations to try. Reference for an aerodrome
    stays pinned to its ICAO, so retrieval can never return a different airport."""
    out: List[Tuple[str, str]] = []
    if res.is_national:
        parts = [res.part] + [p for p in config.AIP_PARTS if p != res.part]
        # Try the resolution's preferred tag first (e.g. AIRSPACE for airspace
        # queries), then the rest, so we don't return GEN content for an ENR ask.
        tags = ([res.reference] if res.reference else []) + \
               [t for t in config.NATIONAL_REFERENCE_TAGS if t != res.reference]
        for ref in tags:
            for part in parts:
                out.append((part, ref))
    else:
        # ICAO query: reference fixed to the ICAO; relax part only (AD first).
        parts = ["AD"] + [p for p in config.AIP_PARTS if p != "AD"]
        for part in parts:
            out.append((part, res.reference or res.icao))
    # de-dup preserving order
    seen = set()
    uniq = []
    for a in out:
        if a not in seen:
            seen.add(a); uniq.append(a)
    return uniq


def search_aip(embedding: list, res: Resolution,
               procedure_type: str = "", runway: str = "") -> SearchOutcome:
    best: Optional[SearchOutcome] = None
    for part, reference in _attempts(res):
        rows = _rpc_match(embedding, part, reference, procedure_type, runway)
        if not rows:
            continue
        mx = max(r.similarity for r in rows)
        rows.sort(key=lambda r: r.similarity, reverse=True)
        candidate = SearchOutcome(results=rows, max_similarity=mx,
                                  used_part=part, used_reference=reference,
                                  abstained=False)
        if best is None or mx > best.max_similarity:
            best = candidate
        if mx >= config.SIMILARITY_THRESHOLD:
            return candidate  # confident hit — stop early

    if best and best.max_similarity >= config.SIMILARITY_THRESHOLD:
        return best
    if best:  # found content but below threshold
        return SearchOutcome(results=best.results, max_similarity=best.max_similarity,
                             used_part=best.used_part, used_reference=best.used_reference,
                             abstained=True, reason="low_confidence")
    return SearchOutcome(abstained=True, reason="no_match")


def get_charts(icao: str, procedure_type: str = "", runway: str = "") -> List[ChartRef]:
    if not icao:
        return []
    try:
        resp = supabase.rpc("get_aip_charts", {
            "p_icao": icao,
            "p_procedure": procedure_type or "",
            "p_runway": runway or "",
        }).execute()
    except Exception:  # noqa: BLE001
        log.exception("get_aip_charts failed (icao=%s)", icao)
        return []

    charts: List[ChartRef] = []
    for row in (resp.data or []):
        url = row.get("chart_url")
        if not url:
            continue
        ext = os.path.splitext(url.split("?")[0])[1].lower()
        charts.append(ChartRef(
            url=url,
            procedure_type=row.get("procedure_type"),
            runway=row.get("runway"),
            icao_code=row.get("icao_code"),
            is_pdf=(ext not in _IMAGE_EXTS),  # treat anything non-image as a document
        ))
    return charts


# The AIP names some plates differently from how pilots ask. A SID is the
# "Area Chart - Departure and Transit Routes"; a STAR is the "Arrival" one; the
# stored "Parking / Docking Chart" has spaces a raw "parking/docking" won't match.
# Map the pilot's term -> catalogue keyword(s). SPECIFIC types win; the generic
# 'approach/plate' fallback applies only when no specific type is named (so
# "RNAV approach" -> RNAV, not every approach chart).
_CHART_SPECIFIC = {
    "sid": ["departure"], "departure": ["departure"],
    "star": ["arrival", "star"], "arrival": ["arrival", "star"],
    "parking": ["parking"], "docking": ["parking"], "stand": ["parking"],
    "apron": ["parking"], "gate": ["parking"],
    "rnav": ["rnav"], "gnss": ["rnav"], "rnp": ["rnav"], "gps": ["rnav"],
    "ils": ["ils"], "vor": ["vor"], "ndb": ["ndb"],
    "obstacle": ["obstacle"], "terrain": ["terrain"], "heliport": ["heliport"],
    "aerodrome chart": ["aerodrome chart"], "airport chart": ["aerodrome chart"],
    "area": ["area"],
}
_CHART_GENERIC = {"approach": ["approach"], "plate": ["approach"]}


def _chart_targets(term: str) -> List[str]:
    r = (term or "").lower()
    spec: List[str] = []
    for key, kws in _CHART_SPECIFIC.items():
        if key in r:
            spec.extend(kws)
    if spec:
        return list(dict.fromkeys(spec))
    gen: List[str] = []
    for key, kws in _CHART_GENERIC.items():
        if key in r:
            gen.extend(kws)
    return list(dict.fromkeys(gen))


def chart_matches(term: str, stored_procedure: str) -> bool:
    """True if a stored chart's procedure_type satisfies the requested term.
    No recognised term -> match all (a generic 'charts for X' request)."""
    targets = _chart_targets(term)
    if not targets:
        return True
    return any(t in (stored_procedure or "").lower() for t in targets)


def get_section_text(icao: str, section_prefix: str = "AD 2.22") -> str:
    """Reconstruct an aerodrome's full section text (e.g. AD 2.22) by fetching all
    its chunks in order and concatenating. Used by the approach-procedure
    sectioniser, which needs the whole ordered block, not a single retrieved piece."""
    try:
        resp = (supabase.table("aip_knowledge_base")
                .select("content, source_page, source_chunk")
                .eq("reference_tag", icao)
                .like("aip_section", f"{section_prefix}%").execute())
    except Exception:  # noqa: BLE001
        log.exception("get_section_text failed (icao=%s)", icao)
        return ""
    rows = sorted((resp.data or []),
                  key=lambda r: (r.get("source_page") or 0, r.get("source_chunk") or 0))
    return "\n".join(r.get("content", "") for r in rows)


def get_subsection_text(icao: str, section: str) -> str:
    """EXACTLY one subsection's text (all its chunks, in order).

    Distinct from get_section_text(), which matches on a LIKE prefix. That is
    right for its original caller (AD 2.22, where no other section starts with
    that string) but WRONG for the subsection router: "AD 2.2" as a prefix also
    matches AD 2.20, 2.21, 2.22, 2.23 and 2.24, so a query about magnetic
    variation would pull six subsections — including the ~55k-character
    AD 2.22 — into a single synthesis context, reintroducing exactly the
    cross-subsection misattribution that routing to one exact section exists
    to make impossible.

    Equality matching is also correct for AD 2.22 itself: vectorise_aip_v3.py
    splits it into several chunks for embedding length, but they all carry
    aip_section = "AD 2.22" and differ only by source_chunk."""
    try:
        resp = (supabase.table("aip_knowledge_base")
                .select("content, source_page, source_chunk")
                .eq("reference_tag", icao)
                .eq("aip_section", section).execute())
    except Exception:  # noqa: BLE001
        log.exception("get_subsection_text failed (icao=%s section=%s)", icao, section)
        return ""
    rows = sorted((resp.data or []),
                  key=lambda r: (r.get("source_page") or 0, r.get("source_chunk") or 0))
    return "\n".join(r.get("content", "") for r in rows)


def get_declared_distances(icao: str) -> list:
    """Structured per-runway declared distances (AD 2.13), read from
    aip_structured — NOT the older aip_declared_distances table.

    Migrated from the old table after a real, confirmed gap: aip_structured
    (Layer 2's ad213_extractor.py output) had 74 rows for subsection 2.13
    against the old table's 69 — meaning several aerodromes' worth of more
    rigorously validated data (including a real thousands-joining bug fixed
    on DNSO, and a real column-assignment bug fixed on DNKT) was sitting
    unused while the live bot kept serving the older, less-validated table.

    Field names are remapped (tora_m -> tora, etc.) to match the existing
    contract declared_distance_reply() and its callers already use, so this
    is a pure data-source swap — no other file needed to change shape.

    A per-field None is now a genuine, expected case (the DNKT fix: a runway
    can have 3 of 4 metrics published with the 4th genuinely absent from the
    source) — unlike the old table, which only ever stored an aerodrome if
    ALL FOUR metrics parsed cleanly. Callers must handle None per field, not
    assume all four are always present.

    Empty list means this aerodrome has no aip_structured row for 2.13 (rare
    — validated at ingestion): the caller falls back to the refuse-to-source
    guard, same as before."""
    try:
        resp = (supabase.table("aip_structured")
                .select("record")
                .eq("icao_code", icao)
                .eq("subsection", "2.13")
                .order("record_index").execute())
    except Exception:  # noqa: BLE001
        log.exception("get_declared_distances failed (icao=%s)", icao)
        return []
    out = []
    for row in (resp.data or []):
        r = row["record"]
        out.append({
            "runway": r.get("runway"),
            "tora": r.get("tora_m"),
            "toda": r.get("toda_m"),
            "asda": r.get("asda_m"),
            "lda": r.get("lda_m"),
            "remarks": r.get("remarks"),
        })
    return out


def get_runway_physical_data(icao: str) -> list:
    """Structured per-runway physical characteristics (AD 2.12) — designation,
    length, width, and per-runway-end free text (surface/strength/coordinates/
    elevation/slope/remarks, kept STRICTLY SEPARATE per end, never merged).

    This is the exact subsection the project's original misattribution incident
    happened on: a query for "Abuja runway" once returned RWY 04's elevation
    spliced together with RWY 22's slope data, both pulled from AD 2.12's dense
    table. Attribution is now resolved ONCE at ingestion (Layer 2's per-entity
    tracking in ad212_extractor.py — text can only ever attach to the runway end
    whose own row most recently preceded it), validated 36/36, so this
    query-time lookup is a plain key fetch, not a similarity search that could
    rank the wrong table.

    Empty list means this aerodrome has no aip_structured row for 2.12 (rare) —
    the caller falls back to the existing verbatim/refuse-to-source path, the
    same way get_declared_distances' callers do."""
    try:
        resp = (supabase.table("aip_structured")
                .select("record")
                .eq("icao_code", icao)
                .eq("subsection", "2.12")
                .order("record_index").execute())
        return [row["record"] for row in (resp.data or [])]
    except Exception:  # noqa: BLE001
        log.exception("get_runway_physical_data failed (icao=%s)", icao)
        return []


def get_lighting_data(icao: str) -> list:
    """Structured per-runway-end approach/runway lighting (AD 2.14) —
    designation and per-end free text (APCH LGT, THR LGT, PAPI angle and
    displacement, TDZ/centreline/edge/end/SWY lighting, remarks).

    This subsection is ENTIRELY per-runway-end — unlike AD 2.12, there is no
    safe symmetric subset (no shared length/width equivalent): every field
    here (PAPI angle, lighting type, displacement) can genuinely differ
    between the two ends of one physical runway, and a vague query has the
    exact same misattribution exposure the original AD 2.12 incident did.
    Attribution is resolved ONCE at ingestion via the same "currently active
    end" tracking ad212_extractor.py uses (ad214_extractor.py reuses it
    directly), so this query-time lookup is a plain key fetch, never a
    similarity search that could rank the wrong runway end.

    A record with designation=None and end_detail={'general_notes': ...} is
    the confirmed genuine case where no runway-end lighting rows exist at
    all (lighting simply isn't published for this aerodrome) — the caller
    displays that text as-is rather than treating it as an error.

    Empty list means this aerodrome has no aip_structured row for 2.14 at
    all (should not happen — validated at ingestion): the caller falls back
    to the existing verbatim path."""
    try:
        resp = (supabase.table("aip_structured")
                .select("record")
                .eq("icao_code", icao)
                .eq("subsection", "2.14")
                .order("record_index").execute())
        return [row["record"] for row in (resp.data or [])]
    except Exception:  # noqa: BLE001
        log.exception("get_lighting_data failed (icao=%s)", icao)
        return []


def get_aerodrome_data(icao: str) -> str:
    """AD 2.2 aerodrome geographic/admin data, fetched BY SECTION. AD 2.2 is a
    prefix of AD 2.20-2.24, so a plain LIKE would pull in local regs / noise /
    procedures / the chart index — we keep only 'AD 2.2' (and its 'AD 2.2.x'
    sub-items) and drop 'AD 2.2<digit>'. Used to answer paired fields like
    reference temperature that the general vector search under-retrieves."""
    try:
        resp = (supabase.table("aip_knowledge_base")
                .select("content, source_page, source_chunk, aip_section")
                .eq("reference_tag", icao)
                .like("aip_section", "AD 2.2%").execute())
    except Exception:  # noqa: BLE001
        log.exception("get_aerodrome_data failed (icao=%s)", icao)
        return ""
    rows = [r for r in (resp.data or [])
            if not re.match(r"AD 2\.2\d", r.get("aip_section") or "")]
    rows.sort(key=lambda r: (r.get("source_page") or 0, r.get("source_chunk") or 0))
    return "\n".join(r["content"] for r in rows)


def get_charts_smart(icao: str, term: str = "", runway: str = "") -> List[ChartRef]:
    """Fetch ALL of an aerodrome's charts DIRECTLY from the table (the RPC drops
    NULL-runway charts even on empty params), then filter by synonym-aware type.
    Runway is a PREFERENCE, not a hard filter: return exact-end matches if any
    exist, else return all type matches so send_charts can add the S5 warning —
    a request for RWY 04 should still show the RWY 22 plate, flagged, not nothing."""
    from responder import runway_serves  # lazy import to avoid a cycle
    try:
        resp = supabase.table("aip_charts").select(
            "chart_url, procedure_type, runway, icao_code").eq("icao_code", icao).execute()
    except Exception:  # noqa: BLE001
        log.exception("aip_charts direct fetch failed (icao=%s)", icao)
        return []
    charts: List[ChartRef] = []
    for row in (resp.data or []):
        url = row.get("chart_url")
        if not url:
            continue
        ext = os.path.splitext(url.split("?")[0])[1].lower()
        charts.append(ChartRef(
            url=url, procedure_type=row.get("procedure_type"),
            runway=row.get("runway"), icao_code=row.get("icao_code"),
            is_pdf=(ext not in _IMAGE_EXTS)))
    charts = [c for c in charts if chart_matches(term, c.procedure_type)]
    if runway:
        exact = [c for c in charts if runway_serves(runway, c.runway)]
        if exact:
            return exact
    return charts
