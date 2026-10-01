from __future__ import annotations

from pathlib import Path

import pytest

from kodocs.agent.guardrails import GuardrailConfig
from kodocs.costs import BudgetExceededError
from kodocs.eval.m6_dev import (
    _validate_model_comparison,
    estimate_dev_dry_run,
    select_model,
)
from kodocs.eval.m6_inputs import load_m6_questions
from kodocs.llm.answer_settings import load_answer_config


def _stats(*, gate=True, cost=1.0, latency=2.0, concurrency=4):
    return {
        "gate_passed": gate,
        "mean_answer_match": 0.5,
        "mean_cost_usd": cost,
        "latency_p50_s": latency,
        "concurrency": concurrency,
    }


def test_selection_gate_and_both_failed() -> None:
    selected = select_model(
        {"a": _stats(gate=False), "b": _stats()},
        comparison={"ci_low": -0.1, "ci_high": 0.1, "mean_diff": 0.0},
        model_order=("a", "b"),
    )
    assert selected == {"selection": "b", "reason": "gate_failed"}

    failed = select_model(
        {"a": _stats(gate=False), "b": _stats(gate=False)},
        comparison={"ci_low": -0.1, "ci_high": 0.1, "mean_diff": 0.0},
        model_order=("a", "b"),
    )
    assert failed == {"selection": None, "reason": "gate_failed"}


def test_selection_uses_ci_then_cost_then_latency() -> None:
    stats = {"a": _stats(cost=3.0, latency=3.0), "b": _stats(cost=1.0, latency=1.0)}
    assert select_model(
        stats,
        comparison={"ci_low": 0.01, "ci_high": 0.2, "mean_diff": 0.1},
        model_order=("a", "b"),
    ) == {"selection": "a", "reason": "ci_excludes_zero"}
    assert select_model(
        stats,
        comparison={"ci_low": -0.01, "ci_high": 0.2, "mean_diff": 0.1},
        model_order=("a", "b"),
    ) == {"selection": "b", "reason": "cheaper_on_tie"}

    tied_cost = {"a": _stats(latency=3.0), "b": _stats(latency=1.0)}
    assert select_model(
        tied_cost,
        comparison={"ci_low": -0.1, "ci_high": 0.1, "mean_diff": 0.0},
        model_order=("a", "b"),
    ) == {"selection": "b", "reason": "latency_on_cost_tie"}


def test_selection_does_not_use_latency_when_concurrency_differs() -> None:
    stats = {
        "a": _stats(latency=3.0, concurrency=4),
        "b": _stats(latency=1.0, concurrency=2),
    }
    selected = select_model(
        stats,
        comparison={"ci_low": -0.1, "ci_high": 0.1, "mean_diff": 0.0},
        model_order=("a", "b"),
    )
    assert selected["selection"] is None
    assert selected["reason"] == "latency_unavailable_concurrency_mismatch"


def test_preregistered_guardrails_and_model_settings_are_unchanged() -> None:
    guardrails = GuardrailConfig()
    assert guardrails.record() == {
        "max_steps": 5,
        "max_tokens_per_question": 100_000,
        "request_timeout_s": 30,
        "simple_timeout_s": 45,
        "agentic_timeout_s": 120,
        "max_output_tokens": 4_000,
        "context_top_k": 8,
        "context_char_budget": 6_000,
    }
    configs = [load_answer_config("luna"), load_answer_config("sol")]
    _validate_model_comparison(configs)
    assert [config.model for config in configs] == ["gpt-6-luna", "gpt-6.1-sol"]
    assert all(config.effort == "low" for config in configs)
    assert all(config.temperature is None for config in configs)
    assert all(config.max_output_tokens == 4_000 for config in configs)
    assert all(config.request_timeout_s == 30 for config in configs)
    assert all(config.max_input_tokens == 272_000 for config in configs)


def test_dry_run_records_300_executions_expected_and_upper_costs() -> None:
    root = Path(__file__).resolve().parents[2]
    questions = load_m6_questions(root, split="dev")
    configs = [load_answer_config("luna"), load_answer_config("sol")]

    result = estimate_dev_dry_run(
        questions, configs, concurrency=4, remaining_budget_usd=100.0
    )

    assert result["execution_count"] == 300
    assert (
        result["models"]["gpt-6-luna"]["systems"]["simple"][  # type: ignore[index]
            "model_call_count"
        ]
        == 75
    )
    assert (
        result["models"]["gpt-6-luna"]["systems"]["agentic"][  # type: ignore[index]
            "model_call_count"
        ]
        == 300
    )
    assert (
        result["totals"]["upper_cost_usd"]
        > result["totals"][  # type: ignore[index]
            "estimated_cost_usd"
        ]
    )
    with pytest.raises(BudgetExceededError):
        estimate_dev_dry_run(
            questions, configs, concurrency=4, remaining_budget_usd=0.0
        )
