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
import time
from typing import List, Optional, Tuple

import httpx
from supabase import Client, create_client

import config
from models import AIPResult, ChartRef, Resolution, SearchOutcome

log = logging.getLogger("vannie.db")

supabase: Client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


# ── a failed read is NEVER "not published" (2026-11-09) ───────────────────────
# Every read below used to catch any error and return "" or [] — exactly what
# an unpublished section looks like. Measured 2026-11-07/09: Supabase dropped
# 7 of ~1,500 reads in one quick check ("Server disconnected", an idle HTTP/2
# connection closed by the server). Each one silently became "not in the AIP":
# Dutse's AD 2.15 read failed and the check concluded Dutse publishes no other
# lighting. In the live bot the same failure can say a published section is
# missing — a wrong answer, against null-over-guess.
#
# Now: a dropped connection is retried (a fresh connection almost always
# works); anything that still fails raises DataUnavailable. main.process turns
# that into "I couldn't reach the AIP database just now", never into a
# "not published" answer. Callers that deliberately degrade (resolver name
# lookups, the double-meaning checks) already catch exceptions and keep doing so.
class DataUnavailable(RuntimeError):
    """The AIP database could not be read, after retrying. Not the same as the
    AIP not publishing something, and must never be answered as if it were."""


READ_ATTEMPTS = int(os.getenv("DB_READ_ATTEMPTS", "3"))
_RETRY_PAUSES = (0.25, 0.75, 1.5)
# httpx.TransportError covers the transient family: RemoteProtocolError
# ("Server disconnected"), ConnectError, ReadError, WriteError and timeouts.
_TRANSIENT = (httpx.TransportError,)


def _execute(build, what: str):
    """Run one Supabase read: build() returns the query or RPC builder.
    Retries a dropped connection; raises DataUnavailable when it cannot read."""
    last = None
    for attempt in range(max(1, READ_ATTEMPTS)):
        try:
            return build().execute()
        except _TRANSIENT as e:
            last = e
            if attempt + 1 < READ_ATTEMPTS:
                log.warning("%s: %s — retrying (%d/%d)", what, type(e).__name__,
                            attempt + 1, READ_ATTEMPTS - 1)
                time.sleep(_RETRY_PAUSES[min(attempt, len(_RETRY_PAUSES) - 1)])
                continue
        except Exception as e:  # noqa: BLE001 — a bad query is not a transient
            log.exception("%s failed", what)
            raise DataUnavailable(f"{what}: {type(e).__name__}: {e}") from e
    log.error("%s failed after %d attempts: %s", what, READ_ATTEMPTS, last)
    raise DataUnavailable(f"{what}: {type(last).__name__}: {last}") from last


def _rpc_match(embedding: list, part: str, reference: str,
               procedure_type: str, runway: str) -> List[AIPResult]:
    resp = _execute(lambda: supabase.rpc("match_aip_text_advanced", {
        "query_embedding": embedding,
        "match_filter_part": part,
        "match_filter_reference": reference,
        "match_procedure_type": procedure_type or "",
        "match_runway": runway or "",
        "match_limit": config.MATCH_LIMIT,
    }), f"match_aip_text_advanced (part={part} ref={reference})")
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
    resp = _execute(lambda: supabase.rpc("get_aip_charts", {
        "p_icao": icao,
        "p_procedure": procedure_type or "",
        "p_runway": runway or "",
    }), f"get_aip_charts (icao={icao})")

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
    # 'SID Chart' since 2026-11-21 (16_fix_chart_labels.sql); before that SID
    # pages were mislabelled and "sid" fell back to the Area departure chart.
    "sid": ["sid chart"], "departure": ["sid chart", "departure"],
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
    resp = _execute(lambda: (supabase.table("aip_knowledge_base")
                             .select("content, source_page, source_chunk")
                             .eq("reference_tag", icao)
                             .like("aip_section", f"{section_prefix}%")),
                    f"get_section_text (icao={icao} section={section_prefix})")
    rows = sorted((resp.data or []),
                  key=lambda r: (r.get("source_page") or 0, r.get("source_chunk") or 0))
    return "\n".join(r.get("content", "") for r in rows)


def search_facts(embedding: list, icao: str, subsection: str = "",
                 limit: int = 8) -> list:
    """Field-level semantic retrieval for AD 2.x (see sql/11_aip_facts.sql).

    Returns atomic, self-describing facts ordered by similarity:
        {subsection, entity, label, fact_value, fact_text, similarity}

    This is the counterpart to search_aip(), at a granularity that actually
    works. search_aip() ranks (aerodrome, subsection) chunks — DNMM's AD 2.22
    is 79,871 characters behind ONE vector, so nothing scores decisively and
    the top hit is frequently the wrong subsection entirely ("lateral limit
    for lagos ctr" -> ENR 3.1 @ 59%). A fact row embeds a single field as a
    sentence close to how a pilot would ask for it, so the match is direct.

    Always scoped to one ICAO at the database boundary, so a fact can never
    surface for a different aerodrome. `subsection` narrows further when the
    classifier already identified one.

    Empty list means no facts indexed for this aerodrome — the caller keeps
    its existing behaviour rather than treating it as 'not published'. A
    failed read raises DataUnavailable instead of returning []."""
    if not icao:
        return []
    resp = _execute(lambda: supabase.rpc("match_aip_facts", {
        "query_embedding": embedding,
        "p_icao": icao,
        "p_subsection": subsection or None,
        "match_limit": limit,
    }), f"match_aip_facts (icao={icao} sub={subsection})")
    return resp.data or []


def search_facts_scoped(embedding: list, scope_kind: str, scope_id: str,
                        subsection: str = "", limit: int = 8) -> list:
    """Field-level retrieval for ANY AIP entity, not just aerodromes.

    The counterpart to search_facts(), which is hard-wired to an ICAO code and
    therefore cannot reach a waypoint, an airway or a danger area. A pilot
    asking "Where is TEMSA?" was told it is not in the Nigerian AIP; it is, on
    seven pages, because nothing in the retrieval path could accept a scope
    that is not an aerodrome.

    The safety property is unchanged and still enforced at the DATABASE
    boundary, not here: match_aip_facts_scoped filters on (scope_kind,
    scope_id), so a fact can never be returned for a different entity. Only
    the definition of "entity" widens — from 40 aerodromes to every published
    entity in the document.

    scope_kind is one of AD | ENR_AREA | ENR_POINT | ENR_ROUTE | ENR_AIRSPACE.
    """
    if not embedding or not scope_kind or not scope_id:
        return []
    res = _execute(lambda: supabase.rpc("match_aip_facts_scoped", {
        "query_embedding": embedding,
        "p_scope_kind": scope_kind,
        "p_scope_id": scope_id,
        "p_subsection": subsection or None,
        "match_limit": limit,
    }), f"match_aip_facts_scoped ({scope_kind} {scope_id})")
    return res.data or []


def list_scope_ids(scope_kind: str) -> list:
    """Every scope_id published for one scope kind.

    The authoritative set for name recognition, read from the index rather than
    described by a pattern. A 2-4 letter navaid ident cannot be matched by
    shape — [A-Z]{2,4} matches half the words in an English sentence, which is
    how a stop-list of "words that are not designators" came to exist. A token
    either IS a published ident or it is not, and only the index knows."""
    if not scope_kind:
        return []
    rows, page, size = [], 0, 1000
    while True:
        lo = page * size
        res = _execute(lambda: (supabase.table("aip_facts").select("scope_id")
                                .eq("scope_kind", scope_kind)
                                .range(lo, lo + size - 1)),
                       f"list_scope_ids ({scope_kind} page {page})")
        batch = res.data or []
        rows.extend(r["scope_id"] for r in batch if r.get("scope_id"))
        if len(batch) < size:
            return sorted(set(rows))
        page += 1


def find_aip_scope(name: str) -> list:
    """Deterministic name -> scope lookup. "TEMSA" -> ENR_POINT/TEMSA.

    Exact, no embedding, no ranking — the same shape as resolver.AERODROMES,
    which is the one part of this system that has never misrouted. A name
    either is a published entity or it is not, and answering "I don't have
    that" for something the AIP publishes is the failure this closes.

    A failed read raises DataUnavailable (after retrying a dropped
    connection). Every caller (resolver, agent) catches it and degrades to
    the existing behaviour, so a lookup outage never takes down the request."""
    if not (name or "").strip():
        return []
    res = _execute(lambda: supabase.rpc("find_aip_scope", {"p_name": name.strip()}),
                   f"find_aip_scope ({name})")
    return res.data or []


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
    resp = _execute(lambda: (supabase.table("aip_knowledge_base")
                             .select("content, source_page, source_chunk")
                             .eq("reference_tag", icao)
                             .eq("aip_section", section)),
                    f"get_subsection_text (icao={icao} section={section})")
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
    resp = _execute(lambda: (supabase.table("aip_structured")
                             .select("record")
                             .eq("icao_code", icao)
                             .eq("subsection", "2.13")
                             .order("record_index")),
                    f"get_declared_distances (icao={icao})")
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
    resp = _execute(lambda: (supabase.table("aip_structured")
                             .select("record")
                             .eq("icao_code", icao)
                             .eq("subsection", "2.12")
                             .order("record_index")),
                    f"get_runway_physical_data (icao={icao})")
    return [row["record"] for row in (resp.data or [])]


def get_structured(icao: str, subsection: str) -> list:
    """All aip_structured records for one aerodrome and subsection ('2.2',
    '2.18', ...), in ingestion order — a plain key read, no similarity. Used by
    the aerodrome summary card, which must show the same fields from the same
    place for every aerodrome. Empty list = nothing stored; a failed read
    raises DataUnavailable."""
    resp = _execute(lambda: (supabase.table("aip_structured")
                             .select("record")
                             .eq("icao_code", icao)
                             .eq("subsection", subsection)
                             .order("record_index")),
                    f"get_structured (icao={icao} sub={subsection})")
    return [row["record"] for row in (resp.data or [])]


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
    resp = _execute(lambda: (supabase.table("aip_structured")
                             .select("record")
                             .eq("icao_code", icao)
                             .eq("subsection", "2.14")
                             .order("record_index")),
                    f"get_lighting_data (icao={icao})")
    return [row["record"] for row in (resp.data or [])]


def get_aerodrome_data(icao: str) -> str:
    """AD 2.2 aerodrome geographic/admin data, fetched BY SECTION. AD 2.2 is a
    prefix of AD 2.20-2.24, so a plain LIKE would pull in local regs / noise /
    procedures / the chart index — we keep only 'AD 2.2' (and its 'AD 2.2.x'
    sub-items) and drop 'AD 2.2<digit>'. Used to answer paired fields like
    reference temperature that the general vector search under-retrieves."""
    resp = _execute(lambda: (supabase.table("aip_knowledge_base")
                             .select("content, source_page, source_chunk, aip_section")
                             .eq("reference_tag", icao)
                             .like("aip_section", "AD 2.2%")),
                    f"get_aerodrome_data (icao={icao})")
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
    resp = _execute(lambda: supabase.table("aip_charts").select(
        "chart_url, procedure_type, runway, icao_code").eq("icao_code", icao),
        f"aip_charts direct fetch (icao={icao})")
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
