#!/usr/bin/env bash
# rebuild_airac.sh — re-ingest the AIP (a new AIRAC amendment, or an extractor
# change) in a fixed, safe order. Car 3.
#
#   bash rebuild_airac.sh check   free  — is this PDF safe to ingest? what changed?
#   bash rebuild_airac.sh apply   ~cents of embeddings — writes, in place
#   bash rebuild_airac.sh gold    ~the cost of a gold run — measures the result
#
# Run from the repo folder. The PDF is PDF_PATH from .env (or export it).
# Each stage writes its logs to airac_runs/<date>_<stage>/.
#
# check
#   1 ingestion/airac_check.py — amendment vs AIRAC_CYCLE, aerodrome page
#     ranges, AD 2.22 spans, ENR 4.1 navaids vs the prompt list, gold rows
#     still on their pages. Must pass. For a NEW amendment that means: set
#     AIRAC_CYCLE in .env to the new one, update aip_structure.AERODROMES /
#     _span22.json / agent.ENR_NAVAIDS / the listed gold rows where it says so.
#   2 every ingestion/validate/validate_ad2*.py on the PDF — must pass.
#   3 ingestion/refresh_structured.py, all AD 2 subsections, DRY RUN — the
#     value-by-value difference from what is stored. Read it: on a new
#     amendment it is the list of changes Vannie is about to serve.
#   When 1 and 2 pass, check records this PDF as checked; apply needs that.
#
# apply
#   1 airac_check again (10 s) — the PDF must be the one that passed check.
#   2 ingestion/vectorise_aip_v3.py --yes — AD 2 chunks + structured records,
#     replaced IN PLACE per subsection (new rows first, then old ones removed).
#     Never delete anything first. Fails if any subsection is STALE.
#   3 ingestion/build_fact_index.py --force, then --enr for each ENR section
#     — facts rewritten, facts the new data no longer has removed.
#   Not yet covered (printed at the end): GEN/ENR/AD 1/AD 3 page chunks
#   (vectorise_aip_v2.py), charts (extract_charts.py), Render's AIRAC_CYCLE.
#
# gold
#   checks/run_gold.py on the full gold set. Exit 1 if the safety gate fails.
set -euo pipefail

STAGE="${1:-}"
case "$STAGE" in check|apply|gold) ;; *)
  echo "usage: bash rebuild_airac.sh check|apply|gold" >&2; exit 2 ;;
esac
[ -f main.py ] && [ -d ingestion ] || { echo "run this from the repo folder" >&2; exit 2; }

PY="${PYTHON:-python}"
# PDF_PATH and AIRAC_CYCLE as the Python scripts see them (config.py loads .env).
PDF="$("$PY" -c 'import os, config; print(os.path.expanduser(os.getenv("PDF_PATH") or ""))')"
CYCLE="$("$PY" -c 'import config; print(config.AIRAC_CYCLE)')"
[ -n "$PDF" ] && [ -f "$PDF" ] || { echo "PDF not found: set PDF_PATH in .env" >&2; exit 2; }
export PDF_PATH="$PDF"

# Which PDF, which cycle: check writes it, apply compares.
SIZE="$(wc -c < "$PDF" | tr -d ' ')"
STAMP="$CYCLE | $(basename "$PDF") | $SIZE bytes"
OK_FILE="airac_runs/.check_passed"

OUT="airac_runs/$(date +%Y%m%d_%H%M)_$STAGE"
mkdir -p "$OUT"
echo "$STAGE: $STAMP"
echo "logs: $OUT/"
echo

case "$STAGE" in
check)
  rm -f "$OK_FILE"
  status=0
  echo "1/3 airac_check"
  if "$PY" ingestion/airac_check.py --pdf "$PDF" > "$OUT/airac_check.txt" 2>&1; then
    echo "    PASS"
  else
    echo "    FAIL — see $OUT/airac_check.txt"; status=1
  fi
  grep -E '^(PASS|FAIL) ' "$OUT/airac_check.txt" | sed 's/^/      /' || true

  echo "2/3 AD 2 validators"
  failed=""
  for v in ingestion/validate/validate_ad2*.py; do
    name="$(basename "$v" .py)"
    if "$PY" "$v" "$PDF" > "$OUT/$name.txt" 2>&1; then :; else failed="$failed $name"; fi
  done
  if [ -z "$failed" ]; then echo "    PASS (all)"; else
    echo "    FAIL:$failed — logs in $OUT/"; status=1; fi

  echo "3/3 stored vs this PDF, all AD 2 subsections (dry run, ~10 min)"
  "$PY" ingestion/refresh_structured.py 2.1 2.2 2.3 2.4 2.5 2.6 2.7 2.8 2.9 2.10 \
      2.11 2.12 2.13 2.14 2.15 2.16 2.17 2.18 2.19 2.20 2.21 2.23 \
      --pdf "$PDF" > "$OUT/structured_diff.txt" 2>&1 || true
  tail -2 "$OUT/structured_diff.txt" | head -1 | sed 's/^/    /'
  echo "    every CHANGED/count line in $OUT/structured_diff.txt is a value Vannie will change"

  echo
  if [ "$status" -eq 0 ]; then
    echo "$STAMP" > "$OK_FILE"
    echo "Check passed. Read $OUT/structured_diff.txt, then: bash rebuild_airac.sh apply"
  else
    echo "Check failed — nothing may be written until it passes."
  fi
  exit "$status"
  ;;

apply)
  if [ ! -f "$OK_FILE" ] || [ "$(cat "$OK_FILE")" != "$STAMP" ]; then
    echo "This PDF and cycle have not passed 'bash rebuild_airac.sh check'." >&2
    echo "  checked: $(cat "$OK_FILE" 2>/dev/null || echo nothing)" >&2
    echo "  now:     $STAMP" >&2
    exit 1
  fi
  echo "1/3 airac_check (gate)"
  "$PY" ingestion/airac_check.py --pdf "$PDF" > "$OUT/airac_check.txt" 2>&1 \
    || { echo "    FAIL — see $OUT/airac_check.txt. Nothing written." >&2; exit 1; }
  echo "    PASS"

  echo "2/3 AD 2 chunks and structured records, in place (~15 min)"
  if "$PY" ingestion/vectorise_aip_v3.py --yes > "$OUT/vectorise_v3.txt" 2>&1; then
    echo "    done"
  else
    echo "    INCOMPLETE — see $OUT/vectorise_v3.txt (STALE lines, KB/struct errors)."
    echo "    Re-running apply resumes; fix STALE subsections before continuing." >&2
    exit 1
  fi
  grep -c "stale chunk\|stale record" "$OUT/vectorise_v3.txt" | sed 's/^/    subsections with old rows replaced: /' || true

  echo "3/3 facts: AD (all 36), then ENR"
  "$PY" ingestion/build_fact_index.py --force > "$OUT/facts_ad.txt" 2>&1 \
    || { echo "    AD facts INCOMPLETE — see $OUT/facts_ad.txt" >&2; exit 1; }
  for s in 5.1 4.4 2.1 4.1 3.1 3.2 3.3; do
    "$PY" ingestion/build_fact_index.py --enr "$s" --pdf "$PDF" > "$OUT/facts_enr_$s.txt" 2>&1 \
      || { echo "    ENR $s facts INCOMPLETE — see $OUT/facts_enr_$s.txt" >&2; exit 1; }
  done
  echo "    done"
  echo
  echo "Applied. Not covered by this script yet — do by hand if this AIRAC changed them:"
  echo "  - GEN / ENR / AD 1 / AD 3 page chunks (vectorise_aip_v2.py)"
  echo "  - charts (ingestion/extract_charts.py)"
  echo "  - AIRAC_CYCLE on Render: set it to '$CYCLE'"
  echo "Then measure: bash rebuild_airac.sh gold"
  ;;

gold)
  echo "full gold run"
  set +e
  "$PY" checks/run_gold.py --out "$OUT/gold" 2>&1 | tee "$OUT/gold.txt" | tail -12
  status=${PIPESTATUS[0]}
  set -e
  exit "$status"
  ;;
esac
