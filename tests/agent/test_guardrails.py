"""Tests for one-question answer guardrails."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from kodocs.agent.guardrails import Guard, GuardrailConfig


class Clock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def test_registered_guardrail_defaults_and_hash_are_stable() -> None:
    config = GuardrailConfig()

    assert config.record() == {
        "max_steps": 5,
        "max_tokens_per_question": 100_000,
        "request_timeout_s": 30,
        "simple_timeout_s": 45,
        "agentic_timeout_s": 120,
        "max_output_tokens": 4_000,
        "context_top_k": 8,
        "context_char_budget": 6_000,
    }
    assert len(config.settings_hash()) == 64
    with pytest.raises(FrozenInstanceError):
        config.max_steps = 6  # type: ignore[misc]


def test_five_tool_calls_are_allowed_and_sixth_is_rejected_before_execution() -> None:
    guard = Guard(GuardrailConfig(), timeout_s=120, clock=Clock())

    assert guard.register_tool_calls(2) is None
    assert guard.register_tool_calls(3) is None
    assert guard.steps_used == 5
    assert guard.register_tool_calls(1) == "guardrail_max_steps"
    assert guard.steps_used == 5


def test_parallel_tool_calls_are_counted_individually() -> None:
    guard = Guard(GuardrailConfig(), timeout_s=120, clock=Clock())

    assert guard.register_tool_calls(4) is None
    assert guard.register_tool_calls(2) == "guardrail_max_steps"
    assert guard.steps_used == 4


def test_token_limit_is_checked_before_the_next_model_call() -> None:
    guard = Guard(GuardrailConfig(), timeout_s=120, clock=Clock())

    guard.add_usage(99_999)
    assert guard.check_before_model_call() is None
    guard.add_usage(1)
    assert guard.check_before_model_call() == "guardrail_tokens"


def test_deadline_uses_injected_clock() -> None:
    clock = Clock(10.0)
    guard = Guard(GuardrailConfig(), timeout_s=45, clock=clock)

    assert guard.deadline == 55.0
    clock.value = 54.9
    assert guard.check_before_model_call() is None
    clock.value = 55.0
    assert guard.check_before_model_call() == "guardrail_timeout"
