"""Car 5 — the model's fields decide routing, not the wording (audit
principle 3). Each test failed on the code before its step."""
import inspect
import re

import main
from schemas import AIPQueryExtraction


def _process_src():
    return inspect.getsource(main.process)


def test_d18_enroute_and_sar_are_model_fields():
    fields = AIPQueryExtraction.model_fields
    assert fields["enroute_chart_request"].default is False
    assert fields["sar_request"].default is False
    # The trap words are named in the descriptions, as data the model reads.
    assert "firefighting" in fields["enroute_chart_request"].description
    assert "RESCUE AND FIRE FIGHTING" in fields["sar_request"].description


def test_d18_no_substring_decides_fir_or_sar_charts():
    src = _process_src()
    assert '"fir" in ql' not in src and '"en-route" in ql' not in src
    assert not re.search(r"\\\\brescue\\\\b", src)
    assert src.count('getattr(ex, "enroute_chart_request", False)') == 2
    assert 'is_sar = bool(getattr(ex, "sar_request", False))' in src


def test_d18_check_scripts_mirror_the_bot():
    import pathlib
    root = pathlib.Path(main.__file__).parent
    for name in ("checks/harness.py", "checks/e2e.py"):
        text = (root / name).read_text()
        assert '"fir" in ql' not in text, name
        assert "enroute_chart_request" in text, name
