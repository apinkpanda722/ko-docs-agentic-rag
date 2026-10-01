"""Tests for the single agent tool execution path and LangGraph wrappers."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from kodocs.agent.evidence import EvidenceLog
from kodocs.agent.guardrails import Guard, GuardrailConfig
from kodocs.agent.tools import AgentContext, build_langgraph_tools
from kodocs.tools import ToolResult, UserContext
from kodocs.tools.contract import (
    TOOL_DESCRIPTIONS,
    TOOL_INPUT_MODELS,
)


class _Trace:
    def __init__(self) -> None:
        self.tool_steps: list[tuple[str, dict[str, object], ToolResult, float]] = []

    def add_tool_step(
        self,
        name: str,
        args: dict[str, object],
        result: ToolResult,
        *,
        latency_ms: float,
    ) -> None:
        self.tool_steps.append((name, args, result, latency_ms))


def _context(user: UserContext) -> AgentContext:
    return AgentContext(
        user=user,
        evidence=EvidenceLog(),
        guard=Guard(GuardrailConfig(), timeout_s=45),
        trace=_Trace(),  # type: ignore[arg-type]
    )


def _invoke_tool_node(tools: list, context: AgentContext, args: dict[str, object]):
    graph = StateGraph(MessagesState, context_schema=AgentContext)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    compiled = graph.compile()
    return compiled.invoke(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "search_docs",
                            "args": args,
                            "id": "tool-call-1",
                        }
                    ],
                )
            ]
        },
        context=context,
    )


def test_langgraph_tool_schemas_exactly_match_contract_models() -> None:
    tools = build_langgraph_tools(object())  # type: ignore[arg-type]

    assert {tool.name for tool in tools} == set(TOOL_INPUT_MODELS)
    for tool in tools:
        contract = TOOL_INPUT_MODELS[tool.name]
        assert tool.description == TOOL_DESCRIPTIONS[tool.name]
        assert tool.args_schema is contract
        assert tool.args_schema.model_json_schema() == contract.model_json_schema()
        assert {"runtime", "team", "grade", "user"}.isdisjoint(
            tool.args_schema.model_fields
        )


def test_toolruntime_injects_graph_context_into_single_execution_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = UserContext("platform", 3)
    context = _context(user)
    captured: dict[str, object] = {}

    def fake_run_tool(name, args, ctx, deps):
        captured.update(name=name, args=args, ctx=ctx, deps=deps)
        return ToolResult(
            payload={"results": [], "truncated": False},
            is_error=False,
            refs=(),
            timings={"embed_ms": 1.0, "search_ms": 2.0},
        )

    monkeypatch.setattr("kodocs.agent.tools.run_tool", fake_run_tool)
    deps = SimpleNamespace(prefer_current=True)

    output = _invoke_tool_node(
        build_langgraph_tools(deps),  # type: ignore[arg-type]
        context,
        {"query": "원문 질문"},
    )

    assert captured == {
        "name": "search_docs",
        "args": {"query": "원문 질문"},
        "ctx": user,
        "deps": deps,
    }
    assert context.guard.steps_used == 1
    assert len(context.evidence.calls) == 1
    assert len(context.trace.tool_steps) == 1  # type: ignore[attr-defined]
    payload = json.loads(output["messages"][-1].content)
    assert payload == {"results": [], "truncated": False}


def test_langgraph_tool_extra_argument_returns_contract_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_run_tool(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("invalid arguments must not execute retrieval")

    monkeypatch.setattr("kodocs.agent.tools.run_tool", fake_run_tool)
    output = _invoke_tool_node(
        build_langgraph_tools(object()),  # type: ignore[arg-type]
        _context(UserContext("platform", 3)),
        {"query": "질문", "team": "security"},
    )

    payload = json.loads(output["messages"][-1].content)
    assert called is False
    assert payload == {
        "error": {
            "code": "invalid_argument",
            "message": "도구 인자가 올바르지 않습니다.",
        }
    }
