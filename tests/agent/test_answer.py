"""Tests for the shared answer entry point and execution traces."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from kodocs.agent.answer import answer
from kodocs.agent.types import AgentRuntime, Citation, ModelAnswer
from kodocs.llm.openai_chat import (
    BillingUncertainError,
    ChatResult,
    ChatUsage,
    FatalLLMError,
)
from kodocs.tools import ToolResult, UserContext


class _Chat:
    def __init__(
        self,
        final: ModelAnswer | BaseException,
        *,
        agent_turns: list[AIMessage] | None = None,
    ) -> None:
        self.final = final
        self.agent_turns = list(agent_turns or [])
        self.calls: list[dict[str, object]] = []

    def invoke(
        self,
        messages: object,
        *,
        tools=None,
        response_schema=None,
        stream: bool = False,
    ) -> ChatResult:
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "response_schema": response_schema,
                "stream": stream,
            }
        )
        if response_schema is ModelAnswer:
            if isinstance(self.final, BaseException):
                raise self.final
            message = AIMessage(content="final")
            parsed = self.final
        else:
            message = self.agent_turns.pop(0)
            parsed = None
        return ChatResult(
            message=message,
            parsed=parsed,
            usage=ChatUsage(10, 2, 1, 4, 3, True),
            cost_usd=0.01,
            latency_ms=4.0,
            ttft_ms=None,
            attempts=(),
            request_id="fake",
            model="fake",
            settings_hash="settings",
            generation={},
        )


def _runtime(chat: _Chat, trace_dir: Path) -> AgentRuntime:
    return AgentRuntime(
        deps=SimpleNamespace(prefer_current=True),  # type: ignore[arg-type]
        chat=chat,
        model_record={
            "model": "fake",
            "effort": "low",
            "temperature": None,
        },
        m5_config_hash="455f",
        trace_dir=trace_dir,
    )


def _search_result() -> ToolResult:
    return ToolResult(
        payload={
            "results": [
                {
                    "doc_id": "d1",
                    "title": "문서",
                    "section_key": "s1",
                    "section_kind": "section",
                    "is_current": True,
                    "text": "근거" * 3_500,
                }
            ],
            "truncated": True,
        },
        is_error=False,
        refs=({"kind": "doc", "doc_id": "d1", "section_key": "s1"},),
        timings={"embed_ms": 1.5, "search_ms": 2.5},
    )


def test_simple_and_agentic_use_identical_final_messages_and_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    final = ModelAnswer(
        answer="답",
        citations=[Citation(kind="doc", doc_id="d1", section_key="s1")],
        refused=False,
    )
    simple_chat = _Chat(final)
    agentic_chat = _Chat(
        final,
        agent_turns=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_docs",
                        "args": {"query": "같은 질문"},
                        "id": "t1",
                    }
                ],
            ),
            AIMessage(content="근거 확보"),
        ],
    )
    monkeypatch.setattr(
        "kodocs.agent.tools.run_tool", lambda *_args, **_kwargs: _search_result()
    )

    simple = answer(
        "같은 질문",
        UserContext("platform", 3),
        system="simple",
        runtime=_runtime(simple_chat, tmp_path / "simple"),
        question_id="simple-q",
    )
    agentic = answer(
        "같은 질문",
        UserContext("platform", 3),
        system="agentic",
        runtime=_runtime(agentic_chat, tmp_path / "agentic"),
        question_id="agentic-q",
    )

    simple_messages = simple_chat.calls[-1]["messages"]
    agentic_messages = agentic_chat.calls[-1]["messages"]
    assert simple_messages == agentic_messages
    evidence = simple_messages[1]["content"].split("<evidence>\n", 1)[1]
    assert evidence.count("근거") == 3_000
    assert simple.answer == agentic.answer == "답"
    assert simple.trace["system"] == "simple"
    assert agentic.trace["system"] == "agentic"


def test_answer_trace_contains_steps_tokens_cost_and_latency_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    chat = _Chat(
        ModelAnswer(
            answer="답",
            citations=[Citation(kind="doc", doc_id="d1", section_key="s1")],
            refused=False,
        ),
        agent_turns=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_docs",
                        "args": {"query": "질문"},
                        "id": "t1",
                    }
                ],
            ),
            AIMessage(content="완료"),
        ],
    )
    monkeypatch.setattr(
        "kodocs.agent.tools.run_tool", lambda *_args, **_kwargs: _search_result()
    )

    result = answer(
        "질문",
        UserContext("platform", 3),
        system="agentic",
        runtime=_runtime(chat, tmp_path),
        question_id="q-trace",
    )

    trace = result.trace
    assert [step["kind"] for step in trace["steps"]] == [
        "model",
        "tool",
        "model",
        "model",
    ]
    assert all("tokens" in step and "cost_usd" in step for step in trace["steps"])
    assert trace["guardrail"] == {"steps_used": 1, "tokens_used": 60}
    assert trace["timings"]["embed_ms"] == 1.5
    assert trace["timings"]["search_ms"] == 2.5
    assert trace["timings"]["ttft_ms"] is None
    assert trace["timings"]["total_ms"] >= 0
    assert trace["hashes"]["evidence_format_version"] == 2  # run_sql evidence has SQL
    line = json.loads(next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8"))
    assert line == trace


def test_graph_is_compiled_once_per_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import importlib

    answer_module = importlib.import_module("kodocs.agent.answer")

    chat = _Chat(ModelAnswer(answer="없음", citations=[], refused=True))
    runtime = _runtime(chat, tmp_path)
    monkeypatch.setattr(
        "kodocs.agent.tools.run_tool", lambda *_args, **_kwargs: _search_result()
    )
    original = answer_module.build_simple_graph
    builds = 0

    def counted(active_runtime):
        nonlocal builds
        builds += 1
        return original(active_runtime)

    monkeypatch.setattr(answer_module, "build_simple_graph", counted)
    for number in range(2):
        answer_module.answer(
            "질문",
            UserContext("platform", 3),
            system="simple",
            runtime=runtime,
            question_id=f"q-{number}",
        )

    assert builds == 1


@pytest.mark.parametrize(
    "failure",
    [BillingUncertainError("uncertain"), FatalLLMError("fatal")],
)
def test_fatal_failures_are_traced_and_reraised(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: BaseException,
) -> None:
    monkeypatch.setattr(
        "kodocs.agent.tools.run_tool", lambda *_args, **_kwargs: _search_result()
    )
    runtime = _runtime(_Chat(failure), tmp_path)

    with pytest.raises(type(failure)):
        answer(
            "질문",
            UserContext("platform", 3),
            system="simple",
            runtime=runtime,
            question_id="q-error",
        )

    trace = json.loads(next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8"))
    assert trace["status"] == "error"
    assert trace["error"] == type(failure).__name__
