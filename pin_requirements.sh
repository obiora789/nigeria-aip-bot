#!/usr/bin/env bash
# pin_requirements.sh — regenerate requirements.txt with exact versions from
# requirements.in, then check them for known vulnerabilities.
#
#   cd /Users/Obiora/Desktop/nigeria_aip_bot
#   source venv/bin/activate          # Python 3.12, same as Render
#   bash pin_requirements.sh
#
# Commit requirements.in AND requirements.txt together.
set -euo pipefail
python -m pip install -q --upgrade pip-tools pip-audit
pip-compile --quiet --strip-extras --no-emit-index-url \
    --output-file requirements.txt requirements.in
echo "requirements.txt pinned:"
grep -c "==" requirements.txt | xargs echo "  packages:"
echo "Checking for known vulnerabilities (pip-audit)..."
pip-audit -r requirements.txt --progress-spinner off
