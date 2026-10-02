"""Offline reproducible schedule and output-shape contracts."""

import json
from collections import Counter

import pytest

from kodocs.eval import m8_stream_repro as repro

REAL_ESTIMATE = repro.estimate


def test_stage_one_crosses_three_paths_deterministically():
    plan = repro.stage_one()
    assert plan == repro.stage_one()
    assert len(plan) == 300  # 4 questions x 3 paths x 25
    assert set(Counter((p["question_id"], p["path"]) for p in plan).values()) == {25}
    for i in range(0, 300, 3):  # every triple crosses all three paths
        assert {p["path"] for p in plan[i : i + 3]} == {"A", "B", "C"}
        assert len({(p["question_id"], p["system"]) for p in plan[i : i + 3]}) == 1
    assert {(p["path"], p["mode"], p["schema"]) for p in plan} == {
        ("A", "nonstreaming", "sdk"),
        ("B", "streaming", "langchain"),
        ("C", "streaming", "sdk"),
    }


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
    plan = [repro._item("q", "simple", path) for path in ("A", "B")]
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


def test_report_is_descriptive_by_path_question_and_quotes_no_raw(sandbox):
    from types import SimpleNamespace

    root, runtime, _plan = sandbox
    repro.run(
        root,
        runtime_factory=lambda model: runtime,
        answer_fn=lambda *args, **kwargs: SimpleNamespace(refuse_reason=None),
    )
    text = repro.report(root)
    assert "재현 안 됨" in text and "가설 검정 없음" in text
    assert "q 경로 A: 0/1" in text
    assert "q 경로 B: 0/1" in text
    assert "B 대 C" in text and "A 대 C" in text
    assert "PRIVATE" not in text


def test_stage_two_plan_is_b_and_c_once_per_source_row(tmp_path, monkeypatch):
    """113 questions x 2 systems x {B, C}; no frozen M7 files needed here."""
    from types import SimpleNamespace

    from kodocs.llm.answer_settings import load_answer_config

    model = load_answer_config("luna")
    source = [
        {"question_id": f"q{i}", "system": system, "run_seq": 1}
        for i in range(113)
        for system in ("simple", "agentic")
    ]
    config = tmp_path / repro.CONFIG_PATH
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"answer": {"settings": model.record()}}))
    done = {
        "stage": 1,
        "status": "completed",
        "runs": [{"runaway": False}],
        "previous_result_sha256": None,
    }
    done["result_sha256"] = repro.canonical_hash(done)
    result = {"schema_version": 1, "history": [done]}
    result["result_sha256"] = repro.canonical_hash(result)
    (tmp_path / repro.MANIFEST).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / repro.MANIFEST).write_text(json.dumps(result))
    latency = tmp_path / repro.LATENCY
    latency.parent.mkdir(parents=True, exist_ok=True)
    latency.write_text(json.dumps({"history": [{"seq": 1}]}))
    monkeypatch.setattr(repro, "verify_config", lambda path: SimpleNamespace(ok=True))
    monkeypatch.setattr(repro, "verify_fingerprint", lambda *args: None)
    monkeypatch.setattr(repro, "load_rows", lambda path: source)
    monkeypatch.setattr(
        repro,
        "load_m6_questions",
        lambda root, split: [SimpleNamespace(id=f"q{i}") for i in range(113)],
    )
    _frozen, _model, _questions, plan = repro.preflight(tmp_path, 2)
    assert len(plan) == 452
    assert Counter(p["path"] for p in plan) == {"B": 226, "C": 226}
    assert all(p["mode"] == "streaming" for p in plan)
    for i in range(0, 452, 2):
        assert {plan[i]["path"], plan[i + 1]["path"]} == {"B", "C"}
        assert plan[i]["question_id"] == plan[i + 1]["question_id"]


class _Inner:
    def __init__(self, fail_with=None):
        self.calls, self.fail_with = [], fail_with

    def invoke(self, messages, **kwargs):
        self.calls.append(kwargs)
        if self.fail_with:
            raise self.fail_with
        raise AssertionError("unused")


def test_capture_chat_adds_nothing_for_path_a_and_library_schema_for_b_and_c():
    from kodocs.agent.types import ModelAnswer
    from kodocs.llm.openai_chat import ChatParseError

    for path, expected in {
        "A": {"response_schema": ModelAnswer},
        "B": {
            "response_schema": ModelAnswer,
            "stream": True,
            "schema_style": "langchain",
        },
        "C": {"response_schema": ModelAnswer, "stream": True, "schema_style": "sdk"},
    }.items():
        inner = _Inner(ChatParseError("x"))
        capture = repro.CaptureChat(inner, path)
        with pytest.raises(ChatParseError):
            capture.invoke("m", response_schema=ModelAnswer)
        call = inner.calls[0]
        call.pop("on_text_delta", None)
        assert call == expected, path
        assert "on_text_delta" not in expected  # A must keep the frozen request
    # Non-final (tool selection) calls never stream.
    inner = _Inner(RuntimeError("stop"))
    with pytest.raises(RuntimeError):
        repro.CaptureChat(inner, "C").invoke("m", tools=["t"])
    assert inner.calls[0] == {"tools": ["t"]}


def test_recording_transport_keeps_only_non_streaming_bodies_in_memory():
    import httpx

    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["stream"])
        return httpx.Response(200, json={"id": "r"})

    recorder = repro.RecordingTransport(httpx.MockTransport(handler))
    with httpx.Client(transport=recorder) as client:
        client.post("https://example.invalid/v1/responses", json={"stream": True})
        assert recorder.last_body is None  # streaming body is never read
        client.post("https://example.invalid/v1/responses", json={"stream": False})
    assert seen == [True, False]
    assert json.loads(recorder.last_body) == {"id": "r"}


def test_parse_recorded_body_is_content_free_in_diagnostics():
    body = json.dumps(
        {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": '{"answer":"PRIVATE'}],
                }
            ],
            "usage": {
                "output_tokens": 4000,
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }
    )
    parsed = repro.parse_recorded_body(body)
    assert parsed["text"] == '{"answer":"PRIVATE'
    assert parsed["diagnostics"] == {
        "status": "incomplete",
        "incomplete_reason": "max_output_tokens",
        "output_tokens": 4000,
        "reasoning_tokens": 0,
        "output_chars": len('{"answer":"PRIVATE'),
    }
    assert "PRIVATE" not in json.dumps(parsed["diagnostics"])
    assert repro.parse_recorded_body("not json") is None
    assert repro.parse_recorded_body(None) is None


def test_path_a_failure_recovers_raw_and_usage_from_the_recorded_body():
    from types import SimpleNamespace

    from kodocs.agent.types import ModelAnswer
    from kodocs.llm.openai_chat import ChatParseError

    body = json.dumps(
        {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [
                {"content": [{"type": "output_text", "text": '{"answer":"cut'}]}
            ],
            "usage": {
                "output_tokens": 4000,
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }
    )
    recorder = SimpleNamespace(last_body="stale body of an earlier request")

    class Failing(_Inner):
        def invoke(self, messages, **kwargs):
            recorder.last_body = body  # the transport sees the response mid-call
            return super().invoke(messages, **kwargs)

    # The frozen path raises a bare ChatParseError (no diagnostics, no message).
    capture = repro.CaptureChat(Failing(ChatParseError("x")), "A", recorder)
    with pytest.raises(ChatParseError):
        capture.invoke("m", response_schema=ModelAnswer)
    assert "".join(capture.text) == '{"answer":"cut'
    assert capture.diagnostics["output_tokens"] == 4000
    assert capture.diagnostics["incomplete_reason"] == "max_output_tokens"
