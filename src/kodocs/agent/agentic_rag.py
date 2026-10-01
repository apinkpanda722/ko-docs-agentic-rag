"""Bounded tool-using Agentic RAG graph with a separate final answer call."""

from __future__ import annotations

from typing import NotRequired

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.runtime import Runtime

from kodocs.agent.evidence import round_robin_context
from kodocs.agent.finalize import finalize_answer
from kodocs.agent.prompts import (
    AGENTIC_TOOL_PROMPT,
    COMMON_SYSTEM_PROMPT,
    build_final_messages,
)
from kodocs.agent.tools import AgentContext, build_langgraph_tools
from kodocs.agent.types import AgentRuntime, AnswerResult, ModelAnswer, RefuseReason
from kodocs.llm.openai_chat import ChatParseError, ChatUsage


class AgenticState(MessagesState):
    """Message history, original question, and normalized final result."""

    question: str
    result: NotRequired[AnswerResult]


def build_agentic_graph(runtime: AgentRuntime):
    """Compile the Agentic RAG loop and its common final-answer stage."""
    agent_runtime = runtime
    tools = build_langgraph_tools(runtime.deps)

    def agent(state: AgenticState, runtime: Runtime[AgentContext]) -> dict:
        ctx = runtime.context
        refuse_reason = ctx.guard.check_before_model_call()
        if refuse_reason is not None:
            return {"result": _finish_refusal(ctx, refuse_reason)}

        history = list(state["messages"])
        messages = history
        prefix: list[object] = []
        if not history:
            prefix = [
                SystemMessage(
                    content=f"{COMMON_SYSTEM_PROMPT}\n\n{AGENTIC_TOOL_PROMPT}"
                ),
                HumanMessage(content=state["question"]),
            ]
            messages = prefix

        chat_result = agent_runtime.chat.invoke(messages, tools=tools)
        tokens = _usage_record(chat_result.usage)
        ctx.guard.add_usage(sum(tokens.values()))
        ctx.trace.add_model_step(
            "agent",
            latency_ms=chat_result.latency_ms,
            tokens=tokens,
            cost_usd=chat_result.cost_usd,
            ttft_ms=chat_result.ttft_ms,
            attempts=chat_result.attempts,
        )

        message = chat_result.message
        calls = _tool_calls(message)
        if calls:
            refuse_reason = ctx.guard.register_tool_calls(len(calls))
            if refuse_reason is not None:
                return {
                    "messages": [*prefix, message],
                    "result": _finish_refusal(ctx, refuse_reason),
                }

        refuse_reason = ctx.guard.check_before_model_call()
        if refuse_reason is not None:
            return {
                "messages": [*prefix, message],
                "result": _finish_refusal(ctx, refuse_reason),
            }
        return {"messages": [*prefix, message]}

    def final(state: AgenticState, runtime: Runtime[AgentContext]) -> dict:
        ctx = runtime.context
        refuse_reason = ctx.guard.check_before_model_call()
        if refuse_reason is not None:
            return {"result": _finish_refusal(ctx, refuse_reason)}

        context_text = round_robin_context(
            ctx.evidence,
            budget=agent_runtime.guardrails.context_char_budget,
        )
        messages = build_final_messages(state["question"], context_text)
        started = agent_runtime.clock()
        try:
            chat_result = agent_runtime.chat.invoke(
                messages,
                response_schema=ModelAnswer,
            )
        except ChatParseError as exc:
            ctx.trace.add_model_step(
                "final_answer",
                latency_ms=_elapsed_ms(started, agent_runtime.clock()),
                error=exc,
                attempts=exc.attempts,
            )
            return {"result": _finish_refusal(ctx, "parse_error")}

        tokens = _usage_record(chat_result.usage)
        ctx.guard.add_usage(sum(tokens.values()))
        ctx.trace.add_model_step(
            "final_answer",
            latency_ms=chat_result.latency_ms,
            tokens=tokens,
            cost_usd=chat_result.cost_usd,
            ttft_ms=chat_result.ttft_ms,
            attempts=chat_result.attempts,
        )
        model_answer = (
            chat_result.parsed if isinstance(chat_result.parsed, ModelAnswer) else None
        )
        result = finalize_answer(model_answer, ctx.evidence)
        _finish_trace(ctx, result)
        return {"result": result}

    def route(state: AgenticState):
        if "result" in state:
            return END
        if _tool_calls(state["messages"][-1]):
            return "tools"
        return "final"

    graph = StateGraph(AgenticState, context_schema=AgentContext)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode(tools))
    graph.add_node("final", final)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route)
    graph.add_edge("tools", "agent")
    graph.add_edge("final", END)
    return graph.compile(name="agentic-rag")


def _tool_calls(message: object) -> list[object]:
    calls = getattr(message, "tool_calls", None)
    return list(calls) if isinstance(calls, list | tuple) else []


def _finish_refusal(ctx: AgentContext, reason: RefuseReason) -> AnswerResult:
    result = finalize_answer(None, ctx.evidence, refuse_reason=reason)
    _finish_trace(ctx, result)
    return result


def _finish_trace(ctx: AgentContext, result: AnswerResult) -> None:
    ctx.trace.finish(
        result,
        guardrail={
            "steps_used": ctx.guard.steps_used,
            "tokens_used": ctx.guard.tokens_used,
        },
    )


def _usage_record(usage: ChatUsage | None) -> dict[str, int]:
    if usage is None:
        return {
            "input": 0,
            "cached_input": 0,
            "cache_write": 0,
            "output": 0,
            "reasoning": 0,
        }
    return {
        "input": usage.input_tokens,
        "cached_input": usage.cached_input_tokens,
        "cache_write": usage.cache_write_tokens,
        "output": usage.output_tokens,
        "reasoning": usage.reasoning_tokens,
    }


def _elapsed_ms(started: float, ended: float) -> float:
    return max(0.0, (ended - started) * 1000)
