"""checks/metered.py counts what a check spends. No network: fake clients."""
import json


class _Details:
    cached_tokens = 800


class _Usage:
    prompt_tokens = 1000
    completion_tokens = 100
    prompt_tokens_details = _Details()


class _Resp:
    usage = _Usage()


class _Completions:
    def parse(self, **kw):
        return _Resp()


class AIPQueryExtraction:      # same name as the bot's schema: counted as extraction
    pass


def test_the_meter_counts_tokens_by_kind_and_prices_them(tmp_path, monkeypatch, capsys):
    import metered
    metered._usage.clear()
    comp = _Completions()
    metered._wrap(comp, "parse", "judge")
    metered._wrap(comp, "parse", "judge")          # wrapping twice must not double-count
    comp.parse(model="gpt-6-luna", response_format=AIPQueryExtraction)
    comp.parse(model="gpt-6-luna", response_format=AIPQueryExtraction)
    comp.parse(model="gpt-6-luna", response_format=dict)

    ex = metered._usage["extraction"]
    assert (ex["calls"], ex["prompt"], ex["cached"], ex["completion"]) == (2, 2000, 1600, 200)
    assert metered._usage["dict"]["calls"] == 1
    # 400 uncached at $0.10, 1,600 cached at $0.01, 200 output at $0.50 per million
    assert abs(metered._usd(ex) - (400 * 0.10 + 1600 * 0.01 + 200 * 0.50) / 1e6) < 1e-12

    out = tmp_path / "cost.jsonl"
    monkeypatch.setenv("VANNIE_METER_OUT", str(out))
    metered._save("6_subsection")
    assert json.loads(out.read_text())["step"] == "6_subsection"
    metered.report(str(out))
    table = capsys.readouterr().out
    assert "6_subsection" in table and "extraction" in table and "total" in table
    metered._usage.clear()


def test_an_unknown_model_is_reported_without_a_price():
    import metered
    assert metered._usd({"model": "some-new-model", "prompt": 1, "cached": 0,
                         "completion": 1, "calls": 1}) is None
