"""Shared answer types independent of any graph implementation."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, model_validator

from kodocs.tools import ToolDeps

if TYPE_CHECKING:
    from kodocs.agent.guardrails import GuardrailConfig
    from kodocs.llm.openai_chat import ChatResult

type CitationKind = Literal["doc", "sql"]
type RefuseReason = Literal[
    "model_refused",
    "no_valid_citation",
    "parse_error",
    "guardrail_max_steps",
    "guardrail_tokens",
    "guardrail_timeout",
]


class Citation(BaseModel):
    """One model-proposed citation tied to retrieved or structured evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: CitationKind
    doc_id: str | None = None
    section_key: str | None = None
    call_id: str | None = None

    @model_validator(mode="after")
    def validate_kind_fields(self) -> Self:
        """Require exactly the fields meaningful for this citation kind."""
        if self.kind == "doc":
            if not self.doc_id:
                raise ValueError("doc citation requires doc_id")
            if self.call_id is not None:
                raise ValueError("doc citation cannot contain call_id")
        else:
            if not self.call_id:
                raise ValueError("sql citation requires call_id")
            if self.doc_id is not None or self.section_key is not None:
                raise ValueError("sql citation cannot contain document fields")
        return self


class ModelAnswer(BaseModel):
    """Structured output requested from the final answer-model call."""

    model_config = ConfigDict(extra="forbid", strict=True)

    answer: str
    citations: list[Citation]
    refused: bool


@dataclass(frozen=True)
class AnswerResult:
    """Public answer contract shared by simple and agentic systems."""

    answer: str
    citations: tuple[Citation, ...]
    refused: bool
    refuse_reason: str | None
    trace: dict[str, object]


class ChatLike(Protocol):
    """Narrow managed-chat surface used by answer orchestration and fakes."""

    def invoke(
        self,
        messages: object,
        *,
        tools: Sequence[object] | None = None,
        response_schema: type[object] | None = None,
        stream: bool = False,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> ChatResult: ...


@dataclass(frozen=True)
class AgentRuntime:
    """Server-injected services and immutable settings for one answer system."""

    deps: ToolDeps
    chat: ChatLike
    model_record: dict[str, object]
    m5_config_hash: str
    guardrails: GuardrailConfig = field(default_factory=lambda: _guardrail_config())
    trace_dir: Path = Path("logs/traces")
    clock: Callable[[], float] = time.monotonic
    event_sink: Callable[[Mapping[str, object]], None] | None = None


def _guardrail_config() -> GuardrailConfig:
    from kodocs.agent.guardrails import GuardrailConfig

    return GuardrailConfig()


def canonical_hash(value: Mapping[str, object]) -> str:
    """Hash JSON-compatible settings using the repository's canonical form."""
    import hashlib
    import json

    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
