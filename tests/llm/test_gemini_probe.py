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
            parsed=gemini_probe.ProbeScore(
                accuracy=3, faithfulness=3, rationale="동일 사유"
            ),
            request_id=f"resp-{len(calls)}",
            usage=GradeUsage(100, 50, 20, 30, 150),
            model_record={"model_version": "reported-version"},
        )

    monkeypatch.setattr(gemini_probe.GeminiGrader, "grade", succeed)
    assert gemini_probe.main([]) == 0
    record = json.loads((tmp_path / "data/manifest/m7_probe.json").read_text())
    assert [c["name"] for c in record["calls"]] == [
        "baseline",
        "minimal_check",
        "repeat",
    ]
    assert len(calls) == 3
    assert all(entry["thinking_tokens_used"] for entry in record["calls"])
    assert all(entry["response_id"] for entry in record["calls"])
    # thinking low for baseline/repeat, minimal only for the support check;
    # temperature and seed are never sent in any call.
    assert [c["thinking_level"] for c in calls] == ["low", "minimal", "low"]
    assert all(c["temperature"] is None and c["seed"] is None for c in calls)
    assert record["minimal_check"] == {
        "ran": True,
        "supported": True,
        "error_type": None,
        "status_code": None,
        "sdk_error_type": None,
    }
    assert record["repeat_comparison"] == {
        "compared": True,
        "scores_equal": True,
        "rationale_equal": True,
    }
    confirmed = record["confirmed_settings"]
    assert (
        confirmed["thinking_level"],
        confirmed["temperature"],
        confirmed["seed"],
    ) == (
        "low",
        None,
        None,
    )
    assert len(confirmed["basis"]) == 2 and confirmed["checked_on"] == "2026-10-01"
    assert "Reference:" not in json.dumps(record)


def test_probe_minimal_rejection_is_recorded_without_retry_and_does_not_stop(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from kodocs.llm.gemini_chat import GradeUsage

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        gemini_probe,
        "load_settings",
        lambda **kwargs: Settings({"GEMINI_API_KEY": "secret"}, 45),
    )
    monkeypatch.setattr(gemini_probe, "create_client", lambda *args: object())
    seen = []

    def grade(self, request):
        seen.append(self.config.thinking_level)
        if self.config.thinking_level == "minimal":
            raise FatalLLMError("safe", status_code=400)
        return SimpleNamespace(
            parsed=gemini_probe.ProbeScore(
                accuracy=3, faithfulness=3, rationale=f"사유{len(seen)}"
            ),
            usage=GradeUsage(100, 50, 20, 30, 150),
            model_record={"model_version": "v"},
            request_id=f"resp-{len(seen)}",
        )

    monkeypatch.setattr(gemini_probe.GeminiGrader, "grade", grade)
    assert gemini_probe.main([]) == 0
    record = json.loads((tmp_path / "data/manifest/m7_probe.json").read_text())
    assert seen == ["low", "minimal", "low"]  # no retry, repeat still runs
    assert record["minimal_check"]["supported"] is False
    assert record["minimal_check"]["status_code"] == 400
    # Repeat comparison reports differing rationale text for the same input.
    assert record["repeat_comparison"]["scores_equal"] is True
    assert record["repeat_comparison"]["rationale_equal"] is False
    assert record["confirmed_settings"]["thinking_level"] == "low"
