"""LangGraph tool wrappers over the shared authorization-aware dispatcher."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from langchain.tools import ToolRuntime, tool
from langchain_core.tools import BaseTool, StructuredTool

from kodocs.agent.evidence import EvidenceLog
from kodocs.agent.guardrails import Guard
from kodocs.agent.trace import TraceSession, _sanitize
from kodocs.agent.types import RefuseReason
from kodocs.tools import (
    INVALID_ARGUMENT,
    ToolDeps,
    ToolResult,
    UserContext,
    error_payload,
    run_tool,
)
from kodocs.tools.contract import (
    TOOL_DESCRIPTIONS,
    TOOL_INPUT_MODELS,
    ToolName,
)

_INVALID_ARGUMENT_MESSAGE = "도구 인자가 올바르지 않습니다."


class ToolCallGuardrailError(RuntimeError):
    """Raised before retrieval when the per-question tool limit is exhausted."""

    def __init__(self, reason: RefuseReason) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class AgentContext:
    """Server-injected, per-question context hidden from model tool arguments."""

    user: UserContext
    evidence: EvidenceLog
    guard: Guard
    trace: TraceSession
    event_sink: Callable[[Mapping[str, object]], None] | None = None
    _event_state: dict = field(default_factory=lambda: {"index": 0, "errors": 0})
    _event_lock: object = field(default_factory=threading.Lock)

    def emit(
        self,
        kind,
        name,
        *,
        index=None,
        args=None,
        latency_ms=0.0,
        error=None,
        refs_count=0,
    ):
        """Publish an immutable, content-free snapshot; observers cannot fail RAG."""
        if self.event_sink is None:
            return 0
        with self._event_lock:
            if index is None:
                self._event_state["index"] += 1
                index = self._event_state["index"]
        snapshot = _freeze_event(
            {
                "kind": kind,
                "name": name,
                "args": _sanitize(args or {}),
                "latency_ms": latency_ms,
                "error": error,
                "refs_count": refs_count,
                "index": index,
            }
        )
        try:
            self.event_sink(snapshot)
        except Exception:  # noqa: BLE001 - observer errors must not affect answers
            with self._event_lock:
                self._event_state["errors"] += 1
        return index


def _freeze_event(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze_event(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_event(v) for v in value)
    return value


class _RuntimeStructuredTool(StructuredTool):
    """Validate model args before restoring ToolNode-injected runtime args.

    LangGraph 1.2 injects ``ToolRuntime`` into the tool-call argument mapping.
    LangChain 1.6 validates that mapping against an explicit ``args_schema`` before
    restoring injected parameters, so an ``extra="forbid"`` contract would reject
    the trusted runtime value. This override preserves the exact contract schema
    while applying the order intended by the installed BaseTool implementation.
    """

    def _parse_input(self, tool_input, tool_call_id):
        if not isinstance(tool_input, dict):
            return super()._parse_input(tool_input, tool_call_id)
        injected = {
            key: tool_input[key]
            for key in self._injected_args_keys
            if key in tool_input
        }
        model_input = {
            key: value for key, value in tool_input.items() if key not in injected
        }
        parsed = super()._parse_input(model_input, tool_call_id)
        if isinstance(parsed, dict):
            parsed.update(injected)
        return parsed


def execute_tool_call(
    ctx: AgentContext,
    deps: ToolDeps,
    name: str,
    args: dict,
) -> ToolResult:
    """Execute, capture, and trace one tool call through the shared dispatcher."""
    if not ctx.guard.consume_registered_tool_call():
        refuse_reason = ctx.guard.register_tool_calls(1)
        if refuse_reason is not None:
            raise ToolCallGuardrailError(refuse_reason)
        if not ctx.guard.consume_registered_tool_call():
            raise RuntimeError("registered tool call could not be claimed")

    index = ctx.emit("tool_start", name, args=args)
    started = time.perf_counter()
    try:
        result = run_tool(name, args, ctx.user, deps)
    except Exception as exc:
        ctx.emit(
            "tool_end",
            name,
            index=index,
            args=args,
            latency_ms=max(0.0, (time.perf_counter() - started) * 1000),
            error=type(exc).__name__,
        )
        raise
    latency_ms = max(0.0, (time.perf_counter() - started) * 1000)
    ctx.evidence.record(name, args, result)
    ctx.trace.add_tool_step(name, args, result, latency_ms=latency_ms)
    ctx.emit(
        "tool_end",
        name,
        index=index,
        args=args,
        latency_ms=latency_ms,
        error=result.payload.get("error", {}).get("code") if result.is_error else None,
        refs_count=len(result.refs),
    )
    return result


def build_langgraph_tools(deps: ToolDeps) -> list[BaseTool]:
    """Build the four model-visible tools with hidden ``ToolRuntime`` context."""
    return [_build_langgraph_tool(name, deps) for name in TOOL_INPUT_MODELS]


def _build_langgraph_tool(name: ToolName, deps: ToolDeps) -> BaseTool:
    @tool(
        name,
        description=TOOL_DESCRIPTIONS[name],
        args_schema=TOOL_INPUT_MODELS[name],
        infer_schema=False,
    )
    def invoke(runtime: ToolRuntime[AgentContext, Any], **kwargs: object) -> str:
        result = execute_tool_call(runtime.context, deps, name, dict(kwargs))
        return json.dumps(
            result.payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    invoke.handle_validation_error = _invalid_argument_json
    return _RuntimeStructuredTool(**invoke.model_dump())


def _invalid_argument_json(_error: Exception) -> str:
    return json.dumps(
        error_payload(INVALID_ARGUMENT, _INVALID_ARGUMENT_MESSAGE),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
