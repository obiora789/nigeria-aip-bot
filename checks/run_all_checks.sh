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
#   bash checks/run_all_checks.sh            # everything (~30-45 min, about a dollar)
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
python checks/check_approach_routing.py --repeat 2 > "$OUT/03_approach_v1.txt" 2>&1

log "4/7 ambiguity — asks when it should, and only then (v1)"
python checks/check_ambiguity.py --repeat 2 --stress stress_questions.csv > "$OUT/04_ambiguity_v1.txt" 2>&1

log "5/7 live vs published classification (v1)"
python checks/measure_intent.py stress_set_clean.csv --limit 300 --tag "$OUT/p1" > "$OUT/05_intent_v1.txt" 2>&1

log "6/7 AD 2 section choice (v1)"
python checks/check_subsection.py stress_questions.csv --tag "$OUT/sub_v1" --repeat 2 > "$OUT/06_subsection_v1.txt" 2>&1

if [ "$MODE" != "quick" ]; then
  log "7/7 full stress set, real request path — the long one"
  python checks/run_stress_set.py stress_set_clean.csv --limit 300 > "$OUT/07_stress_v1.txt" 2>&1
  cp stress_results.csv "$OUT/stress_v1.csv"
else
  log "7/7 skipped (quick mode)"
fi

log "done — zip and send:  zip -r $OUT.zip $OUT"
