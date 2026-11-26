"""rebuild_airac.sh runs its stages in order and refuses to write unless the
same PDF and cycle passed `check`. A stand-in `python` records each call and
fails on request; no real script, PDF or database is touched."""
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FAKE_PY = """#!/usr/bin/env bash
if [ "$1" = "-c" ]; then
  case "$2" in *AIRAC_CYCLE*) echo "$FAKE_CYCLE" ;; *) echo "$FAKE_PDF" ;; esac; exit 0
fi
echo "$*" >> "$FAKE_LOG"
if [ -n "${FAKE_FAIL:-}" ]; then case "$*" in *"$FAKE_FAIL"*) echo "FAIL  2 boundaries"; exit 1 ;; esac; fi
case "$1" in
  *airac_check.py) echo "PASS  1 amendment" ;;
  *refresh_structured.py) printf 'same 72  CHANGED 0  count 0  skipped 0  text-only 72\\nDry run.\\n' ;;
esac
exit 0
"""

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


@pytest.fixture
def run(tmp_path):
    work = tmp_path / "repo"
    (work / "ingestion" / "validate").mkdir(parents=True)
    (work / "main.py").write_text("")
    for v in ("validate_ad21.py", "validate_ad22.py"):
        (work / "ingestion" / "validate" / v).write_text("")
    shutil.copy(ROOT / "rebuild_airac.sh", work)
    py = tmp_path / "fakepy"
    py.write_text(FAKE_PY)
    py.chmod(0o755)
    pdf = tmp_path / "AIP.pdf"
    pdf.write_bytes(b"%PDF one")
    log = tmp_path / "calls.log"

    def _run(stage, **env):
        e = dict(os.environ, PYTHON=str(py), FAKE_PDF=str(pdf), FAKE_LOG=str(log),
                 FAKE_CYCLE="AIRAC AMDT 03/2026", FAKE_FAIL="")
        e.update(env)
        log.write_text("")
        p = subprocess.run(["bash", "rebuild_airac.sh", stage], cwd=work, env=e,
                           capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout + p.stderr, log.read_text().splitlines()
    _run.pdf = pdf
    return _run


def test_check_then_apply_runs_every_step_in_order(run):
    code, out, calls = run("check")
    assert code == 0, out
    assert [c.split()[0].split("/")[-1] for c in calls] == [
        "airac_check.py", "validate_ad21.py", "validate_ad22.py", "refresh_structured.py"]
    code, out, calls = run("apply")
    assert code == 0, out
    assert calls[1] == "ingestion/vectorise_aip_v3.py --yes"
    assert calls[2] == "ingestion/build_fact_index.py --force"
    assert [c.split()[2] for c in calls[3:]] == ["5.1", "4.4", "2.1", "4.1", "3.1", "3.2", "3.3"]
    assert "AIRAC_CYCLE on Render" in out


def test_a_failed_check_blocks_apply(run):
    code, out, _ = run("check", FAKE_FAIL="validate_ad22")
    assert code == 1 and "validate_ad22" in out
    code, out, calls = run("apply")
    assert code == 1 and "have not passed" in out and calls == []


def test_apply_refuses_a_different_pdf_or_cycle_than_was_checked(run):
    assert run("check")[0] == 0
    code, out, calls = run("apply", FAKE_CYCLE="AIRAC AMDT 01/2027")
    assert code == 1 and calls == []
    run.pdf.write_bytes(b"%PDF a different, longer file")
    code, out, calls = run("apply")
    assert code == 1 and calls == []


def test_an_incomplete_ad2_ingest_stops_before_the_facts(run):
    assert run("check")[0] == 0
    code, out, calls = run("apply", FAKE_FAIL="vectorise_aip_v3")
    assert code == 1 and "INCOMPLETE" in out
    assert not any("build_fact_index" in c for c in calls)
