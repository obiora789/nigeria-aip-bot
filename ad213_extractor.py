"""
ad213_extractor.py — AD 2.13 DECLARED DISTANCES.

Thirteenth Layer 2 subsection extractor. Tabular kind, one record per
declared runway direction (TORA/TODA/ASDA/LDA).

REWRITTEN (round 2) after real, user-caught bugs in the count-based version:

BUG 1 (DNSO, confirmed and fixed): the thousands-joining helper disabled
itself whenever ANY decimal appeared anywhere in a row's text — including
an UNRELATED one in the remarks ("1 200 m at 1.2%"), which silently
corrupted every genuinely-integer value on that row (produced TORA=3/
TODA=0 instead of 3000/3000). Checked all 36 aerodromes directly for the
same signature after fixing it; DNSO was the only one affected — its
remarks happened to be the only ones in the whole dataset containing a
decimal (a slope percentage). Fixed by joining thousands per-TOKEN
(merging an adjacent bare 1-2-digit + bare 3-digit pair) instead of a
single blanket regex over the whole line.

BUG 2 (DNKT, confirmed and fixed — the more fundamental one): the
count-based version assumed "the first 4 numbers found, in reading order,
are TORA/TODA/ASDA/LDA in that order" — which is WRONG whenever a column is
genuinely blank in the source, because it silently shifts every later
value one column to the left. Confirmed directly on DNKT via real word
x-positions: its row reads "05 3 500 3 500 3 500 NIL" with only 3 numeric
groups, and TORA's own column position (x0=147.7) has NO value there at
all — the 3 values found are TODA/ASDA/LDA (x0=232.7/317.8/402.8), not
TORA/TODA/ASDA. The count-based version put the first value in TORA,
producing a wrong-by-one-column result even though it "looked" plausible
(a whole number, not obviously an error).

FIX: assign each numeric token to a column by X-POSITION, not by count or
reading order — the same gutter/proximity technique already used
throughout this Layer 2 rebuild (AD 2.10's a-f markers, parse_key_value_
rows' gutter detection), extended to 5 columns here. Column boundaries are
read directly from the page's own "TORA"/"TODA"/"ASDA"/"LDA"/"Remarks"
header words, confirmed present in the segment's own words. A column with
no token assigned to it is null — a TRUE reflection of a blank source
cell, never filled by positional guessing.
"""
import re

from extractor_base import SubsectionExtractor, ExtractResult, ValidationIssue

DESIGNATOR_RE = re.compile(r"^(\d{2}[LRC]?)\b")
HEADER_LABELS = ["TORA", "TODA", "ASDA", "LDA", "Remarks"]


def _join_thousands_tokens(word_texts):
    """word_texts: list of word strings already bucketed to ONE column, in
    reading order. Joins an adjacent bare 1-2-digit token + bare 3-digit
    token (a genuine thousands separator, e.g. "3"+"500" -> "3500");
    returns the joined text. Token-based per BUG 1's fix above — operating
    on a single column's own tokens only, so there is no risk of an
    unrelated decimal elsewhere interfering."""
    out = []
    i = 0
    while i < len(word_texts):
        t = word_texts[i]
        if (i + 1 < len(word_texts) and re.fullmatch(r"\d{1,2}", t)
                and re.fullmatch(r"\d{3}", word_texts[i + 1])):
            out.append(t + word_texts[i + 1])
            i += 2
        else:
            out.append(t)
            i += 1
    return " ".join(out)


class AD213Extractor(SubsectionExtractor):
    subsection = "2.13"
    kind = "tabular"

    def extract(self, icao: str, segments: list) -> ExtractResult:
        from segment_page import _line_groups
        all_words = self.combine_segment_words(segments)
        warnings = []

        # Column boundaries: read the header words' own x-positions directly
        # from the page, rather than assuming a fixed layout — confirmed
        # necessary, not optional (see BUG 2 above).
        header_x = {}
        for w in all_words:
            if w[4] in HEADER_LABELS and w[4] not in header_x:
                header_x[w[4]] = w[0]
        if len(header_x) < 5:
            warnings.append(f"could not find all 5 column headers on the page "
                             f"(found: {sorted(header_x)}) — cannot reliably "
                             f"assign values to columns, refusing to guess")
            return ExtractResult(
                icao=icao, subsection=self.subsection, kind=self.kind,
                records=[], text="", embed_text="", warnings=warnings,
            )

        cols_sorted = sorted(header_x.items(), key=lambda kv: kv[1])
        col_names = [c for c, _ in cols_sorted]
        col_xs = [x for _, x in cols_sorted]
        # Boundaries for TORA/TODA/ASDA/LDA use ordinary midpoints — these
        # header positions reliably track their own data (confirmed direct:
        # DNKN's TORA/TODA/ASDA/LDA header x0s land within ~8pt of their own
        # column's actual data values). "Remarks" is different: its header
        # label can sit far right of where remarks TEXT actually starts
        # (confirmed directly on DNKN: Remarks header at x0=445, but the
        # real remarks word "NIL" renders at x0=358 — a midpoint against the
        # header position would misclassify it as LDA's own value instead).
        # Fix: derive the LDA-to-Remarks boundary from the SAME consistent
        # spacing already established between the numeric columns
        # themselves, not from the Remarks header's own (unreliable)
        # position — the numeric columns are evenly spaced, and remarks
        # text begins right after the last of them, regardless of where its
        # header is drawn.
        numeric_cols = [c for c in col_names if c != "Remarks"]
        numeric_xs = [header_x[c] for c in numeric_cols]
        avg_spacing = ((numeric_xs[-1] - numeric_xs[0]) / (len(numeric_xs) - 1)
                        if len(numeric_xs) > 1 else 60.0)
        remarks_left_boundary = numeric_xs[-1] + avg_spacing * 0.5

        boundaries = [-1e9]
        for i in range(len(col_names) - 1):
            if col_names[i + 1] == "Remarks":
                boundaries.append(remarks_left_boundary)
            else:
                boundaries.append((col_xs[i] + col_xs[i + 1]) / 2)
        boundaries.append(1e9)

        def bucket_for(x0):
            for i in range(len(col_names)):
                if boundaries[i] <= x0 < boundaries[i + 1]:
                    return col_names[i]
            return col_names[-1]

        lines = _line_groups(all_words)
        records = []
        current_buckets = None   # dict[col_name] -> list[word_text], for open row
        current_record = None

        def flush():
            if current_record is None:
                return
            for field, col in (("tora_m", "TORA"), ("toda_m", "TODA"),
                                ("asda_m", "ASDA"), ("lda_m", "LDA")):
                tokens = current_buckets.get(col, [])
                joined = _join_thousands_tokens(tokens)
                nums = re.findall(r"\d+(?:\.\d+)?", joined)
                current_record[field] = int(float(nums[0])) if nums else None
            remarks_text = self.clean_text(" ".join(current_buckets.get("Remarks", []))).strip()
            current_record["remarks"] = remarks_text or None
            records.append(current_record)

        for line in lines:
            first_word = line[0] if line else None
            line_text = " ".join(w[4] for w in line)
            m = DESIGNATOR_RE.match(line_text) if first_word else None
            # A genuine new row starts with a designator AND that designator
            # word itself sits in/near the RWY column (leftmost) — guards
            # against a stray designator-shaped token appearing elsewhere.
            is_new_row = bool(m) and first_word[0] < col_xs[0]

            if is_new_row:
                flush()
                current_record = {"icao": icao, "runway": m.group(1).upper()}
                current_buckets = {c: [] for c in col_names}
                remaining = line[1:]  # skip the designator word itself
            else:
                remaining = line

            for w in remaining:
                col = bucket_for(w[0])
                if current_buckets is not None:
                    current_buckets.setdefault(col, []).append(w[4])

        flush()

        if not records:
            warnings.append("no declared-distance rows found at all")

        embed_text = f"{icao} declared distances: " + "; ".join(
            f"{r['runway']} TORA={r['tora_m']} TODA={r['toda_m']} "
            f"ASDA={r['asda_m']} LDA={r['lda_m']}" for r in records)

        return ExtractResult(
            icao=icao, subsection=self.subsection, kind=self.kind,
            records=records, text="", embed_text=embed_text, warnings=warnings,
        )

    def validate(self, result: ExtractResult) -> list:
        issues = []
        if not result.records:
            issues.append(ValidationIssue("error", "records",
                                          f"{result.icao}: no declared-distance records"))
            return issues
        for rec in result.records:
            # null-over-guess, now correctly scoped PER FIELD rather than
            # all-or-nothing per runway (the DNKT fix): a genuinely blank
            # column is None and reported as a warning-worthy gap, but does
            # NOT block the other three genuinely-published values from
            # being stored. A record with ZERO populated fields at all is
            # still a hard error — that's a real extraction failure, not a
            # partial-publication case.
            populated = [rec[f] for f in ("tora_m", "toda_m", "asda_m", "lda_m")
                         if rec[f] is not None]
            if not populated:
                issues.append(ValidationIssue(
                    "error", "all_fields",
                    f"{result.icao} RWY{rec['runway']}: no declared distances at all"))
        return issues
