"""Offline reproducible schedule and output-shape contracts."""

import json
from collections import Counter

import pytest

from kodocs.eval import m8_stream_repro as repro

REAL_ESTIMATE = repro.estimate


def test_stage_one_crosses_modes_deterministically():
    plan = repro.stage_one()
    assert plan == repro.stage_one()
    assert len(plan) == 200
    assert set(Counter((p["question_id"], p["mode"]) for p in plan).values()) == {25}
    assert all(plan[i]["mode"] != plan[i + 1]["mode"] for i in range(0, 200, 2))


def test_runaway_boundary_and_shape_only():
    assert not repro.is_runaway(3999, False)
    assert repro.is_runaway(4000, False)
    assert repro.is_runaway(10, True)
    raw = '{"answer":"abcabcabc", "citations":[{},{}], "refused":false}'
    shape = repro.shape_statistics(raw)
    assert shape["field_chars"]["answer"] == 9
    assert shape["citations_count"] == 2
    assert shape["whitespace_ratio"] == pytest.approx(raw.count(" ") / len(raw))
    assert shape["repetition_length"] >= 3
    assert shape["repetition_count"] >= 2
    assert "abc" not in json.dumps(shape)
    assert repro.shape_statistics('{"answer":"cut')["active_field"] == "answer"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from kodocs.agent.guardrails import GuardrailConfig
    from kodocs.agent.types import AgentRuntime
    from kodocs.llm.answer_settings import load_answer_config

    model = load_answer_config("luna")
    frozen = {
        "config_hash": "fixed",
        "m5_config_hash": "m5",
        "answer": {"settings": model.record()},
        "guardrails": {
            "settings": GuardrailConfig().record(),
            "settings_hash": GuardrailConfig().settings_hash(),
        },
    }
    q = SimpleNamespace(
        id="q", question="PRIVATE QUESTION", asker=SimpleNamespace(team="a", grade=1)
    )
    plan = [
        {"question_id": "q", "system": "simple", "mode": m}
        for m in ("streaming", "nonstreaming")
    ]
    monkeypatch.setattr(
        repro, "preflight", lambda root, stage: (frozen, model, {"q": q}, plan)
    )
    monkeypatch.setattr(repro, "estimate", lambda root, stage: {"upper_cost_usd": 0.01})
    monkeypatch.setattr(
        repro, "budget", lambda *a, **k: SimpleNamespace(check=lambda cost: None)
    )
    monkeypatch.setattr(
        repro.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0)
    )
    runtime = AgentRuntime(object(), object(), {}, "m5")
    return tmp_path, runtime, plan


def test_raw_hashes_lock_history_and_no_body_in_manifest(sandbox, monkeypatch):
    from types import SimpleNamespace

    root, runtime, _plan = sandbox

    def fake_answer(*args, runtime, **kwargs):
        runtime.chat.text.append('{"answer":"PRIVATE RAW')
        runtime.chat.diagnostics = {
            "output_tokens": 4000,
            "reasoning_tokens": 0,
            "incomplete_reason": "max_output_tokens",
        }
        return SimpleNamespace(refuse_reason="parse_error")

    result = repro.run(
        root, runtime_factory=lambda model: runtime, answer_fn=fake_answer
    )
    serialized = json.dumps(result)
    assert "PRIVATE" not in serialized
    entry = result["history"][0]
    assert all(r["raw"]["sha256"] and r["raw"]["rows"] == 1 for r in entry["runs"])
    assert len(entry["runs"]) == 2 and all(r["runaway"] for r in entry["runs"])
    with pytest.raises(ValueError, match="RERUN_REASON"):
        repro.run(root, runtime_factory=lambda model: runtime, answer_fn=fake_answer)
    newer = repro.run(
        root,
        rerun_reason="test correction",
        runtime_factory=lambda model: runtime,
        answer_fn=fake_answer,
    )
    assert newer["history"][1]["previous_result_sha256"] == entry["result_sha256"]
    raw = root / newer["history"][0]["runs"][0]["raw"]["path"]
    raw.write_text("modified")
    with pytest.raises(ValueError, match="raw hash"):
        repro.read_result(root)


def test_item_failure_continues_fatal_stops_and_registers(sandbox):
    from types import SimpleNamespace

    from kodocs.llm.openai_chat import FatalLLMError

    root, runtime, _plan = sandbox
    calls = []

    def answer(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("PRIVATE")
        return SimpleNamespace(refuse_reason=None)

    result = repro.run(root, runtime_factory=lambda model: runtime, answer_fn=answer)
    assert len(calls) == 2
    assert result["history"][0]["runs"][0]["status"] == "RuntimeError"

    def fatal(*args, **kwargs):
        raise FatalLLMError("PRIVATE")

    with pytest.raises(FatalLLMError):
        repro.run(
            root,
            rerun_reason="fatal fixture",
            runtime_factory=lambda model: runtime,
            answer_fn=fatal,
        )
    result = repro.read_result(root)
    assert result["history"][-1]["status"] == "stopped"
    assert len(result["history"][-1]["runs"]) == 1
    assert not (root / repro.RAW_DIR / ".stage-1.lock").exists()


def test_live_process_lock_rejects_without_overwriting(sandbox):
    root, runtime, _ = sandbox
    lock = root / repro.RAW_DIR / ".stage-1.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("live")
    with pytest.raises(FileExistsError):
        repro.run(root, runtime_factory=lambda model: runtime)
    assert lock.read_text() == "live"
    assert not (root / repro.MANIFEST).exists()


def test_real_budget_loads_isolated_dotenv_dry_does_not(tmp_path, monkeypatch):
    from dotenv import load_dotenv

    from kodocs import config

    for name in (*config.KEY_ENV_VARS, "MONTHLY_BUDGET_USD"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / ".env"
    path.write_text("OPENAI_API_KEY=test-placeholder\nMONTHLY_BUDGET_USD=0.25\n")
    seen = []

    def isolated():
        seen.append(True)
        return load_dotenv(path)

    monkeypatch.setattr(config, "load_dotenv", isolated)
    assert repro.budget(tmp_path).monthly_budget_usd == 45
    assert seen == []
    assert repro.budget(tmp_path, execute=True).monthly_budget_usd == 0.25
    assert seen == [True]


def test_dry_run_monthly_cap_rejects(tmp_path, monkeypatch):
    from kodocs.costs import BudgetExceededError, BudgetGuard, CostLedger

    monkeypatch.setattr(
        repro,
        "estimate",
        lambda root, stage: {"upper_cost_usd": 2, "estimated_cost_usd": 1},
    )
    monkeypatch.setattr(
        repro, "budget", lambda root: BudgetGuard(CostLedger(tmp_path / "ledger"), 1.5)
    )
    with pytest.raises(BudgetExceededError):
        repro.dry_run(tmp_path)


def test_stage_two_requires_stage_one_and_config_hash(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from kodocs.llm.answer_settings import load_answer_config

    monkeypatch.setattr(
        repro,
        "verify_config",
        lambda path: SimpleNamespace(ok=False, mismatches=["config_hash"]),
    )
    with pytest.raises(ValueError, match="config mismatch"):
        repro.preflight(tmp_path, 1)
    monkeypatch.setattr(repro, "verify_config", lambda path: SimpleNamespace(ok=True))
    path = tmp_path / repro.CONFIG_PATH
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"answer": {"settings": load_answer_config("luna").record()}})
    )
    with pytest.raises(ValueError, match="stage 1 completion"):
        repro.preflight(tmp_path, 2)


def test_raw_output_path_is_ignored_and_targets_only_declared():
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", str(repro.RAW_DIR / "probe.txt")],
        cwd=root,
        check=False,
    )
    assert result.returncode == 0
    text = (root / "Makefile").read_text()
    assert "m8-stream-repro" in text.splitlines()[0].split()
    assert "python -m kodocs.eval.m8_stream_repro $(M8_RERUN_FLAGS)" in text
    assert "--dry-run $(if $(STAGE),--stage $(STAGE))" in text


def test_shape_exact_fixture_no_fragments():
    raw = '{"answer":"ababab","citations":[],"refused":false}\n'
    result = repro.shape_statistics(raw)
    assert result["repetition_length"] == 4
    assert result["repetition_count"] == 2
    assert result["newline_ratio"] == pytest.approx(1 / len(raw))
    assert result["field_chars"] == {"answer": 6, "citations": 2, "refused": 5}
    assert result["active_field"] is None


def test_estimate_counts_cost_components_once_and_uses_runaway_cost(
    sandbox, monkeypatch
):
    root, _runtime, _plan = sandbox
    rows = [
        {
            "run_id": "m7-latency",
            "request_id": key,
            "service": kind,
            "usage": {"output_tokens": tokens, "reasoning_tokens": 0},
            "latency_ms": 10,
            "amount_usd": cost,
        }
        for key, tokens, cost in [("a", 40, 0.01), ("b", 4000, 0.03)]
        for kind in ("input", "output")
    ]
    latency = root / repro.LATENCY
    latency.parent.mkdir(parents=True)
    latency.write_text(json.dumps({"warmup_excluded_count": 0}))
    monkeypatch.setattr(
        repro,
        "load_rows",
        lambda path: rows if path == root / repro.LEDGER else [{}] * 2,
    )
    monkeypatch.setattr(repro, "verify_fingerprint", lambda *args: None)
    result = REAL_ESTIMATE(root, 1)
    assert result["request_mean_usd"] == pytest.approx(0.04)
    assert result["runaway_request_cost_usd"] == pytest.approx(0.06)
    assert result["estimated_cost_usd"] == pytest.approx(0.08)
    assert result["upper_cost_usd"] == pytest.approx(0.12)


def test_report_missing_and_invalid_result_never_hides_failure(tmp_path):
    assert repro.report(tmp_path) == "재현 실행 미실행"
    path = tmp_path / repro.MANIFEST
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    with pytest.raises(ValueError, match="result hash"):
        repro.report(tmp_path)


def test_report_is_descriptive_by_mode_question_and_quotes_no_raw(sandbox):
    from types import SimpleNamespace

    root, runtime, _plan = sandbox
    repro.run(
        root,
        runtime_factory=lambda model: runtime,
        answer_fn=lambda *args, **kwargs: SimpleNamespace(refuse_reason=None),
    )
    text = repro.report(root)
    assert "재현 안 됨" in text and "가설 검정 없음" in text
    assert "q streaming: 0/1" in text
    assert "q nonstreaming: 0/1" in text
    assert "PRIVATE" not in text
