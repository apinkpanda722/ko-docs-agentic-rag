"""Thin in-process answer adapter for the demo."""

import json
from dataclasses import dataclass, replace
from typing import Literal

import httpx

from kodocs.agent.answer import answer as _answer_fn
from kodocs.agent.runtime import build_default_runtime
from kodocs.agent.types import AgentRuntime
from kodocs.config import MissingAPIKeyError
from kodocs.costs import BudgetExceededError
from kodocs.llm.openai_chat import ChatParseError
from kodocs.ui.streaming import FinalStreamingChat
from kodocs.ui.users import DemoUser

STREAM_ERROR = "응답이 비정상적으로 길어져 중단됐습니다. 다시 시도해 주세요"


class UiError(RuntimeError):
    """A fixed public message that never contains the original exception."""


@dataclass(frozen=True)
class UiCitation:
    doc_id: str | None
    section_key: str | None
    title: str | None
    call_id: str | None


@dataclass(frozen=True)
class UiTool:
    name: str
    args_summary: str
    latency_ms: float
    tokens: dict[str, int]


@dataclass(frozen=True)
class UiAnswer:
    answer: str
    refused: bool
    refuse_reason: str | None
    citations: tuple[UiCitation, ...]
    tools: tuple[UiTool, ...]
    model: str
    cost_usd: float
    total_ms: float


def ask(
    question: str,
    user: DemoUser,
    system: Literal["simple", "agentic"],
    *,
    messages=None,
) -> UiAnswer:
    """Call the shared answer entry point; user attributes stay outside the prompt."""
    try:
        runtime = build_default_runtime()
        if messages is not None and isinstance(runtime, AgentRuntime):
            runtime = replace(
                runtime,
                chat=FinalStreamingChat(
                    runtime.chat, lambda delta: messages.put(("delta", delta))
                ),
                event_sink=lambda event: messages.put(("event", event)),
            )
        result = _answer_fn(
            question,
            user.to_user_context(),
            system=system,
            runtime=runtime,
        )
        if messages is not None and result.refuse_reason == "parse_error":
            raise ChatParseError("stream parse failure")
        trace = result.trace
        steps = trace.get("steps", [])
        tool_steps = [step for step in steps if step.get("kind") == "tool"]
        titles = {
            (ref.get("doc_id"), ref.get("section_key")): ref.get("title")
            for step in tool_steps
            for ref in step.get("refs", [])
            if isinstance(ref.get("title"), str)
        }
        citations = tuple(
            UiCitation(
                c.doc_id,
                c.section_key,
                titles.get((c.doc_id, c.section_key)),
                c.call_id,
            )
            for c in result.citations
        )
        tools = tuple(
            UiTool(
                name=step["name"],
                args_summary=json.dumps(step.get("args", {}), ensure_ascii=False)[:500],
                latency_ms=float(step.get("latency_ms", 0)),
                tokens=dict(step.get("tokens", {})),
            )
            for step in tool_steps
        )
        return UiAnswer(
            answer=result.answer,
            refused=result.refused,
            refuse_reason=result.refuse_reason,
            citations=citations,
            tools=tools,
            model=str(trace.get("model", {}).get("id", "알 수 없음")),
            cost_usd=sum(float(step.get("cost_usd", 0)) for step in steps),
            total_ms=float(trace.get("timings", {}).get("total_ms") or 0),
        )
    except ChatParseError:
        raise UiError(STREAM_ERROR) from None
    except MissingAPIKeyError:
        raise UiError(
            "API 키가 설정되지 않았습니다. 키 설정 후 다시 실행해 주세요."
        ) from None
    except BudgetExceededError:
        raise UiError(
            "월 비용 상한에 도달했습니다. 비용 원장을 확인해 주세요."
        ) from None
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise UiError(
            "OpenSearch에 연결할 수 없습니다. make up 실행 상태를 확인해 주세요."
        ) from None
    except Exception:  # noqa: BLE001 - never expose unknown exception values
        raise UiError(
            "요청 처리 중 오류가 발생했습니다. 실행 설정과 서비스 상태를 확인해 주세요."
        ) from None
