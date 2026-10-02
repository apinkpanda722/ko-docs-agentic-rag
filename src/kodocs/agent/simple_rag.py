"""Fixed one-search, one-model-call simple RAG graph."""

from __future__ import annotations

from typing import NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from kodocs.agent.evidence import round_robin_context
from kodocs.agent.finalize import finalize_answer
from kodocs.agent.prompts import build_final_messages
from kodocs.agent.tools import AgentContext, execute_tool_call
from kodocs.agent.types import AgentRuntime, AnswerResult, ModelAnswer, RefuseReason
from kodocs.llm.openai_chat import ChatParseError, ChatUsage


class SimpleState(TypedDict):
    """Graph state containing only model-visible input and the final result."""

    question: str
    result: NotRequired[AnswerResult]


def build_simple_graph(runtime: AgentRuntime):
    """Compile the pre-registered simple RAG flow: retrieve then answer."""
    agent_runtime = runtime

    def retrieve(state: SimpleState, runtime: Runtime[AgentContext]) -> dict:
        execute_tool_call(
            runtime.context,
            agent_runtime.deps,
            "search_docs",
            {"query": state["question"]},
        )
        return {}

    def answer(state: SimpleState, runtime: Runtime[AgentContext]) -> dict:
        ctx = runtime.context
        refuse_reason = ctx.guard.check_before_model_call()
        if refuse_reason is not None:
            return {"result": _finish_refusal(ctx, refuse_reason)}

        context_text = round_robin_context(
            ctx.evidence,
            budget=agent_runtime.guardrails.context_char_budget,
        )
        messages = build_final_messages(state["question"], context_text)
        index = ctx.emit("final_start", "final_answer")
        ctx.emit("model_start", "final_answer", index=index)
        started = agent_runtime.clock()
        try:
            chat_result = agent_runtime.chat.invoke(
                messages,
                response_schema=ModelAnswer,
            )
        except ChatParseError as exc:
            ctx.emit(
                "model_end",
                "final_answer",
                index=index,
                latency_ms=_elapsed_ms(started, agent_runtime.clock()),
                error="ChatParseError",
            )
            ctx.trace.add_model_step(
                "final_answer",
                latency_ms=_elapsed_ms(started, agent_runtime.clock()),
                error=exc,
                attempts=exc.attempts,
            )
            return {"result": _finish_refusal(ctx, "parse_error")}
        except Exception as exc:
            ctx.emit(
                "model_end",
                "final_answer",
                index=index,
                latency_ms=_elapsed_ms(started, agent_runtime.clock()),
                error=type(exc).__name__,
            )
            raise

        ctx.emit(
            "model_end", "final_answer", index=index, latency_ms=chat_result.latency_ms
        )
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
        ctx.trace.finish(
            result,
            guardrail={
                "steps_used": ctx.guard.steps_used,
                "tokens_used": ctx.guard.tokens_used,
            },
        )
        return {"result": result}

    graph = StateGraph(SimpleState, context_schema=AgentContext)
    graph.add_node("retrieve", retrieve)
    graph.add_node("answer", answer)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "answer")
    graph.add_edge("answer", END)
    return graph.compile(name="simple-rag")


def _finish_refusal(ctx: AgentContext, reason: RefuseReason) -> AnswerResult:
    result = finalize_answer(None, ctx.evidence, refuse_reason=reason)
    ctx.trace.finish(
        result,
        guardrail={
            "steps_used": ctx.guard.steps_used,
            "tokens_used": ctx.guard.tokens_used,
        },
    )
    return result


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
