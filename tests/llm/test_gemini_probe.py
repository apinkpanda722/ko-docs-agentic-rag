"""Probe is at most three user-triggered calls; dry-run stays offline."""

import json

from kodocs.config import Settings
from kodocs.llm import gemini_probe
from kodocs.llm.gemini_chat import FatalLLMError


def test_dry_run_never_constructs_client_or_reads_env(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        gemini_probe,
        "create_client",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")),
    )
    assert gemini_probe.main(["--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert len(plan["calls"]) == 3
    assert plan["maximum_cost_usd"] > 0
    assert not (tmp_path / "data").exists()


def test_probe_records_safe_metadata_and_stops_after_fatal(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        gemini_probe,
        "load_settings",
        lambda **kwargs: Settings({"GEMINI_API_KEY": "secret"}, 45),
    )
    monkeypatch.setattr(gemini_probe, "create_client", lambda *args: object())
    calls = []

    def fail(self, request):
        calls.append(request)
        raise FatalLLMError("safe", status_code=401)

    monkeypatch.setattr(gemini_probe.GeminiGrader, "grade", fail)
    assert gemini_probe.main([]) == 1
    record = json.loads((tmp_path / "data/manifest/m7_probe.json").read_text())
    assert len(calls) == 1
    assert record["calls"][0]["status_code"] == 401
    assert "secret" not in json.dumps(record)


def test_probe_three_calls_record_usage_and_only_integer_scores(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from kodocs.llm.gemini_chat import GradeUsage

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        gemini_probe,
        "load_settings",
        lambda **kwargs: Settings({"GEMINI_API_KEY": "secret"}, 45),
    )
    monkeypatch.setattr(gemini_probe, "create_client", lambda *args: object())
    calls = []

    def succeed(self, request):
        calls.append(self.config.record())
        return SimpleNamespace(
            parsed=gemini_probe.ProbeScore(accuracy=3, faithfulness=3),
            usage=GradeUsage(100, 50, 20, 30, 150),
            model_record={"model_version": "reported-version"},
        )

    monkeypatch.setattr(gemini_probe.GeminiGrader, "grade", succeed)
    assert gemini_probe.main([]) == 0
    record = json.loads((tmp_path / "data/manifest/m7_probe.json").read_text())
    assert len(calls) == 3
    assert all(entry["thinking_tokens_used"] for entry in record["calls"])
    assert all(
        entry["scores"] == {"accuracy": 3, "faithfulness": 3}
        for entry in record["calls"]
    )
    assert calls[0]["temperature"] is None
    assert calls[1]["temperature"] == 0
    assert calls[2]["seed"] == 20261001
    assert "Reference:" not in json.dumps(record)
