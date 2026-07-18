"""
ad214_extractor.py — AD 2.14 APPROACH AND RUNWAY LIGHTING.

Fourteenth Layer 2 subsection extractor. Tabular kind, one record per
physical runway (paired ends, like AD212Extractor) — but NO structured
numeric fields, unlike AD 2.12/2.13. This table (10 columns: APCH LGT, THR
LGT, VASIS/PAPI, TDZ LGT, centreline LGT, edge LGT, end LGT, SWY LGT,
remarks) is overwhelmingly categorical/descriptive text — confirmed
directly on DNAA and DNKN: values are mostly "Available"/"NIL"/"PALS CAT I"
plus PAPI angle-and-displacement prose, with no comparable clean number the
way AD 2.12 had designation+dimensions or AD 2.13 had TORA/TODA/ASDA/LDA.

SAME SAFETY PROPERTY AS AD 2.12, reused directly: tracks the currently
active runway end while walking the segment's lines, so every line's text
is bucketed under whichever end most recently introduced it — cross-end
merging (04's lighting details bleeding into 22's) is structurally
impossible. Confirmed necessary: DNKN shows some ends with genuinely
minimal data ("05 NIL", "23 NIL") right next to others with substantial
detail ("06 PALS PAPI 400m from THR, CAT I, angle and direction of
displacement...") — real variation that must stay correctly attributed per
end, not a bug to normalize away.
"""
import re

from extractor_base import SubsectionExtractor, ExtractResult, ValidationIssue
from ad212_extractor import _opp, _norm_end

END_START_RE = re.compile(r"^(\d{1,2}[LRC]?)\s+(?=[A-Z])")
# Confirmed necessary: a loose "bare number at line start" rule falsely
# matched PAPI angle/displacement fragments ("3°35', 3°15',") and the
# "Minimum Eye height -17.83m" line (both start with digits at a line
# boundary after wrapping), plus the column-number header row itself
# ("1 2 3 4 5 6 7 8 9 10", where "1" alone looked like designator "01").
# Every GENUINE end-marker row in this table is immediately followed by a
# capitalized status word (PALS/NIL/Available/AVBL/Not) — requiring an
# uppercase letter right after the whitespace excludes all three false-
# positive shapes (a degree symbol, a decimal point, or another digit).


class AD214Extractor(SubsectionExtractor):
    subsection = "2.14"
    kind = "tabular"

    def extract(self, icao: str, segments: list) -> ExtractResult:
        from segment_page import _line_groups
        all_words = self.combine_segment_words(segments)
        lines = _line_groups(all_words)
        warnings = []

        end_text = {}
        current_end = None

        for line in lines:
            text = " ".join(w[4] for w in line)
            m = END_START_RE.match(text)
            if m:
                current_end = _norm_end(m.group(1))
                end_text.setdefault(current_end, [])
            if current_end:
                end_text.setdefault(current_end, []).append(text)

        if not end_text:
            # No end-marker rows found. Confirmed two real reasons, both
            # genuine source content, not extraction failure: DNAK/DNZA are
            # pure prose ("Not available."), while DNAN/DNBY/DNGO/DNMK/DNSU
            # show the full table header but genuinely zero data rows after
            # it (the same pattern already confirmed for AD 2.13's DNBB —
            # lighting simply isn't published for any runway at these
            # aerodromes). Either way, capture what IS there as a single
            # general_notes record rather than silently producing nothing —
            # "no lighting published" is a real, useful fact, and this keeps
            # it distinguishable from a genuine extraction failure.
            full_text = self.clean_text(" ".join(w[4] for w in all_words))
            warnings.append("no runway-end lighting rows found — "
                             "captured whole segment as general_notes")
            runways = [{
                "icao": icao, "designation": None,
                "end_detail": {"general_notes": full_text},
            }] if full_text.strip() else []
            if not full_text.strip():
                warnings.append("AD 2.14 segment produced no text at all")
            return ExtractResult(
                icao=icao, subsection=self.subsection, kind=self.kind,
                records=runways, text="", embed_text=full_text, warnings=warnings,
            )

        seen = set()
        runways = []
        for end in sorted(end_text, key=lambda e: int(re.match(r'\d+', e).group())):
            if end in seen:
                continue
            opp_end = _opp(end)
            pair_present = opp_end in end_text
            seen.add(end)
            if pair_present:
                seen.add(opp_end)
            ends_sorted = sorted([end] + ([opp_end] if pair_present else []),
                                  key=lambda e: int(re.match(r'\d+', e).group()))
            designation = "/".join(ends_sorted)
            runways.append({
                "icao": icao,
                "designation": designation,
                "end_detail": {
                    e: self.clean_text(" ".join(end_text.get(e, [])))
                    for e in ends_sorted
                },
            })

        embed_text = f"{icao} AD 2.14 runway lighting: " + "; ".join(
            r["designation"] for r in runways)

        return ExtractResult(
            icao=icao, subsection=self.subsection, kind=self.kind,
            records=runways, text="", embed_text=embed_text, warnings=warnings,
        )

    def validate(self, result: ExtractResult) -> list:
        issues = []
        if not result.records:
            issues.append(ValidationIssue("error", "records",
                                          f"{result.icao}: no AD 2.14 records produced"))
            return issues
        for rec in result.records:
            # No field here is safety-critical enough to block on emptiness
            # — "NIL" (no lighting) is a real, valid, common answer, not a
            # gap. Only total extraction failure (already checked above)
            # blocks.
            for end, detail in rec["end_detail"].items():
                if not detail.strip():
                    issues.append(ValidationIssue(
                        "error", "end_detail",
                        f"{result.icao} {rec['designation']} [{end}]: no text captured at all"))
        return issues
