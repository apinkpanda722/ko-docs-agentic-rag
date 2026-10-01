"""Tests for secret-safe, append-only answer traces."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kodocs.agent.finalize import NO_ANSWER_TEXT
from kodocs.agent.guardrails import GuardrailConfig
from kodocs.agent.trace import TraceRecorder
from kodocs.agent.types import AnswerResult, Citation
from kodocs.tools import ToolResult, UserContext


def _recorder(tmp_path: Path) -> TraceRecorder:
    return TraceRecorder(
        tmp_path,
        now=lambda: datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC),
    )


def _session(recorder: TraceRecorder, question_id: str = "q-1"):
    return recorder.start(
        question_id=question_id,
        system="simple",
        user=UserContext("platform", 3),
        model_record={
            "model": "gpt-fake",
            "effort": "low",
            "temperature": None,
        },
        hashes={
            "common_prompt": "a" * 64,
            "agentic_prompt": "b" * 64,
            "guardrail_settings": GuardrailConfig().settings_hash(),
            "m5_config_hash": "455f",
        },
    )


def _success() -> AnswerResult:
    return AnswerResult(
        answer="답",
        citations=(Citation(kind="doc", doc_id="d1", section_key="s1"),),
        refused=False,
        refuse_reason=None,
        trace={"dropped_citations": []},
    )


def test_trace_has_required_fields_and_is_exactly_one_json_line(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    session = _session(recorder)
    session.add_model_step(
        "final_answer",
        latency_ms=12.5,
        tokens={
            "input": 10,
            "cached_input": 2,
            "cache_write": 1,
            "output": 4,
            "reasoning": 3,
        },
        cost_usd=0.01,
        ttft_ms=4.0,
    )
    session.add_tool_step(
        "search_docs",
        {"query": "질문"},
        ToolResult(
            payload={"results": [{"text": "원문은 기록하지 않는다"}]},
            is_error=False,
            refs=({"kind": "doc", "doc_id": "d1", "section_key": "s1"},),
            timings={"embed_ms": 1.0, "search_ms": 2.0},
        ),
        latency_ms=3.5,
    )
    path = session.finish(_success(), guardrail={"steps_used": 1, "tokens_used": 20})

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    trace = json.loads(lines[0])
    assert set(trace) == {
        "schema_version",
        "trace_id",
        "question_id",
        "system",
        "user",
        "model",
        "hashes",
        "steps",
        "timings",
        "citations",
        "dropped_citations",
        "refused",
        "refuse_reason",
        "guardrail",
        "status",
        "error",
    }
    assert trace["user"] == {"team": "platform", "grade": 3}
    assert set(trace["model"]) == {"id", "settings", "settings_hash"}
    assert set(trace["timings"]) == {
        "embed_ms",
        "search_ms",
        "ttft_ms",
        "total_ms",
    }
    tool_step = trace["steps"][1]
    assert tool_step["refs"] == [{"kind": "doc", "doc_id": "d1", "section_key": "s1"}]
    assert tool_step["result_chars"] > 0
    assert "payload" not in tool_step
    assert "원문은 기록하지 않는다" not in lines[0]


def test_secret_named_values_are_redacted_from_every_recorded_input(
    tmp_path: Path,
) -> None:
    secret = "sk-fake-do-not-record"
    session = _recorder(tmp_path).start(
        question_id="q-secret",
        system="agentic",
        user=UserContext("security", 4),
        model_record={"model": "fake", "api_key": secret},
        hashes={"common_prompt": "a", "authorization": f"Bearer {secret}"},
    )
    session.add_tool_step(
        "search_docs",
        {"query": "질문", "token": secret, "Authorization": f"Bearer {secret}"},
        ToolResult(payload={}, is_error=False, refs=(), timings=None),
        latency_ms=1,
    )
    path = session.finish(
        AnswerResult(NO_ANSWER_TEXT, (), True, "no_valid_citation", {})
    )

    contents = path.read_text(encoding="utf-8")
    assert secret not in contents
    assert "Bearer" not in contents


def test_concurrent_records_never_mix_json_lines(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)

    def write(index: int) -> None:
        _session(recorder, f"q-{index}").finish(_success())

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(40)))

    path = tmp_path / "2026-09-30.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    parsed = [json.loads(line) for line in lines]
    assert len(parsed) == 40
    assert {item["question_id"] for item in parsed} == {
        f"q-{index}" for index in range(40)
    }


def test_context_manager_records_exception_type_then_reraises(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)

    with (
        pytest.raises(RuntimeError, match="sensitive failure detail"),
        _session(recorder, "q-error"),
    ):
        raise RuntimeError("sensitive failure detail")

    trace = json.loads((tmp_path / "2026-09-30.jsonl").read_text(encoding="utf-8"))
    assert trace["status"] == "error"
    assert trace["error"] == "RuntimeError"
    assert "sensitive failure detail" not in json.dumps(trace)


def test_dropped_citations_are_persisted_in_the_trace_line(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    dropped = [{"kind": "doc", "doc_id": "d9", "section_key": "-", "call_id": None}]
    result = AnswerResult(
        answer=NO_ANSWER_TEXT,
        citations=(),
        refused=True,
        refuse_reason="no_valid_citation",
        trace={"dropped_citations": dropped},
    )
    with _session(recorder, "q-dropped") as session:
        session.finish(result)

    line = json.loads(next(tmp_path.glob("*.jsonl")).read_text().splitlines()[-1])
    assert line["dropped_citations"] == dropped
    assert session.record["dropped_citations"] == dropped


def test_missing_dropped_citations_default_to_an_empty_list(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    with _session(recorder, "q-clean") as session:
        session.finish(_success())

    assert session.record["dropped_citations"] == []
