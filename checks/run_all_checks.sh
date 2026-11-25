#!/usr/bin/env bash
# Always run from the repo folder: the checks open their data files there.
cd "$(dirname "$0")/.." || exit 1
# --quick (added by add_quick_flag.py)
if [ "${1:-}" = "--quick" ]; then
  shift
  env -u FACTS_ENABLED pytest -q || exit $?
  ${PYTHON:-python} checks/quick_check.py "$@"
  exit $?
fi
# run_all_checks.sh — every pending measurement, in one go, outputs saved.
#
#   cd /Users/Obiora/Desktop/nigeria_aip_bot
#   bash checks/run_all_checks.sh            # everything (~30-45 min; cost per step in cost.txt)
#   bash checks/run_all_checks.sh quick      # skip the two full stress runs (~12 min)
#
# Writes checks_<date>/ with one file per check; send me the whole folder
# (zip it: zip -r checks.zip checks_<date>).
# Nothing here changes code or data, or posts to Telegram.
set -u
MODE="${1:-all}"
OUT="checks_$(date +%Y%m%d_%H%M)"
mkdir -p "$OUT"
log() { echo "[$(date +%H:%M:%S)] $*"; }

# WHAT WAS MEASURED, ON WHAT (2026-11-25): commit, models and prompt
# fingerprint go in run_info.txt, and every paid step runs under
# checks/metered.py, which counts its OpenAI tokens into cost.jsonl; the table
# is printed into cost.txt at the end. A full run had cost about $5 with no
# record of which step spent it.
export VANNIE_METER_OUT="$OUT/cost.jsonl"
M="python checks/metered.py"
{
  echo "date:     $(date '+%Y-%m-%d %H:%M %Z')"
  echo "commit:   $(git rev-parse --short HEAD 2>/dev/null)$(git diff --quiet 2>/dev/null || echo ' (+ uncommitted changes)')"
  python - <<'PY' 2>/dev/null
import config, observability
print(f"models:   extraction={config.EXTRACTION_MODEL} [{config.reasoning_effort_for(config.EXTRACTION_MODEL, 'extraction')}]"
      f"  synthesis={config.SYNTHESIS_MODEL} [{config.reasoning_effort_for(config.SYNTHESIS_MODEL, 'synthesis')}]")
print(f"prompts:  {observability.prompt_version()}")
PY
} > "$OUT/run_info.txt"
cat "$OUT/run_info.txt"

# Production runs with the facts store on; measure the same way.
# (prompts_v2 was measured worse on every check on 2026-10-21 and is no longer run.)
export FACTS_ENABLED=1

if [ ! -f stress_questions.csv ]; then
  log "stress_questions.csv missing — making it from the stress set questions"
  cp stress_results.csv stress_questions.csv 2>/dev/null || true
fi

log "1/7 unit tests"
# The unit tests check the defaults, so they run WITHOUT the production switch
# (with it set, test_facts_path_is_off_by_default fails by design).
env -u FACTS_ENABLED pytest -q > "$OUT/01_pytest.txt" 2>&1

log "2/7 approach procedures, every aerodrome (read-only, no model calls)"
python checks/check_procedures_all.py > "$OUT/02_procedures_all.txt" 2>&1

log "3/7 approach routing, current prompt (v1)"
$M 3_approach checks/check_approach_routing.py --repeat 2 > "$OUT/03_approach_v1.txt" 2>&1

log "4/7 ambiguity — asks when it should, and only then (v1)"
$M 4_ambiguity checks/check_ambiguity.py --repeat 2 --stress stress_questions.csv > "$OUT/04_ambiguity_v1.txt" 2>&1

log "5/7 live vs published classification (v1)"
$M 5_intent checks/measure_intent.py stress_set_clean.csv --limit 300 --tag "$OUT/p1" > "$OUT/05_intent_v1.txt" 2>&1

log "6/7 AD 2 section choice (v1)"
$M 6_subsection checks/check_subsection.py stress_questions.csv --tag "$OUT/sub_v1" --repeat 2 > "$OUT/06_subsection_v1.txt" 2>&1

if [ "$MODE" != "quick" ]; then
  log "7/7 full stress set, real request path — the long one"
  $M 7_stress checks/run_stress_set.py stress_set_clean.csv --limit 300 > "$OUT/07_stress_v1.txt" 2>&1
  cp stress_results.csv "$OUT/stress_v1.csv"
else
  log "7/7 skipped (quick mode)"
fi

log "8 gold set — answers copied from the AIP PDF, safety gate on verified rows"
$M 8_gold checks/run_gold.py gold_set.csv --out "$OUT/gold" > "$OUT/08_gold.txt" 2>&1 \
  || log "   GOLD SAFETY GATE FAILED — see $OUT/08_gold.txt"
tail -12 "$OUT/08_gold.txt"

[ -f "$OUT/cost.jsonl" ] && python checks/metered.py --report "$OUT/cost.jsonl" > "$OUT/cost.txt" 2>&1 && cat "$OUT/cost.txt"
log "done — zip and send:  zip -r $OUT.zip $OUT"
