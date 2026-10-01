"""Tests for the fixed one-search, one-answer simple RAG graph."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from kodocs.agent.evidence import EvidenceLog
from kodocs.agent.finalize import NO_ANSWER_TEXT
from kodocs.agent.guardrails import Guard
from kodocs.agent.simple_rag import SimpleState, build_simple_graph
from kodocs.agent.tools import AgentContext
from kodocs.agent.trace import TraceRecorder
from kodocs.agent.types import AgentRuntime, Citation, ModelAnswer
from kodocs.llm.openai_chat import ChatParseError, ChatResult, ChatUsage
from kodocs.tools import ToolResult, UserContext


class _Clock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class _Chat:
    def __init__(self, answer: ModelAnswer | BaseException) -> None:
        self.answer = answer
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
        if isinstance(self.answer, BaseException):
            raise self.answer
        return ChatResult(
            message=object(),
            parsed=self.answer,
            usage=ChatUsage(
                input_tokens=10,
                cached_input_tokens=2,
                cache_write_tokens=1,
                output_tokens=4,
                reasoning_tokens=3,
                cache_write_tokens_present=True,
            ),
            cost_usd=0.01,
            latency_ms=12.0,
            ttft_ms=None,
            attempts=(),
            request_id="fake",
            model="fake",
            settings_hash="settings",
            generation={},
        )


class _Trace:
    def __init__(self) -> None:
        self.tool_steps: list[dict[str, object]] = []
        self.model_steps: list[dict[str, object]] = []
        self.finished = []

    def add_tool_step(self, name, args, result, *, latency_ms, error=None) -> None:
        self.tool_steps.append(
            {
                "name": name,
                "args": args,
                "result": result,
                "latency_ms": latency_ms,
                "error": error,
            }
        )

    def add_model_step(self, name, **kwargs) -> None:
        self.model_steps.append({"name": name, **kwargs})

    def finish(self, result, *, guardrail=None, status="completed") -> Path:
        self.finished.append((result, guardrail, status))
        return Path("trace.jsonl")


def _runtime(chat: _Chat, clock: _Clock | None = None) -> AgentRuntime:
    active_clock = clock or _Clock()
    return AgentRuntime(
        deps=SimpleNamespace(prefer_current=True),  # type: ignore[arg-type]
        chat=chat,
        model_record={"model": "fake", "effort": "low"},
        m5_config_hash="455f",
        clock=active_clock,
    )


def _context(
    user: UserContext,
    runtime: AgentRuntime,
    *,
    trace: object | None = None,
) -> AgentContext:
    return AgentContext(
        user=user,
        evidence=EvidenceLog(),
        guard=Guard(
            runtime.guardrails,
            timeout_s=runtime.guardrails.simple_timeout_s,
            clock=runtime.clock,
        ),
        trace=trace or _Trace(),  # type: ignore[arg-type]
    )


def _search_result(prefix: str = "doc") -> ToolResult:
    results = [
        {
            "doc_id": f"{prefix}-{number}",
            "title": f"문서 {number}",
            "section_key": f"s{number}",
            "section_kind": "section",
            "is_current": True,
            "text": str(number) * 750,
        }
        for number in range(8)
    ]
    return ToolResult(
        payload={"results": results, "truncated": False},
        is_error=False,
        refs=tuple(
            {
                "kind": "doc",
                "doc_id": item["doc_id"],
                "section_key": item["section_key"],
            }
            for item in results
        ),
        timings={"embed_ms": 1.0, "search_ms": 2.0},
    )


def test_simple_graph_searches_original_question_once_and_answers_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    question = "  원문 질문을 그대로 검색해 주세요?  "
    chat = _Chat(
        ModelAnswer(
            answer="답변",
            citations=[
                Citation(kind="doc", doc_id="doc-0", section_key="s0"),
                Citation(kind="doc", doc_id="권한-밖-문서", section_key="secret"),
            ],
            refused=False,
        )
    )
    runtime = replace(_runtime(chat), trace_dir=tmp_path)
    user = UserContext("platform", 3)
    recorder = TraceRecorder(
        tmp_path,
        clock=runtime.clock,
        now=lambda: datetime(2026, 9, 30, tzinfo=UTC),
    )
    trace = recorder.start(
        question_id="q-1",
        system="simple",
        user=user,
        model_record=runtime.model_record,
        hashes={"m5_config_hash": runtime.m5_config_hash},
    )
    context = _context(user, runtime, trace=trace)
    calls: list[tuple[str, dict[str, object], UserContext, object]] = []

    def fake_run_tool(name, args, ctx, deps):
        calls.append((name, args, ctx, deps))
        assert deps.prefer_current is True
        return _search_result()

    monkeypatch.setattr("kodocs.agent.tools.run_tool", fake_run_tool)

    output: SimpleState = build_simple_graph(runtime).invoke(
        {"question": question},
        context=context,
    )

    assert calls == [("search_docs", {"query": question}, user, runtime.deps)]
    assert len(chat.calls) == 1
    assert chat.calls[0]["tools"] is None
    assert chat.calls[0]["response_schema"] is ModelAnswer
    messages = chat.calls[0]["messages"]
    evidence = (
        messages[1]["content"].split("<evidence>\n", 1)[1].split("\n</evidence>", 1)[0]
    )
    assert sum(evidence.count(str(number) * 750) for number in range(8)) == 8
    assert sum(len(call.items[0].text) for call in context.evidence.calls) == 750
    assert sum(len(item.text) for item in context.evidence.calls[0].items) == 6_000
    assert output["result"].answer == "답변"
    assert output["result"].citations == (
        Citation(kind="doc", doc_id="doc-0", section_key="s0"),
    )
    trace_lines = (
        (tmp_path / "2026-09-30.jsonl").read_text(encoding="utf-8").splitlines()
    )
    assert len(trace_lines) == 1
    recorded = json.loads(trace_lines[0])
    assert [step["kind"] for step in recorded["steps"]] == ["tool", "model"]
    assert recorded["citations"] == [
        {
            "call_id": None,
            "doc_id": "doc-0",
            "kind": "doc",
            "section_key": "s0",
        }
    ]
    assert recorded["guardrail"] == {"steps_used": 1, "tokens_used": 20}


@pytest.mark.parametrize(
    ("answer", "prepare_guard", "reason", "model_calls"),
    [
        (
            ChatParseError("bad structured output"),
            lambda _guard: None,
            "parse_error",
            1,
        ),
        (
            ModelAnswer(answer="거절", citations=[], refused=True),
            lambda _guard: None,
            "model_refused",
            1,
        ),
        (
            ModelAnswer(answer="미호출", citations=[], refused=False),
            lambda guard: guard.add_usage(100_000),
            "guardrail_tokens",
            0,
        ),
    ],
)
def test_simple_graph_normalizes_parse_model_and_guardrail_refusals(
    monkeypatch: pytest.MonkeyPatch,
    answer: ModelAnswer | BaseException,
    prepare_guard,
    reason: str,
    model_calls: int,
) -> None:
    chat = _Chat(answer)
    runtime = _runtime(chat)
    context = _context(UserContext("platform", 3), runtime)
    prepare_guard(context.guard)
    monkeypatch.setattr(
        "kodocs.agent.tools.run_tool",
        lambda *_args, **_kwargs: _search_result(),
    )

    output = build_simple_graph(runtime).invoke(
        {"question": "질문"},
        context=context,
    )

    assert output["result"].answer == NO_ANSWER_TEXT
    assert output["result"].refused is True
    assert output["result"].refuse_reason == reason
    assert len(chat.calls) == model_calls
    assert len(context.trace.finished) == 1  # type: ignore[attr-defined]


def test_simple_graph_timeout_after_retrieval_skips_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    chat = _Chat(ModelAnswer(answer="미호출", citations=[], refused=False))
    runtime = _runtime(chat, clock)
    context = _context(UserContext("platform", 3), runtime)

    def expire_after_search(*_args, **_kwargs):
        clock.value = 45.0
        return _search_result()

    monkeypatch.setattr("kodocs.agent.tools.run_tool", expire_after_search)
    output = build_simple_graph(runtime).invoke({"question": "질문"}, context=context)

    assert output["result"].refuse_reason == "guardrail_timeout"
    assert chat.calls == []


def test_reusing_graph_with_alternating_users_never_mixes_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ContextChat(_Chat):
        def invoke(self, messages, **kwargs):
            evidence = messages[1]["content"]
            team = "team-a" if "team-a-0" in evidence else "team-b"
            self.answer = ModelAnswer(
                answer=team,
                citations=[Citation(kind="doc", doc_id=f"{team}-0", section_key="s0")],
                refused=False,
            )
            return super().invoke(messages, **kwargs)

    chat = ContextChat(ModelAnswer(answer="unused", citations=[], refused=True))
    runtime = _runtime(chat)
    graph = build_simple_graph(runtime)

    def user_scoped_result(_name, _args, ctx, _deps):
        return _search_result(ctx.team)

    monkeypatch.setattr("kodocs.agent.tools.run_tool", user_scoped_result)
    contexts = [
        _context(UserContext("team-a", 1), runtime),
        _context(UserContext("team-b", 2), runtime),
        _context(UserContext("team-a", 1), runtime),
    ]

    outputs = [
        graph.invoke({"question": "같은 질문"}, context=context)["result"]
        for context in contexts
    ]

    assert [result.answer for result in outputs] == ["team-a", "team-b", "team-a"]
    evidence_messages = [call["messages"][1]["content"] for call in chat.calls]
    assert "team-b-0" not in evidence_messages[0]
    assert "team-a-0" not in evidence_messages[1]
    assert "team-b-0" not in evidence_messages[2]
