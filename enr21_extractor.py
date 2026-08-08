#!/usr/bin/env python3
"""
enr21_extractor.py — ENR 2.1 FIR, UIR, TMA and CTA.

WHAT THIS SECTION ACTUALLY LOOKS LIKE
-------------------------------------
Not the AD 2.17 field table. That was the prediction; the real pages are a
HIERARCHY, published as free-flowing text:

    KANO Flight Information Region (KANO FIR)
      KANO SECTOR
        KANO EAST SECTOR
        KANO WEST SECTOR
      LAGOS SECTOR
        LAGOS EAST SECTOR
        LAGOS WEST SECTOR
    ABUJA Terminal Control Area (TMA)
    CALABAR Terminal Control Area (TMA)
    ...

Each entity is a NAME LINE followed by a multi-line lateral-limits paragraph,
then vertical limits and class stacked as bare lines, then the ATS unit, call
sign, language, hours and frequencies. There are no "Label=Value" pairs and no
column delimiters — the name line IS the delimiter.

WHY THE NAME LINE IS A SAFE DELIMITER
-------------------------------------
It is structurally identifiable without guessing at wording: an entity line is
ALL-CAPS or title-case, contains one of the published airspace kinds (FIR,
SECTOR, TMA, CTA, UIR), and is short. The lateral-limits prose that follows is
sentence-case and long. Verified against all four real pages.

NESTING IS PRESERVED, NOT FLATTENED
-----------------------------------
KANO EAST SECTOR is inside KANO SECTOR is inside KANO FIR, and each level
publishes DIFFERENT vertical limits and a different class — KANO EAST is
Class A above FL 145 and Class D below. Flattening them would let one level's
limits answer a question about another, which is the misattribution this
project exists to prevent. Each becomes its own scope, with its parent
recorded.

WHAT IS DELIBERATELY NOT PARSED
-------------------------------
Vertical limits arrive as bare stacked lines ("UNL" / "FL 145" /
"Class of airspace: A" / "FL 145" / "3 500FT AMSL" / "Class of airspace: D") —
TWO limit bands for one entity, upper and lower, each with its own class. They
are captured as an ordered block, verbatim, rather than split into named
fields. Splitting would require deciding which band a bare "FL 145" belongs to,
and that decision cannot be made from the text alone: it is the same
adjacent-value ambiguity that produced the Maiduguri splice. A pilot reads the
block as published.
"""
import re

from extractor_base import ExtractResult, SubsectionExtractor, ValidationIssue

# THE ENTITY SET IS DISCOVERED FROM THE DOCUMENT, NOT PATTERN-MATCHED.
#
# An earlier version treated any short line naming FIR/SECTOR/TMA as an entity.
# That produced four false ones from prose fragments — and worse, "Kaduna TMA"
# (a phrase INSIDE Kano TMA's lateral limits: "...delineated to the south-west
# by Kaduna TMA") became an entity and ABSORBED Kano TMA's Class B and FL 145.
# Kano's own record was left with no vertical limits at all. That is the
# misattribution class this project exists to prevent, arriving through the
# entity boundary rather than the value.
#
# Two things make discovery reliable here:
#
#  (a) The AIP DECLARES its hierarchy in its own words:
#         "(KANO FIR consists of KANO SECTOR and LAGOS SECTOR)"
#         "(KANO SECTOR consists of KANO EAST SECTOR and KANO WEST SECTOR)"
#         "(LAGOS SECTOR consists of LAGOS EAST SECTOR and LAGOS WEST SECTOR)"
#      Those sentences name every sector, so sectors need not be guessed.
#
#  (b) A TMA/CTA/FIR entity line is a HEADING: it ENDS with its kind (possibly
#      parenthesised), rather than merely mentioning it. "KANO Terminal Control
#      Area (TMA)" is a heading; "...to the south-west by Kaduna TMA." is a
#      sentence that happens to end in the same letters — the trailing full
#      stop and the lower-case body distinguish them.
#
# Neither test is a vocabulary guess about how a pilot might phrase something.
# Both read the document's own structure.

# "X consists of A and B" — the AIP's own hierarchy declaration.
_CONSISTS_RE = re.compile(
    r"([A-Z][A-Z\s]{2,40}?)\s+consists\s+of\s+(.+?)(?:\)|$)", re.I | re.S)

# A heading line: an ALL-CAPS or Title-Case place name followed by its kind,
# ending there. Anchored at both ends — that is what excludes a sentence.
_HEADING_RE = re.compile(
    r"^([A-Z][A-Za-z' ]{1,28}?)\s+"
    r"(Flight\s+Information\s+Region|Terminal\s+Control\s+Area|Control\s+Area)"
    r"(?:\s*\((FIR|TMA|CTA|UIR)\))?\s*$", re.I)

# A bare continuation of a heading split across lines: "PORT HARCOURT Terminal
# Control Area" / "(TMA)".
_KIND_ONLY_RE = re.compile(r"^\((FIR|UIR|TMA|CTA)\)$", re.I)

# Class of airspace, published on its own line.
_CLASS_RE = re.compile(r"^Class\s+of\s+airspace\s*:\s*(.+)$", re.I)

# A vertical level as published: FL 145, UNL, GND, 3 500FT AMSL, 2 500FT ALT.
_LEVEL_RE = re.compile(
    r"^(?:UNL|GND|MSL|FL\s?\d{2,3}|\d[\d\s]{0,6}\s*FT\s*(?:AMSL|ALT|AGL)?)$", re.I)

# Frequencies and hours. "NIL" is the adjacent Purpose column and is stripped:
# leaving it made a frequency read "127.9 MHz NIL", which looks like a value
# qualifier rather than a neighbouring empty cell.
_FREQ_RE = re.compile(r"\b\d{2,3}[.,]\d{1,3}\s*MH[Zz]|\b\d[\d\s]{2,6}\s*kHz\b")
_PURPOSE_NIL_RE = re.compile(r"\s*\bNIL\b\s*$", re.I)
_HOURS_RE = re.compile(r"^(?:H24|\d{4}\s*-\s*\d{4}.*)$", re.I)

# Page furniture and the repeated column-header block.
_CHROME_RE = re.compile(
    r"^(?:ENR\s*2\.1-\d+|NIGERIA AIP|NIGERIAN AIRSPACE MANAGEMENT AGENCY|"
    r"ENR\s*2\.\s*AIR TRAFFIC SERVICES AIRSPACE|ENR\s*2\.1\s+FIR.*|"
    r"Name|Lateral limits|Vertical limits|Class of airspace|"
    r"Unit providing|service|Call sign|Languages|Area and conditions.*|"
    r"of use|use|Hours of service|Frequency/?|Purpose|Remarks|"
    r"AIRAC\s+AMDT.*|\d{2}\s+[A-Z]{3}\s+\d{2}|[\d\s]{1,10})\s*$", re.I)

# 2.1.1 is explanatory prose about airspace classification, not an entity.
_PROSE_START_RE = re.compile(r"^2\.1\.\d+\s+", re.I)

# A line opening with a bracketed COORDINATE or navaid ident is lateral-limits
# prose that happens to wrap, not a note: "(083936.96N 0065200.52E) that cuts
# the Kano/".
_COORD_IN_PROSE_RE = re.compile(r"^\(\s*\d{6}", re.I)

# Column 3: languages and conditions of use.
_LANGUAGE_RE = re.compile(r"^(?:EN|FR|EN\s+and\s+FR)$", re.I)

# Column 5: remarks — CPDLC logon addresses, level bands, NIL.
_REMARK_RE = re.compile(
    r"^(?:CPDLC\s+LOGON\s+\S+|NIL|FL\s?\d{2,3}\s*-\s*(?:FL\s?\d{2,3}|UNL)"
    r"\s*-\s*[A-G]|H24\s+during\s+.*)$", re.I)

# Column 2/3: the ATS unit and its call sign. ALL-CAPS, short, and — the part
# that matters — containing no boundary vocabulary, which is what separates
# "KANO CONTROL KANO EAST" from a lateral-limits sentence.
_UNIT_RE = re.compile(r"^[A-Z][A-Z /'’.\-]{3,60}$")
_LIMITS_PROSE_RE = re.compile(
    r"\d{4,6}[NS]|\bthence\b|\bjoining\b|\bboundary\b|\bcircle\b|"
    r"\bstraight\b|\bcentred\b|\bradius\b|\bdelineated\b", re.I)


def _clean_lines(text: str) -> list:
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or _CHROME_RE.match(line):
            continue
        out.append(line)
    return out


def _norm_name(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip(" .")


def discover_entities(lines: list):
    """The airspace names this document publishes, and their parents.

    Returns {name: parent_or_None}. Sectors come from the AIP's own "consists
    of" sentences; FIR/TMA/CTA come from heading lines. Nothing is inferred
    from a name that only APPEARS in prose."""
    blob = " ".join(lines)
    hierarchy = {}

    # (a) declared parent/child relationships
    for m in _CONSISTS_RE.finditer(blob):
        parent = _norm_name(m.group(1))
        for child in re.split(r"\s+and\s+|,\s*", m.group(2)):
            child = _norm_name(child)
            if re.search(r"\b(SECTOR|FIR|TMA|CTA)\b", child, re.I) and len(child) < 40:
                hierarchy[child] = parent
        hierarchy.setdefault(parent, None)

    # (b) heading lines, including a kind split onto the next line
    for i, ln in enumerate(lines):
        m = _HEADING_RE.match(ln)
        if not m:
            continue
        name = _norm_name(ln)
        if i + 1 < len(lines) and _KIND_ONLY_RE.match(lines[i + 1]):
            name = f"{name} {lines[i + 1].strip()}"
        hierarchy.setdefault(_norm_name(name), None)

    return hierarchy


class ENR21Extractor(SubsectionExtractor):
    """ENR 2.1 — one record per FIR / UIR / sector / TMA / CTA."""

    subsection = "2.1"
    kind = "tabular"

    def extract(self, scope_id: str, segments: list) -> ExtractResult:
        text = self.segment_text(segments) if segments else ""
        lines = _clean_lines(text)
        warnings = []

        hierarchy = discover_entities(lines)
        if not hierarchy:
            return ExtractResult(icao=scope_id, subsection=self.subsection,
                                 kind=self.kind, scope_kind="ENR_AIRSPACE",
                                 records=[],
                                 warnings=["ENR 2.1: no airspace entities found"])

        # A line OPENS a block only if it is exactly a discovered entity name.
        # Requiring an exact match is what stops "Kaduna TMA." — a phrase in
        # Kano TMA's lateral limits — from opening a block and stealing Kano's
        # vertical limits and class.
        names = set(hierarchy)
        starts = []
        for i, ln in enumerate(lines):
            nm = _norm_name(ln)
            if nm in names:
                starts.append((i, nm))
                continue
            # a heading whose kind wrapped to the next line
            if i + 1 < len(lines) and _KIND_ONLY_RE.match(lines[i + 1]):
                joined = _norm_name(f"{ln} {lines[i + 1].strip()}")
                if joined in names:
                    starts.append((i, joined))

        found = {nm for _, nm in starts}
        # The AIP names the FIR twice — "KANO Flight Information Region" as the
        # heading and "(KANO FIR)" as its short form in the consists-of
        # sentence. They are ONE entity, so a declared name whose words are all
        # present in a found name is an alias, not a missing block.
        def _is_alias(nm):
            toks = {t for t in re.findall(r"[A-Z]{2,}", nm.upper())
                    if t not in {"FIR", "UIR", "TMA", "CTA"}}
            return any(toks and toks <= set(re.findall(r"[A-Z]{2,}", f.upper()))
                       for f in found)

        for nm in sorted(names - found):
            if _is_alias(nm):
                continue
            warnings.append(f"{nm}: declared by the AIP but no block found")

        records = []
        for n, (idx, name) in enumerate(starts):
            end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
            block = lines[idx:end]

            body, note = [], None
            levels, classes, freqs, hours = [], [], [], []
            units, languages, remarks = [], [], []
            depth = 0
            for ln in block[1:]:
                if _PROSE_START_RE.match(ln):
                    break                      # 2.1.1 classification prose
                # A parenthetical note may WRAP across lines — "(LAGOS SECTOR
                # consists of LAGOS EAST" / "SECTOR and LAGOS WEST SECTOR)".
                # Track bracket depth so the continuation is absorbed into the
                # note rather than left behind as stray body text.
                # A NOTE is a whole-line parenthetical — "(KANO SECTOR consists
                # of ...)" — possibly wrapped across lines. It is NOT a bracket
                # appearing inside prose: the lateral limits are full of them
                # ("(KAN VOR)", "(ERMAD)", "(IKROP)"), and treating those as
                # notes both invented a note and cut a fragment out of the
                # limits text a pilot needs whole.
                #
                # The distinction is structural: a note line STARTS with "(",
                # a prose bracket does not.
                if depth:
                    note = f"{note} {ln}".strip() if note else ln
                    depth += ln.count("(") - ln.count(")")
                    depth = max(depth, 0)
                    continue
                if ln.startswith("(") and not _COORD_IN_PROSE_RE.match(ln):
                    note = f"{note} {ln}".strip() if note else ln
                    depth = max(ln.count("(") - ln.count(")"), 0)
                    continue
                cm = _CLASS_RE.match(ln)
                if cm:
                    classes.append(cm.group(1).strip())
                    continue
                if _LEVEL_RE.match(ln):
                    levels.append(ln.strip())
                    continue
                if _HOURS_RE.match(ln):
                    hours.append(ln.strip())
                    continue
                if _FREQ_RE.search(ln):
                    freqs.append(_PURPOSE_NIL_RE.sub("", ln).strip())
                    continue
                # COLUMNS 2, 3 and 5 — the ATS unit, its call sign and
                # language, and the remarks — are separate published columns,
                # not part of the lateral limits. Confirmed against the printed
                # table: "KANO CONTROL" is column 2 and "KANO EAST / EN / H24"
                # is column 3, while the boundary description is column 1.
                # Leaving them in meant a pilot asking where a sector's
                # boundary runs was also handed ATC callsigns and CPDLC logon
                # addresses.
                if _LANGUAGE_RE.match(ln):
                    languages.append(ln.strip())
                    continue
                if _REMARK_RE.match(ln):
                    remarks.append(ln.strip())
                    continue
                if _UNIT_RE.match(ln) and not _LIMITS_PROSE_RE.search(ln):
                    units.append(ln.strip())
                    continue
                body.append(ln.strip())

            lateral = " ".join(body).strip()
            # A GROUPING is any entity that is a parent of another. The
            # earlier test also required it to have no parent itself, which
            # excluded KANO SECTOR and LAGOS SECTOR — both are children of
            # KANO FIR AND parents of their east/west halves. They publish no
            # limits of their own, so they were then reported as defective for
            # lacking values they are not supposed to have.
            is_group = any(v == name for v in hierarchy.values())
            if not levels and not is_group:
                warnings.append(f"{name}: no vertical limits found")
            if not lateral and not is_group:
                warnings.append(f"{name}: no lateral limits found")

            records.append({
                "scope_kind": "ENR_AIRSPACE",
                "scope_id": name,
                "parent": hierarchy.get(name),
                # A grouping (KANO SECTOR) publishes no limits of its own —
                # its children do. Recording that explicitly stops a missing
                # value being read as a defect, and stops a pilot being told
                # a container has limits it does not have.
                # A GROUPING has children that publish their own limits. It
                # may still publish a class of its own — KANO SECTOR prints
                # "Class of airspace: A, D" with no vertical limits at all,
                # confirmed against the printed table. So "grouping" means
                # "publishes no vertical extent", not "publishes nothing".
                "is_grouping": is_group or None,
                "note": _norm_name(note) if note else None,
                "lateral_limits": lateral or None,
                "vertical_limits": " / ".join(levels) if levels else None,
                "airspace_class": " / ".join(classes) if classes else None,
                "hours": " / ".join(dict.fromkeys(hours)) if hours else None,
                "ats_unit": " / ".join(dict.fromkeys(units)) if units else None,
                "languages": " / ".join(dict.fromkeys(languages)) if languages else None,
                "remarks": " / ".join(dict.fromkeys(remarks)) if remarks else None,
                "frequencies": " / ".join(dict.fromkeys(freqs)) if freqs else None,
            })

        return ExtractResult(
            icao=scope_id, subsection=self.subsection, kind=self.kind,
            scope_kind="ENR_AIRSPACE", records=records,
            embed_text="; ".join(
                f"{r['scope_id']} airspace, vertical limits "
                f"{r.get('vertical_limits')}, class {r.get('airspace_class')}"
                for r in records),
            warnings=warnings)

    def validate(self, result: ExtractResult) -> list:
        """An airspace with no vertical limits is not storable.

        This section exists to say how high a piece of controlled airspace
        reaches and what class it is. A record naming one without those answers
        a pilot's question with a confident non-answer."""
        issues = []
        if not result.records:
            issues.append(ValidationIssue("error", "records",
                                          "ENR 2.1 produced no airspace records"))
            return issues
        for rec in result.records:
            nm = rec.get("scope_id")
            if rec.get("is_grouping"):
                # A container publishes no limits of its own; demanding them
                # would reject a correct record.
                continue
            if not rec.get("vertical_limits"):
                issues.append(ValidationIssue(
                    "error", nm, f"{nm}: no vertical limits — an airspace with "
                                 f"unknown vertical extent cannot be published"))
            if not rec.get("lateral_limits"):
                issues.append(ValidationIssue(
                    "error", nm, f"{nm}: no lateral limits"))
        return issues
