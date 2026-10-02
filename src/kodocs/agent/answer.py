"""Shared public entry point for simple and agentic answer systems."""

from __future__ import annotations

import threading
import uuid
from dataclasses import replace
from typing import Literal

from kodocs.agent.agentic_rag import build_agentic_graph
from kodocs.agent.evidence import EvidenceLog
from kodocs.agent.guardrails import Guard
from kodocs.agent.prompts import prompt_hashes
from kodocs.agent.simple_rag import build_simple_graph
from kodocs.agent.tools import AgentContext
from kodocs.agent.trace import TraceRecorder
from kodocs.agent.types import AgentRuntime, AnswerResult
from kodocs.tools import UserContext

type AnswerSystem = Literal["simple", "agentic"]

_GRAPH_CACHE: dict[int, tuple[AgentRuntime, dict[AnswerSystem, object]]] = {}
_GRAPH_CACHE_LOCK = threading.Lock()


def answer(
    question: str,
    user_ctx: UserContext,
    *,
    system: Literal["simple", "agentic"],
    runtime: AgentRuntime,
    question_id: str | None = None,
) -> AnswerResult:
    """Run one isolated answer execution through a cached compiled graph."""
    if system not in {"simple", "agentic"}:
        raise ValueError("system must be simple or agentic")

    recorder = TraceRecorder(runtime.trace_dir, clock=runtime.clock)
    prompts = prompt_hashes()
    trace = recorder.start(
        question_id=question_id or uuid.uuid4().hex,
        system=system,
        user=user_ctx,
        model_record=runtime.model_record,
        hashes={
            "common_prompt": prompts["common"],
            "agentic_prompt": prompts["agentic"],
            "guardrail_settings": runtime.guardrails.settings_hash(),
            "m5_config_hash": runtime.m5_config_hash,
        },
    )
    timeout_s = (
        runtime.guardrails.simple_timeout_s
        if system == "simple"
        else runtime.guardrails.agentic_timeout_s
    )
    context = AgentContext(
        user=user_ctx,
        evidence=EvidenceLog(),
        guard=Guard(runtime.guardrails, timeout_s=timeout_s, clock=runtime.clock),
        trace=trace,
        event_sink=runtime.event_sink,
    )
    graph = _compiled_graph(runtime, system)
    inputs = {"question": question}
    if system == "agentic":
        inputs["messages"] = []

    with trace:
        state = graph.invoke(inputs, context=context)  # type: ignore[attr-defined]
    result = state.get("result")
    if not isinstance(result, AnswerResult):
        raise TypeError("answer graph did not return an AnswerResult")
    return replace(result, trace=trace.record)


def _compiled_graph(runtime: AgentRuntime, system: AnswerSystem):
    key = id(runtime)
    with _GRAPH_CACHE_LOCK:
        entry = _GRAPH_CACHE.get(key)
        if entry is None or entry[0] is not runtime:
            entry = (runtime, {})
            _GRAPH_CACHE[key] = entry
        graphs = entry[1]
        graph = graphs.get(system)
        if graph is None:
            graph = (
                build_simple_graph(runtime)
                if system == "simple"
                else build_agentic_graph(runtime)
            )
            graphs[system] = graph
        return graph
