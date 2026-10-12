"""
charges.py — published GEN 4 charges, shown VERBATIM with the amendment they
are valid for and who to ask for current rates.

WHY THIS IS ITS OWN PATH
------------------------
Charges were refused outright as "MONEY", although GEN 4 publishes them. The
refusal was wrong, but answering them through the ordinary path would be worse:
vector search plus synthesis over GEN 4.1, which prints international and
domestic schedules, and day and night landing rates, side by side. That is the
juxtaposition hazard AD 2.12 and AD 2.17 already guard against — a synthesised
"the landing fee" could return the domestic night rate for an international
day landing with every digit genuinely present in the source.

So: the extraction LLM decides WHETHER this is a charges question and WHICH
subsection (schemas.gen4_charge — no keyword matching on the pilot's words),
main.py fetches that subsection BY NAME, and this module shows the AIP's own
lines. Nothing is computed, converted or summarised.

WHAT GEN 4 ACTUALLY SAYS (read from Complete_AIP2026.pdf, pages 115-117)
-----------------------------------------------------------------------
  GEN 4.1  Aerodrome charges — landing, parking, avio bridge, passenger
           service, cargo/port, fuel throughput, CUTE/check-in counters.
           "common to all airports managed by the Federal Airports Authority
           of Nigeria (FAAN) except where it is stated to the contrary."
           Page date 30 OCT 25, AIRAC AMDT 02/2025.
  GEN 4.2  Air navigation services charges — en-route, overflight, terminal
           navigation (TNC), outside-hours ATS. Levied by NAMA.
           Page date 7 DEC 17, AIRAC AMDT 02/2017.

Two consequences shape the reply:
  * Landing fees are ONE national schedule per kg of AUW, not a figure per
    aerodrome. A question naming Kano gets the national schedule and is told
    so, rather than an answer that implies a Kano-specific rate.
  * The two subsections were last amended eight years apart and are levied by
    different bodies. The caveat is therefore per subsection: one generic
    "check with NAMA" line would send landing-fee questions to an agency the
    AIP itself says does not set them.

AIRAC DISCIPLINE
----------------
_GEN4 below is static, like resolver.AERODROMES, and must be checked at every
AIRAC cycle against the GEN 4.x page footers. As a safety net the reply prefers
an "AIRAC AMDT nn/yyyy" stamp found in the stored text itself (the page footer
survives ingestion), and logs a warning when it disagrees with this table, so a
re-ingested AIP is not silently mislabelled with last cycle's amendment.

PARSING THE DOCUMENT, NOT THE QUESTION
--------------------------------------
The only patterns here match the AIP's own numbering ("4.1.3"), its page
chrome and its amendment stamps — document structure, the same thing every
extractor in this project parses. Choosing which numbered block to show uses
word overlap with the block headings, which is presentation only: a poor match
shows MORE of the subsection, never a different one, and the other headings
are always listed.
"""
import logging
import re

import config

log = logging.getLogger("vannie.charges")

_GEN4 = {
    "GEN 4.1": {
        "kind": "aerodrome",
        "title": "Aerodrome charges",
        "amdt": "AIRAC AMDT 02/2025",
        "effective": "30 OCT 25",
        # Who to send a pilot to for CURRENT rates. Per GEN 4.1.1.1 these are
        # FAAN's charges; NAMA (AIS) is named as well because NAMA publishes
        # the AIP and its amendments (GEN 0.1). To name NAMA only, replace
        # this string — nothing else depends on its wording.
        "ask": ("GEN 4.1 states these charges are set by the Federal Airports "
                "Authority of Nigeria (FAAN). For current rates contact FAAN, "
                "or NAMA AIS (aishq@nama.gov.ng, per GEN 0.1) for the latest "
                "AIP amendment."),
        "scope_note": ("GEN 4.1 publishes one schedule, \"common to all "
                       "airports managed by the Federal Airports Authority of "
                       "Nigeria (FAAN) except where it is stated to the "
                       "contrary\" — not a separate schedule per aerodrome."),
        # The AIP's own third-level headings, copied from pages 115-116. Used
        # to match a question to a block and to list what else the subsection
        # holds. Static structure, checked at each AIRAC like the stamp above;
        # a number missing from here still works, it just matches on body text.
        "headings": {
            "4.1.1": "General",
            "4.1.2": "Methods of payments",
            "4.1.3": "Landing charges",
            "4.1.4": "Parking charges",
            "4.1.5": "Avio bridge charges",
            "4.1.6": "Passenger service charges",
            "4.1.7": "Port charge (cargo)",
            "4.1.8": "Fuel charge",
            "4.1.9": "Common User Terminal Equipment (CUTE) and Checking-in-Counters",
        },
    },
    "GEN 4.2": {
        "kind": "air_navigation",
        "title": "Air navigation services charges",
        "amdt": "AIRAC AMDT 02/2017",
        "effective": "7 DEC 17",
        "ask": ("Contact NAMA for any updated charges (NAMA AIS: "
                "aishq@nama.gov.ng, per GEN 0.1)."),
        "scope_note": ("GEN 4.2 publishes these nationally, per flight — not "
                       "per aerodrome."),
        # Page 117. 4.2.2 carries the en-route, overflight, terminal navigation
        # (TNC) and outside-hours charges as 4.2.2.1-4.2.2.4.
        "headings": {
            "4.2.1": "General",
            "4.2.2": ("Air Navigation Service Charges — en-route, overflight, "
                      "terminal navigation (TNC), outside normal hours"),
            "4.2.3": "Exemptions",
            "4.2.4": "Payment",
        },
    },
}
_KIND_TO_SECTION = {meta["kind"]: sec for sec, meta in _GEN4.items()}


def section_for(kind):
    """'aerodrome' -> 'GEN 4.1', 'air_navigation' -> 'GEN 4.2', else None."""
    return _KIND_TO_SECTION.get(kind) if kind else None


# --- reassembling the stored text -------------------------------------------
# get_subsection_text() joins a section's chunks with "\n". Each page's FIRST
# chunk carries the vectoriser's "[Nigerian AIP 2026 | ...]" prefix, and
# consecutive chunks of one page overlap by ~300 characters (CHUNK_OVERLAP), so
# a plain join repeats text — measured on a faithful rebuild of GEN 4.1, the
# landing-charge exemptions paragraph appeared twice. Repeated lines in a fee
# schedule are not harmless: they read as two separate rates.

_PREFIX_RE = re.compile(r"\[Nigerian AIP[^\]]*\]\s*")
_MIN_OVERLAP = 40      # shorter matches could be coincidence, not overlap
_MAX_OVERLAP = 450     # CHUNK_OVERLAP is 300; allow for boundary adjustment


def _drop_overlap(prev: str, nxt: str) -> str:
    """nxt with any leading copy of prev's tail removed."""
    hi = min(len(prev), len(nxt), _MAX_OVERLAP)
    for k in range(hi, _MIN_OVERLAP - 1, -1):
        if prev.endswith(nxt[:k]):
            return nxt[k:].lstrip()
    return nxt


def _reassemble(raw: str) -> str:
    chunks = [_PREFIX_RE.sub("", c).strip() for c in (raw or "").split("\n")]
    chunks = [c for c in chunks if c]
    out = []
    for c in chunks:
        out.append(_drop_overlap(out[-1], c) if out else c)
    return " ".join(p for p in out if p)


# --- page chrome --------------------------------------------------------------
_AMDT_RE = re.compile(r"AIRAC\s+AMDT\s+(\d{2}/\d{4})")
_CHROME_RES = [
    re.compile(r"\bGEN\s+4\.[12]-\d+\b"),                          # page id
    re.compile(r"\bNIGERIA\s+AIP\b"),
    re.compile(r"\bNIGERIAN\s+AIRSPACE\s+MANAGEMENT\s+AGENCY\b"),
    re.compile(r"\b\d{1,2}\s+(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+\d{2}\b"),
    _AMDT_RE,
    re.compile(r"\bGEN\s+4\.\s+CHARGES\s+FOR\s+AERODROMES\s+AND\s+AIR\s+NAVIGATION\s+SERVICES\b"),
    re.compile(r"\bGEN\s+4\.1\s+AERODROME\s+CHARGES\b"),
    re.compile(r"\bGEN\s+4\.2\s+AIR\s+NAVIGATION\s+SERVICES\s+CHARGES\b"),
    re.compile(r"\bINTENTIONALLY\s+BLANK\b"),
]


def _stored_amdt(text: str):
    """The single 'nn/yyyy' amendment stamp printed in this subsection, or None
    if there is none or more than one (then the static table decides)."""
    found = set(_AMDT_RE.findall(text or ""))
    return f"AIRAC AMDT {found.pop()}" if len(found) == 1 else None


def _strip_chrome(text: str) -> str:
    for rx in _CHROME_RES:
        text = rx.sub(" ", text)
    return re.sub(r"\s{2,}", " ", text).strip()


# --- the AIP's own numbered blocks -------------------------------------------

def _blocks(text: str, section: str):
    """[(number, body)] split at this subsection's third-level headings
    ('4.1.3 Landing charges'), each body running to the next heading. Deeper
    numbers (4.1.3.2) stay inside their parent block, so a block always holds
    a heading together with every rate and exemption beneath it."""
    n = section.split()[-1]                                   # "4.1"
    head_re = re.compile(rf"(?<![\d.]){re.escape(n)}\.\d{{1,2}}(?![\d.])")
    marks = list(head_re.finditer(text))
    best = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.start():end].strip()
        # keep the fuller copy if reassembly left a heading twice
        if len(body) > len(best.get(m.group(0), "")):
            best[m.group(0)] = body
    return sorted(best.items(),
                  key=lambda kv: tuple(int(p) for p in kv[0].split(".")))


_SKIP = {"the", "what", "is", "are", "for", "at", "of", "and", "how", "much",
         "does", "do", "per", "fee", "charge", "rate", "cost", "price",
         "tariff", "airport", "aerodrome", "nigeria", "nigerian", "current",
         "pay", "paid", "payable", "there", "any", "in", "on", "to", "a", "an"}


def _words(s: str):
    out = set()
    for w in re.findall(r"[a-z]{3,}", (s or "").lower()):
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        if w not in _SKIP:
            out.add(w)
    return out


def _heading(section: str, num: str, body: str) -> str:
    """The AIP heading for a block number; the body's first words if the
    number is not in the table (a future amendment added a heading)."""
    known = _GEN4[section].get("headings", {}).get(num)
    if known:
        return known
    words = []
    for w in body.split()[1:]:
        if w[:1].isdigit() or len(words) == 4:
            break
        words.append(w)
    return " ".join(words)


def _pick(blocks, query: str, section: str = ""):
    """Blocks whose heading (weighted) or body share words with the question.
    [] means no confident match — the caller then shows the whole section."""
    q = _words(query)
    if not q or not blocks:
        return []
    scored = []
    for num, body in blocks:
        title = _heading(section, num, body) if section else ""
        scored.append((3 * len(q & _words(title)) + len(q & _words(body)), num, body))
    top = max(s for s, _, _ in scored)
    if top == 0:
        return []
    winners = [(num, body) for s, num, body in scored if s == top]
    return winners if len(winners) <= 3 else []


# CONFIRMED SOURCE-RENDERING CORRECTION — GEN 4.2.2.1 weight factor.
# On the page (GEN 4.2-1, PDF page 117) the formula is drawn as
#     P = √( Maximum take-off mass (Tons) / 50 )
# with the square root and the fraction bar as GRAPHICS. Text extraction keeps
# only the words, so the stored text reads "P = Maximum take-off mass (Tons) 50"
# — which, read literally, is not the formula, and an en-route charge worked out
# from it would be wrong by a large factor. Verified by rendering the page.
#
# Same policy as the README's confirmed AIP corrections (ENR 3.3 GANDA): fixed
# in code, matched EXACTLY so it cannot fire on anything else, and always
# flagged in the reply so the pilot knows the line was reconstructed.
_WEIGHT_FACTOR_RE = re.compile(
    r"P\s*=\s*Maximum\s+take-off\s+mass\s+\(Tons\)\s+50\b")
_WEIGHT_FACTOR_FIX = "P = √( Maximum take-off mass (Tons) ÷ 50 )"
_WEIGHT_FACTOR_NOTE = (
    "[Formula above restored from the AIP page (GEN 4.2-1): the square root "
    "and fraction bar are drawn as graphics and are lost in the stored text.]")


def _format(body: str) -> str:
    """Line breaks before the AIP's own sub-numbers and lettered items — pure
    re-layout, no word added, removed or reordered — plus the one flagged
    formula restoration above."""
    restored = False
    if _WEIGHT_FACTOR_RE.search(body):
        body = _WEIGHT_FACTOR_RE.sub(_WEIGHT_FACTOR_FIX, body)
        restored = True
    body = re.sub(r"\s+(?=(?<![\d.])4\.[12]\.\d{1,2}\.\d)", "\n", body)
    body = re.sub(r"\s+(?=[a-e]\)\s)", "\n", body)
    body = body.strip()
    return f"{body}\n{_WEIGHT_FACTOR_NOTE}" if restored else body


def caveat(section: str, stored_text: str = "") -> str:
    """The currency + authority line for one GEN 4 subsection."""
    meta = _GEN4[section]
    amdt = _stored_amdt(stored_text) or meta["amdt"]
    if amdt != meta["amdt"]:
        log.warning("%s: stored text says %s but charges._GEN4 says %s — update "
                    "the table for this AIRAC cycle", section, amdt, meta["amdt"])
        when = f"{amdt}"                       # the table's date is stale too
    else:
        when = f"{amdt}, effective {meta['effective']}"
    return (f"These charges are as published in {section} ({when}) and are "
            f"valid for that amendment only — they may have changed since. "
            f"{meta['ask']}")


def charges_reply(section: str, raw_text: str, query: str = "",
                  aerodrome_named: bool = False) -> str:
    """The pilot-facing reply for a GEN 4 charges question."""
    meta = _GEN4[section]
    footer = (f"\n\n———\nSource: Nigeria AIP · {section} · {config.AIRAC_CYCLE}\n"
              f"{config.DISCLAIMER}")

    if not (raw_text or "").strip():
        # Null over guess: never quote a rate from anywhere but the stored AIP.
        return (f"{meta['title']} ({section})\n\nI couldn't retrieve {section} "
                f"from the stored AIP just now, so I won't quote a rate. "
                f"{caveat(section)}{footer}")

    text = _strip_chrome(_reassemble(raw_text))
    blocks = _blocks(text, section)
    chosen = _pick(blocks, query, section)

    lines = [f"{meta['title']} — {section}", ""]
    if aerodrome_named:
        lines += [meta["scope_note"], ""]
    if chosen:
        for _, body in chosen:
            lines += [_format(body), ""]
        picked = {c[0] for c in chosen}
        others = [f"{n} {_heading(section, n, b)}"
                  for n, b in blocks if n not in picked]
        if others:
            lines.append(f"Also in {section}: " + "; ".join(others) + ".")
            lines.append("")
    else:
        for _, body in blocks or [("", text)]:
            lines += [_format(body), ""]

    lines.append(caveat(section, raw_text))
    return "\n".join(lines).rstrip() + footer
