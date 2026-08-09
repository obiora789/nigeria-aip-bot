#!/usr/bin/env python3
"""
enr41_extractor.py — ENR 4.1 en-route radio navigation aids.

THE IDENT IS THE ENTITY, NOT THE STATION
----------------------------------------
One station block can publish SEVERAL navaids:

    BENIN / VOR/DME / L / BEN / BE / 116.5 MHz / CH 112 X / 272KHz / ...

That is a VOR/DME (ident BEN, 116.5 MHz) AND a locator (ident BE, 272 kHz) at
one place. Keying on "BENIN" would collapse them, and a pilot asking for the
Benin locator frequency could be handed the VOR's — the same misattribution
AD 2.19's guard exists to prevent, arriving through the entity boundary rather
than the value. So BEN and BE are separate scopes.

THREE PUBLISHED LAYOUTS
-----------------------
All three are in the document; all three were found by reading the pages, and
handling only the first produced 16 dropped stations.

  1. INLINE — type, ident and frequency on one line:
         BIDA / "VOR/DME BDA 112.7 MHz" / "CH 74 X" / "H24 090607.7N ..."

  2. STACKED — parallel columns, one value per line:
         KANO / DVOR/DME / NDB / KAN / AO / 112.5 MHz / CH 72 X / 340kHz / ...

  3. SPLIT — the type stands alone, ident and frequency share the next line:
         ANAMBRA / DVOR/DME / "ANU 113.8MHz" / "CH 85 X" / ...
     Four stations publish this way (AKURE, ANAMBRA, ASABA, BAYELSA) and
     produced NO record at all until it was handled.

HOW A STATION IS TOLD FROM AN IDENT
-----------------------------------
By POSITION, not by length. An earlier version used "4+ characters is a
station", which is arbitrary and failed badly: BIDA, EKET, KANO and YOLA are
four-letter STATION names that also match the ident shape, so they were filed
as idents of the PRECEDING station — KANO became a navaid at KAINJI — and 16
stations vanished.

A station name is the line IMMEDIATELY BEFORE a type or an inline row. An ident
always FOLLOWS a type. That is the document's own structure.

PAIRING IS NEVER GUESSED
------------------------
In the stacked layout the frequency list is LONGER than the ident list whenever
a VOR/DME publishes both a MHz frequency and a DME channel — 2 idents, 3
frequencies. Positional pairing would hand the locator "CH 72 X". So pairing
happens only when the counts agree exactly; otherwise every record carries the
station's full published list and says the pairing is ambiguous. A pilot then
reads what the AIP prints, which is the same choice AD 2.19's guard makes.
"""
import re

from extractor_base import ExtractResult, SubsectionExtractor, ValidationIssue

_TYPE_RE = re.compile(
    r"^((?:D?VOR(?:/DME)?|DME|NDB|LLZ(?:\s*\d{2}[LRC]?)?|GP|TACAN|L|LO|"
    r"ILS(?:/DME)?)(?:\s*\d{2}[LRC]?)?)$", re.I)

# Layout 1: "VOR/DME BDA 112.7 MHz"
_INLINE_RE = re.compile(
    r"^((?:D?VOR(?:/DME)?|DME|NDB|LLZ|GP|TACAN|L|LO|ILS(?:/DME)?)"
    r"(?:\s*\d{2}[LRC]?)?)\s+([A-Z]{2,4})\s+(.*)$")

# Layout 3: "ANU 113.8MHz"
_IDENT_FREQ_RE = re.compile(
    r"^([A-Z]{2,4})\s+(\d{2,3}[.,]\d{1,3}\s*MH[Zz]|\d{2,4}\s*[Kk][Hh][Zz])\s*(.*)$")

_NAME_LINE_RE = re.compile(r"^([A-Z][A-Z' \-/]{2,34})$")
_IDENT_RE = re.compile(r"^([A-Z]{2,4})$")
_FREQ_RE = re.compile(
    r"^(\d{2,3}[.,]\d{1,3}\s*MH[Zz]|\d{2,4}\s*[Kk][Hh][Zz]|CH\s*\d{1,3}\s*[XY]?)$")

# KATSINA publishes "13015.1N 0074106.9E" — a FIVE-digit latitude where six is
# the format. Accepting 5-6 digits captures it verbatim; rejecting it dropped
# the station's position entirely, which is worse than showing what the AIP
# prints. validate() flags the short form so it can be raised with NAMA.
_DATA_RE = re.compile(
    r"^(H24|\d{4}\s*-\s*\d{4})?\s*"
    r"(\d{5,6}(?:\.\d+)?[NS])\s*(\d{7}(?:\.\d+)?[EW])\s*(.*)$")
_HOURS_RE = re.compile(
    r"^(H24|\d{4}\s*-\s*\d{4}|\(H24\s+during|Hajj|Hajj\s+operation\)|operation\))$", re.I)
_ELEV_RE = re.compile(r"^([\d.]+\s*m(?:\s*\(\d+\s*ft\))?)\s*(.*)$", re.I)
_FRA_RE = re.compile(r"FRA\s*\(([ADEIX]+)\)", re.I)

_CHROME_RE = re.compile(
    r"^(?:ENR\s*4\.1-\d+|NIGERIA AIP|NIGERIAN AIRSPACE MANAGEMENT AGENCY|"
    r"ENR\s*4\.\s*RADIO NAVIGATION.*|ENR\s*4\.1\s+RADIO.*|"
    r"Navaids used for.*|categorized according.*|more functions.*|"
    r"in column FRA.*|Legend for FRA.*|\([ADEIX]\):.*|"
    r"Name of station|ID|Frequency|\(CH\)|Hours of|operation|Coordinates|"
    r"ELEV|DME|Antenna|Remarks|AIRAC\s+AMDT.*|\d{2}\s+[A-Z]{3}\s+\d{2}|"
    r"[\d\s]{1,12})\s*$", re.I)


def _clean_lines(text: str) -> list:
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or _CHROME_RE.match(line):
            continue
        out.append(line)
    return out


def find_station_starts(lines: list) -> list:
    """[(index, station_name)] — stations identified by POSITION.

    Handles a station name that WRAPS across two lines ("PORT" / "HARCOURT").
    The continuation index is recorded as consumed so it cannot also open a
    block of its own — that bug left "PORT" as an empty station emitting a
    warning while its navaid was filed under "HARCOURT"."""
    starts = []
    consumed = set()
    for i, ln in enumerate(lines):
        if i in consumed:
            continue
        if not _NAME_LINE_RE.match(ln) or _TYPE_RE.match(ln):
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if _INLINE_RE.match(nxt) or _TYPE_RE.match(nxt):
            starts.append((i, ln.strip()))
            continue
        # Wrapped name: NAME / NAME / TYPE
        nxt2 = lines[i + 2] if i + 2 < len(lines) else ""
        if (_NAME_LINE_RE.match(nxt) and not _TYPE_RE.match(nxt)
                and (_INLINE_RE.match(nxt2) or _TYPE_RE.match(nxt2))):
            starts.append((i, f"{ln.strip()} {nxt.strip()}"))
            consumed.add(i + 1)
    return starts


class ENR41Extractor(SubsectionExtractor):
    """ENR 4.1 — one record per NAVAID (by ident), not per station."""

    subsection = "4.1"
    kind = "tabular"

    def extract(self, scope_id: str, segments: list) -> ExtractResult:
        text = self.segment_text(segments) if segments else ""
        lines = _clean_lines(text)
        warnings = []

        starts = find_station_starts(lines)
        if not starts:
            return ExtractResult(icao=scope_id, subsection=self.subsection,
                                 kind=self.kind, scope_kind="ENR_NAVAID",
                                 records=[],
                                 warnings=["ENR 4.1: no station blocks found"])

        records, seen = [], set()
        for n, (idx, station) in enumerate(starts):
            end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
            body = lines[idx + 1:end]
            # Skip the wrapped-name continuation, already folded into `station`.
            if body and _NAME_LINE_RE.match(body[0]) and not _TYPE_RE.match(body[0]) \
                    and not _INLINE_RE.match(body[0]) \
                    and not _IDENT_FREQ_RE.match(body[0]):
                body = body[1:]

            paired = []                 # (type, ident, freq) stated by the doc
            types, idents, freqs = [], [], []
            # ONE COORDINATE LINE PER NAVAID. A multi-navaid station publishes
            # a position for each, in ident order:
            #     KANO / DVOR/DME / NDB / KAN / AO / ... /
            #       H24 120209.1N 0082945.8E   <- KAN, the DVOR/DME
            #           120331.2N 0083230.3E   <- AO,  the NDB
            # An earlier version kept a single `coords` and overwrote it, so
            # every record at the station got the LAST position — KAN was shown
            # at AO's location. A pilot tuning ILR and navigating to the
            # displayed coordinates would fly to the wrong place, which is the
            # misattribution this project exists to prevent.
            coord_list = []
            pending_type = None
            hours = elev = remarks = None

            def _absorb(tail):
                """Pull hours/coords/elev/remarks off a trailing fragment.

                Coordinates APPEND rather than overwrite — see coord_list."""
                nonlocal hours, elev, remarks
                dm = _DATA_RE.match((tail or "").strip())
                if not dm:
                    return
                hours = hours or (dm.group(1) or "").strip() or None
                coord_list.append(f"{dm.group(2)} {dm.group(3)}")
                rem = dm.group(4).strip()
                em = _ELEV_RE.match(rem)
                if em:
                    elev, rem = em.group(1).strip(), em.group(2).strip()
                if rem:
                    remarks = f"{remarks} {rem}".strip() if remarks else rem

            for ln in body:
                # Layout 3 — type already seen, this line is "IDENT FREQ".
                ifm = _IDENT_FREQ_RE.match(ln)
                if ifm and (pending_type or types):
                    t = pending_type or (types.pop() if types else None)
                    pending_type = None
                    paired.append([t, ifm.group(1), ifm.group(2).strip()])
                    _absorb(ifm.group(3))
                    continue
                # Layout 1 — type, ident and frequency together.
                im = _INLINE_RE.match(ln)
                if im:
                    rest = im.group(3).strip()
                    fm = re.match(r"^(\d{2,3}[.,]\d{1,3}\s*MH[Zz]|"
                                  r"\d{2,4}\s*[Kk][Hh][Zz])\s*(.*)$", rest)
                    paired.append([im.group(1).strip(), im.group(2).strip(),
                                   fm.group(1).strip() if fm else None])
                    _absorb(fm.group(2) if fm else rest)
                    continue
                if _TYPE_RE.match(ln):
                    types.append(ln.strip())
                    pending_type = ln.strip()
                    continue
                if _FREQ_RE.match(ln):
                    freqs.append(re.sub(r"\s+", " ", ln).strip())
                    continue
                if _IDENT_RE.match(ln):
                    idents.append(ln.strip())
                    continue
                if _HOURS_RE.match(ln):
                    hours = f"{hours} {ln}".strip() if hours else ln.strip()
                    continue
                if _DATA_RE.match(ln):
                    _absorb(ln)
                    continue

            entries = []
            # Layouts 1 and 3: the pairing is the document's, so any remaining
            # DME channel lines belong to that navaid.
            for t, ident, freq in paired:
                chans = [f for f in freqs if f.upper().startswith("CH")]
                entries.append((t, ident,
                                " / ".join([f for f in [freq] + chans if f]) or None,
                                False))
            # Layout 2: pair only when the counts agree exactly.
            if idents:
                ok_t = len(types) == len(idents)
                ok_f = len(freqs) == len(idents)
                if len(idents) > 1 and not (ok_t and ok_f):
                    warnings.append(
                        f"{station}: {len(idents)} idents, {len(types)} types, "
                        f"{len(freqs)} frequencies — pairing is ambiguous, so "
                        f"each record carries the station's full frequency list")
                for k, ident in enumerate(idents):
                    entries.append((
                        types[k] if ok_t and k < len(types)
                        else (" / ".join(types) if types else None),
                        ident,
                        freqs[k] if ok_f and k < len(freqs)
                        else (" / ".join(freqs) if freqs else None),
                        (not ok_f) and len(idents) > 1))

            if not entries:
                warnings.append(f"{station}: no navaid found — no record emitted")
                continue
            if not coord_list:
                warnings.append(f"{station}: no coordinates found")
            # Pair positions to navaids ONLY when the counts agree. ILORIN
            # publishes three coordinate lines for two idents; assigning by
            # position there would give one navaid a location the AIP never
            # gave it. Where they disagree, every record carries the station's
            # full published list and says so — visible ambiguity beats a
            # confident wrong position.
            coords_pairable = len(coord_list) == len(entries)
            if len(entries) > 1 and not coords_pairable:
                warnings.append(
                    f"{station}: {len(entries)} navaids but {len(coord_list)} "
                    f"coordinate line(s) — positions cannot be paired, so each "
                    f"record carries the station's full list")

            fra = None
            if remarks:
                fm = _FRA_RE.search(remarks)
                if fm:
                    fra = fm.group(1).upper()

            for k, (navaid_type, ident, freq, ambiguous) in enumerate(entries):
                if ident in seen:
                    warnings.append(f"{ident}: duplicate ident — second ignored")
                    continue
                seen.add(ident)
                records.append({
                    "scope_kind": "ENR_NAVAID",
                    "scope_id": ident,
                    "station": station,
                    "navaid_type": navaid_type,
                    "frequency": freq,
                    "frequency_is_ambiguous": ambiguous or None,
                    "hours": hours,
                    "coordinates": (coord_list[k] if coords_pairable
                                    else " / ".join(coord_list) or None),
                    "coordinates_are_ambiguous": (not coords_pairable
                                                  and len(entries) > 1) or None,
                    "elevation": elev,
                    "fra_relevance": fra,
                    "remarks": remarks,
                })

        return ExtractResult(
            icao=scope_id, subsection=self.subsection, kind=self.kind,
            scope_kind="ENR_NAVAID", records=records,
            embed_text="; ".join(
                f"{r['scope_id']} {r.get('navaid_type') or 'navaid'} at "
                f"{r['station']}, {r.get('frequency')}, {r.get('coordinates')}"
                for r in records),
            warnings=warnings)

    def validate(self, result: ExtractResult) -> list:
        """A navaid with no position or no frequency is not usable — both are
        the reason a pilot looks one up."""
        issues = []
        if not result.records:
            issues.append(ValidationIssue("error", "records",
                                          "ENR 4.1 produced no navaids"))
            return issues
        for rec in result.records:
            nm = rec.get("scope_id")
            if rec.get("coordinates") and re.match(
                    r"^\d{5}(?:\.\d+)?[NS]", rec["coordinates"]):
                issues.append(ValidationIssue(
                    "warning", nm,
                    f"{nm}: latitude {rec['coordinates'].split()[0]!r} has five "
                    f"digits, not six — as published. Query with NAMA."))
            if not rec.get("coordinates"):
                issues.append(ValidationIssue(
                    "error", nm, f"{nm}: no coordinates — a navaid with no "
                                 f"published position cannot be located"))
            if not rec.get("frequency"):
                issues.append(ValidationIssue("error", nm, f"{nm}: no frequency"))
        return issues
