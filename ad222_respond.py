"""
ad222_respond.py — AD 2.22 response composer.

Implements the two AD 2.22 display rules on top of the accurate, ordered
AD 2.22 capture produced by ad222_extractor.py (which is what finally makes
procedures.py usable — its docstring depended on exactly this column-aware,
in-reading-order re-ingest):

  RULE 1 — procedure / approach-plate queries:
      show the AIP's procedure text VERBATIM, then the corresponding chart.
      The verbatim text comes from procedures.extract() (scoped to the one
      unambiguous approach by runway + type, sectioned into Holding / Letdown /
      Missed Approach — it never splices approaches). The chart is the matching
      plate from the aerodrome's own AD 2.24 index (mirrors what extract_charts
      stored in aip_charts, keyed by icao / procedure_type / runway).
      If procedures.extract() can't produce clean, unambiguous sections it
      returns None; per its safety contract we then show the plate alone rather
      than a spliced or partial procedure.

  RULE 2 — non-procedure queries (General, Runway in use, Radar procedures,
      VFR minima, ...): show ONLY the accurate text answer for the asked-about
      item, with NO chart.

Everything displayed is the source's own words in the source's own order; this
module selects and routes, it never paraphrases an operational value.
"""
import re

import procedures

# ---- approach-type normalisation (aligned with procedures._type_match) -------
_TYPE_KEYS = {
    "vor": "vor", "vor/dme": "vor", "vordme": "vor",
    "ils": "ils", "ils/dme": "ils", "loc": "ils", "localiser": "ils",
    "rnav": "rnav", "gnss": "rnav", "rnp": "rnav", "lnav": "rnav", "vnav": "rnav",
    "ndb": "ndb",
}
_PLATE_RE = re.compile(r'AD\s*2\s*-\s*(DN[A-Z]{2})\s*-\s*(\d+)', re.IGNORECASE)
_RWY_RE = re.compile(r'\bRWY\s*(\d{2}[LRC]?(?:/\d{2}[LRC]?)?)', re.IGNORECASE)
_INSTR_APPROACH = re.compile(r'instrument\s+approach\s+chart', re.IGNORECASE)


def _type_key(s):
    s = (s or "").lower().strip()
    return _TYPE_KEYS.get(s, s)


# ---- chart index linkage -----------------------------------------------------
def parse_chart_index(icao, index_words):
    """Parse an AD 2.24 charts-index page into approach-chart records:
    [{name, approach_types:set, runways:set, plate_ref}]. Entries may wrap
    across visual lines, so we work off the flat text and split on plate refs
    (AD 2-{ICAO}-NN), attributing the text preceding each ref to that chart."""
    from segment_page import _line_groups
    lines = [" ".join(w[4] for w in sorted(ln, key=lambda w: w[0]))
             for ln in _line_groups(index_words)]
    flat = " ".join(lines)
    charts = []
    last = 0
    for m in _PLATE_RE.finditer(flat):
        desc = flat[last:m.start()]
        last = m.end()
        if not _INSTR_APPROACH.search(desc):
            continue
        # take just this entry's own description (after the previous chart's tail)
        desc_tail = re.split(r'(?i)instrument\s+approach\s+chart', desc)[-1]
        rwys = set(r.group(1).upper() for r in _RWY_RE.finditer(desc_tail))
        types = set()
        for tok, key in (("VOR", "vor"), ("ILS", "ils"), ("LOC", "ils"),
                         ("RNAV", "rnav"), ("GNSS", "rnav"), ("RNP", "rnav"),
                         ("LNAV", "rnav"), ("NDB", "ndb")):
            if re.search(rf'\b{tok}\b', desc_tail, re.IGNORECASE):
                types.add(key)
        charts.append({
            "name": re.sub(r'\s+', ' ',
                           ("Instrument Approach Chart" + desc_tail)).strip(),
            "approach_types": types,
            "runways": rwys,
            "plate_ref": f"AD 2-{m.group(1).upper()}-{m.group(2)}",
        })
    return charts


def find_charts(charts, rwy, req_type):
    """Charts whose runway matches `rwy` and (if given) whose approach type
    includes `req_type`. Runway match is on the numeric head with optional
    L/C/R, and a chart covering a runway pair (04/22) matches either."""
    want_rwy = re.match(r'(\d{2})([LRC]?)', rwy or "")
    key = _type_key(req_type)
    hits = []
    for c in charts:
        rwy_ok = False
        for cr in c["runways"]:
            for part in cr.split("/"):
                pm = re.match(r'(\d{2})([LRC]?)', part)
                if pm and want_rwy and pm.group(1) == want_rwy.group(1):
                    if not (want_rwy.group(2) and pm.group(2)
                            and want_rwy.group(2) != pm.group(2)):
                        rwy_ok = True
        if not rwy_ok:
            continue
        if key and c["approach_types"] and key not in c["approach_types"]:
            continue
        hits.append(c)
    # most specific first: exact single-type match (a dedicated VOR/DME plate)
    # ranks above a broader combined plate (VOR ILS DME) for a plain VOR query.
    if key:
        hits.sort(key=lambda c: (c["approach_types"] != {key}, len(c["approach_types"])))
    return hits


# ---- rule 1: procedure / approach-plate answer -------------------------------
def answer_procedure(icao, ad222_text, charts, rwy, req_type, res_label=None):
    """{'kind':'procedure','text':<verbatim or None>,'charts':[...],
        'fallback':bool}. text present => show it THEN the chart(s); text None
    => unambiguous sections unavailable, show the plate(s) alone."""
    res = procedures.extract(ad222_text, rwy, req_type)
    matched = find_charts(charts, rwy, req_type)
    if res:
        text = procedures.format_message(res_label or icao, res)
        # tighten chart choice to the matched approach type when known
        typed = find_charts(charts, res["rwy"], res["type"]) or matched
        return {"kind": "procedure", "text": text, "charts": typed,
                "fallback": False}
    return {"kind": "procedure", "text": None, "charts": matched,
            "fallback": True}


# ---- rule 2: non-procedure info answer (text only, no chart) ------------------
# Ordered by specificity; each maps a query intent to its AD 2.22 heading anchor
# and the heading that ends the block.
_INFO_BLOCKS = [
    ("runway in use", r'2\.22\.2\s+Runway\s+in\s+use', r'2\.22\.3'),
    ("general", r'2\.22\.1\s+General', r'2\.22\.2'),
    ("radar", r'2\.22\.\d+\s+Radar\s+Procedures', r'2\.22\.\d+\s+Procedures\s+for\s+VFR'),
    ("vfr minima", r'2\.22\.\d+(?:\.\d+)?\s+VFR\s+weather\s+minima', r'2\.22\.7|\Z'),
    ("vfr", r'2\.22\.\d+\s+Procedures\s+for\s+VFR\s+flights', r'2\.22\.\d+\s+Procedures\s+for\s+VFR\s+flights\s+within\s+CTR|2\.22\.\d+\s+VFR\s+weather|2\.22\.7'),
]


def _slice(text, start_re, end_re):
    m = re.search(start_re, text, re.IGNORECASE)
    if not m:
        return None
    rest = text[m.end():]
    e = re.search(end_re, rest, re.IGNORECASE)
    body = rest[:e.start()] if e else rest
    # clean page furniture / section numbers / collapse spacing
    body = re.sub(r'AD\s*2-DN[A-Z]{2}-\d+|NIGERIAN AIRSPACE MANAGEMENT AGENCY|'
                  r'AIRAC\s+AMDT[^\n]*', '', body, flags=re.IGNORECASE)
    body = re.sub(r'\b2\.22(?:\.\d+)+\b', '', body)
    body = re.sub(r'[ \t]{2,}', ' ', body)
    body = re.sub(r'\n{2,}', '\n', body)
    return body.strip(" ;:.\n\u2022")


def answer_info(icao, ad222_text, query):
    """{'kind':'info','text':<accurate block text or None>,'charts':[]}."""
    q = (query or "").lower()
    for intent, start_re, end_re in _INFO_BLOCKS:
        if all(tok in q for tok in intent.split()):
            body = _slice(ad222_text, start_re, end_re)
            if body:
                return {"kind": "info", "text": body, "charts": []}
    return {"kind": "info", "text": None, "charts": []}


# ---- dispatcher --------------------------------------------------------------
_APPROACH_HINT = re.compile(
    r'\b(approach|plate|chart|procedure|holding|letdown|missed|ils|vor|rnav|'
    r'rnp|gnss|ndb|loc|localiser|star|sid|iac)\b', re.IGNORECASE)


def answer(icao, query, ad222_text, charts, rwy=None, req_type=None,
           res_label=None):
    """Compose an AD 2.22 answer under the two display rules.

    rwy / req_type: parsed upstream (resolver) when known. If a query is
    approach-related but carries no runway, procedures.extract() returns None by
    contract and we show the plate(s) — never a runway-ambiguous procedure."""
    is_proc = bool(rwy) or bool(_APPROACH_HINT.search(query or ""))
    if is_proc:
        return answer_procedure(icao, ad222_text, charts, rwy or "",
                                req_type or "", res_label=res_label)
    return answer_info(icao, ad222_text, query)
