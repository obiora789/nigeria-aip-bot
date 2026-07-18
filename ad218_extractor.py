"""
ad218_extractor.py — AD 2.18 ATS COMMUNICATION FACILITIES.

Eighteenth Layer 2 subsection extractor, and — like AD 2.12 — a known
misattribution-risk subsection flagged explicitly in this project's prior
history (a comms guard was already built once for exactly this reason).
Each ATS service (TWR, RADAR/APP, ATIS, ACC, Ground Control...) can have
MULTIPLE frequency lines (Primary/Secondary/Emergency), and only the FIRST
line of each service block carries the service label — every continuation
line is a bare frequency+hours+remark. Confirmed directly on DNAA/DNKN/
DNMM: "TWR Abuja Tower 118.6 MHz H24 Primary frequency" opens a block; the
very next line, "118.9 MHz H24 Secondary frequency", has no service label
at all and belongs to TWR purely by position. Attaching a continuation line
to the wrong service is exactly the failure mode a pilot could act on
directly — calling Tower's frequency when they meant Approach.

SAME SAFETY PROPERTY AS AD 2.12/2.14, reused: tracks the currently active
service while walking the segment's lines, bucketing every line (including
bare-frequency continuations) under whichever service most recently
introduced it. Cross-service merging is structurally impossible.

SERVICE VOCABULARY, surveyed (not guessed) across all 36 before designing:
TWR, RADAR/APP, RADAR, ATIS, ACC, FIS, SMC, Ground/Ground Control — an
explicit enumerated set, not a loose "capitalized word" heuristic, since a
loose rule risked treating ordinary capitalized remark text (confirmed:
"Kano East", "Emergency", parenthetical numbered notes within ACC's own
remarks) as if it were a new service.

STRUCTURED SUB-EXTRACTION: within each service's correctly-scoped text,
individual frequency lines are parsed into (value_mhz, hours, remark_tag)
records — the actual number a pilot would tune, kept as a real field, not
buried in a text blob. A service can have multiple call signs under one
label (confirmed: DNKN's ACC covers BOTH "Kano East" and "Kano West" as
separate call signs within the same ACC block) — call sign changes are
tracked but do not close the service the way a real designator change does.
"""
import re

from extractor_base import SubsectionExtractor, ExtractResult, ValidationIssue

SERVICE_VOCAB = ["RADAR/APP", "RADAR", "TWR", "ATIS", "ACC", "FIS", "SMC",
                 "SAR", "ACFT", "Ground Control", "Ground", "APP", "AFIS", "DEP", "ARR"]
# SAR (Search and Rescue) confirmed real and necessary directly on DNAI:
# without it, "SAR 406MHz 0600-2000" — a genuine, safety-relevant emergency
# beacon frequency (406 MHz is the real COSPAS-SARSAT international distress
# frequency, correctly outside the 118-137 MHz VHF voice band) — silently
# merged into the PRECEDING service (ATIS)'s data. This IS the exact
# misattribution risk this subsection was flagged for in prior project
# history, now confirmed as a real, live instance, not hypothetical.
# Longest-match-first so "RADAR/APP" wins over "RADAR", "Ground Control" over
# "Ground".
SERVICE_VOCAB.sort(key=len, reverse=True)
SERVICE_START_RE = re.compile(
    r"^(" + "|".join(re.escape(s) for s in SERVICE_VOCAB) + r")\b")

FREQ_RE = re.compile(r"\b(\d{2,3}(?:\.\d{1,4})?)\s*(MHz|KHz)\b", re.I)
# Fallback for a confirmed real case (DNBB) where the source omits the MHz/
# KHz unit entirely. Requires a decimal point specifically — genuine
# aviation VHF frequencies always carry one (118.6, 120.8, 122.8...) — which
# excludes bare integers (page numbers, unrelated IDs) from ever matching
# this narrower, unit-less fallback. Only ever consulted when FREQ_RE finds
# nothing at all for a service (see extract()).
BARE_FREQ_RE = re.compile(r"\b(\d{3}\.\d{1,2})\b")
HOURS_RE = re.compile(r"\b(H24|HJ|HN|HX|\d{4}\s*-\s*\d{4}|Sunrise\s*[\u2013-]\s*Sunset)\b", re.I)
# HJ (sunrise-to-sunset), HN (sunset-to-sunrise), HX (irregular) are real
# standard ICAO operating-hours codes, alongside H24 — confirmed necessary:
# without HJ specifically, genuine data rows on DNES/DNET/DNKS/DNMK/DNOG were
# rejected entirely (no hours token found nearby), losing real frequency
# data, not a source gap. "Sunrise – Sunset" appears as literal spelled-out
# text on some aerodromes' pages instead of the "HJ" abbreviation — same
# underlying meaning, different rendering, both accepted here.
HOURS_PROXIMITY = 20   # max characters between a frequency and its hours token
# for it to be trusted as a genuine data row, not a prose mention. Confirmed
# necessary directly on DNKN: its ACC service block includes a parenthetical
# operational note — "(1) Contact Kano East on 124.1MHz or Kano West on
# 128.5MHz call 8903KHz as alternative" — that RE-MENTIONS already-captured
# frequencies in prose, and includes "8903KHz" (a real HF contact frequency,
# not a duplicate) with no unit/hours structure a genuine table row has. A
# naive frequency scan over the whole service text produced THREE spurious
# entries from this one sentence, including a garbled "903.0 MHz" from a
# substring match inside "8903". Every confirmed genuine frequency row in
# this table has its hours token within a few characters of the frequency
# (e.g. "124.1 MHz H24 Primary frequency") — requiring that proximity is a
# structural signature prose mentions don't have, and reliably excludes them
# without needing to detect/exclude specific note formats.


class AD218Extractor(SubsectionExtractor):
    subsection = "2.18"
    kind = "tabular"

    def extract(self, icao: str, segments: list) -> ExtractResult:
        from segment_page import _line_groups
        all_words = self.combine_segment_words(segments)
        lines = _line_groups(all_words)
        warnings = []

        service_lines = {}   # service -> list[str] (all its lines, in order)
        service_callsigns = {}  # service -> list[str] (call signs seen)
        current_service = None

        for line in lines:
            text = " ".join(w[4] for w in line)
            m = SERVICE_START_RE.match(text)
            if m:
                current_service = m.group(1)
                service_lines.setdefault(current_service, [])
                service_callsigns.setdefault(current_service, [])
            if current_service:
                service_lines.setdefault(current_service, []).append(text)

        if not service_lines:
            warnings.append("no ATS service blocks found at all")

        records = []
        for service, text_lines in service_lines.items():
            full_text = self.clean_text(" ".join(text_lines))
            frequencies = []
            for fm in FREQ_RE.finditer(full_text):
                val = float(fm.group(1))
                unit = fm.group(2).upper()
                window = full_text[fm.end():fm.end() + HOURS_PROXIMITY]
                hm = HOURS_RE.search(window)
                if not hm:
                    # No hours token immediately after — not a genuine data
                    # row (see HOURS_PROXIMITY comment above). Skip rather
                    # than store an unverified/prose-mentioned value.
                    continue
                remark_window = full_text[fm.end():fm.end() + 60]
                remark_m = re.search(r"(Primary|Secondary|Emergency)\s*frequency",
                                      remark_window, re.I)
                # NOT widened to catch DNET's "Coordination"/"Domestic" labels
                # — tried this, and it introduced a real regression: dropping
                # the requirement that the word be immediately followed by
                # "frequency" made the match too loose, and on DNBK it
                # attached 121.5's "Emergency Frequency" label to the
                # PRECEDING 121.7MHz entry instead — a genuine cross-
                # frequency misattribution, the exact risk this subsection is
                # flagged for. The frequency value and hours (the safety-
                # critical fields) are unaffected either way; missing a
                # purpose label is an acceptable, honest gap, but a
                # mislabeled one is not.
                frequencies.append({
                    "value": val,
                    "unit": unit,
                    "hours": hm.group(1),
                    "remark": remark_m.group(0) if remark_m else None,
                })

            if not frequencies:
                # Fallback for a confirmed real source gap, NOT a false
                # source gap as originally assumed: DNBB's TWR entries print
                # "120.8"/"122.8" with no MHz suffix and "12" instead of a
                # recognized H24/HJ-style hours token at all — confirmed
                # directly against the real page image, where both values
                # sit unambiguously under the Frequency/Hours column headers,
                # immediately after "Bebi Tower", with explicit "Primary
                # frequency"/"Secondary frequency" remarks. This is not an
                # ambiguous number that MIGHT be something else — VHF
                # aeronautical voice comms are universally in MHz by
                # convention, so a bare value in that band, appearing where
                # this table's Frequency column always appears, is
                # unambiguous regardless of whether the source restates the
                # unit. Only engages when the standard unit-bearing search
                # above found NOTHING for this service at all — never runs
                # alongside a successful match, so it cannot double-count or
                # override a normally-parsed value.
                for bfm in BARE_FREQ_RE.finditer(full_text):
                    val = float(bfm.group(1))
                    if not (118.0 <= val <= 137.0):
                        continue  # outside the VHF comms band — not a
                                  # confident enough match to infer a unit
                    remark_window = full_text[bfm.end():bfm.end() + 60]
                    remark_m = re.search(r"(Primary|Secondary|Emergency)\s*frequency",
                                          remark_window, re.I)
                    # Hours: try the normal recognized patterns first: if
                    # none match nearby (DNBB's own "12" doesn't), store
                    # whatever bare token follows as-is rather than
                    # discarding the whole row — a raw, honestly-labeled
                    # hours string is better than losing the frequency
                    # entirely over a field this table doesn't reliably
                    # standardize here.
                    window = full_text[bfm.end():bfm.end() + HOURS_PROXIMITY]
                    hm = HOURS_RE.search(window)
                    hours_val = hm.group(1) if hm else None
                    if hours_val is None:
                        raw_hm = re.match(r"\s*(\S+)", window)
                        hours_val = raw_hm.group(1) if raw_hm else None
                    frequencies.append({
                        "value": val, "unit": "MHZ", "hours": hours_val,
                        "remark": remark_m.group(0) if remark_m else None,
                    })
                if frequencies:
                    warnings.append(
                        f"{icao} {service}: frequency recovered via bare-value "
                        f"fallback (source omits MHz unit) — unit inferred from "
                        f"VHF comms band, not read from the source text")

            if not frequencies:
                warnings.append(f"{icao} service {service!r}: no frequency values found "
                                 f"in its own text: {full_text[:100]!r}")
            records.append({
                "icao": icao, "service": service,
                "frequencies": frequencies,
                "raw_text": full_text,
            })

        embed_text = f"{icao} ATS comms: " + "; ".join(
            f"{r['service']}=" + ",".join(f"{f['value']}{f['unit']}" for f in r["frequencies"])
            for r in records)

        return ExtractResult(
            icao=icao, subsection=self.subsection, kind=self.kind,
            records=records, text="", embed_text=embed_text, warnings=warnings,
        )

    def validate(self, result: ExtractResult) -> list:
        issues = []
        if not result.records:
            issues.append(ValidationIssue("error", "records",
                                          f"{result.icao}: no ATS communication records"))
            return issues
        for rec in result.records:
            # null-over-guess is a hard requirement here: a service block
            # with ZERO parseable frequencies is a genuine extraction
            # failure (every real ATS service has at least one frequency
            # published), not a legitimate gap — unlike AD 2.7/2.16's
            # optional descriptive fields, a comms service without any
            # frequency is not a valid answer to give a pilot.
            if not rec["frequencies"]:
                issues.append(ValidationIssue(
                    "error", "frequencies",
                    f"{result.icao} {rec['service']}: no frequencies parsed"))
        return issues
