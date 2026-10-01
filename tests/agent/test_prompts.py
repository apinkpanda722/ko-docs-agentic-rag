"""Tests for shared answer prompts and runtime types."""

from __future__ import annotations

from pathlib import Path

from kodocs.agent.guardrails import GuardrailConfig
from kodocs.agent.prompts import (
    AGENTIC_TOOL_PROMPT,
    COMMON_SYSTEM_PROMPT,
    build_final_messages,
    prompt_hashes,
)
from kodocs.agent.types import AgentRuntime
from kodocs.tools import ToolDeps


def test_prompts_encode_grounding_injection_and_schema_rules() -> None:
    assert "제공된 근거" in COMMON_SYSTEM_PROMPT
    assert "refused=true" in COMMON_SYSTEM_PROMPT
    assert "citations" in COMMON_SYSTEM_PROMPT
    assert "지시문" in COMMON_SYSTEM_PROMPT
    assert "데이터" in COMMON_SYSTEM_PROMPT
    assert "권한" in COMMON_SYSTEM_PROMPT
    assert all(
        name in AGENTIC_TOOL_PROMPT
        for name in ("search_docs", "lookup_id", "run_sql", "get_document")
    )
    assert "5" in AGENTIC_TOOL_PROMPT


def test_final_messages_never_add_user_team_or_grade() -> None:
    messages = build_final_messages("질문", "근거 본문")
    serialized = repr(messages)

    assert messages[0] == {"role": "system", "content": COMMON_SYSTEM_PROMPT}
    assert "질문" in serialized
    assert "근거 본문" in serialized
    assert "team" not in serialized.casefold()
    assert "grade" not in serialized.casefold()


def test_prompt_hashes_are_separate_sha256_values() -> None:
    hashes = prompt_hashes()

    assert set(hashes) == {"common", "agentic"}
    assert all(len(value) == 64 for value in hashes.values())
    assert hashes["common"] != hashes["agentic"]


def test_agent_runtime_holds_only_injected_services_and_safe_metadata() -> None:
    runtime = AgentRuntime(
        deps=object(),  # type: ignore[arg-type]
        chat=object(),  # type: ignore[arg-type]
        model_record={"model": "fake"},
        m5_config_hash="hash",
    )

    assert isinstance(runtime.guardrails, GuardrailConfig)
    assert runtime.trace_dir == Path("logs/traces")
    assert runtime.clock() >= 0
    assert AgentRuntime.__annotations__["deps"] in {ToolDeps, "ToolDeps"}
