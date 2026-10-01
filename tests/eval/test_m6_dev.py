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


def _dev_rows(questions, configs, *, finalizer=None):
    from kodocs.agent.finalize import finalizer_hash
    from kodocs.agent.prompts import prompt_hashes
    from kodocs.tools.deps import M5_CONFIG_HASH

    prompts = prompt_hashes()
    rows = []
    for question in questions:
        relevant = sorted({q.doc_id for q in question.qrels if q.rel >= 1})
        for config in configs:
            for system in ("simple", "agentic"):
                cited = []
                # Only luna cites, and only in the agentic system with a wrong doc
                # for half of its questions, so the per-model means must differ.
                if config.model == "gpt-6-luna" and relevant:
                    cited = [relevant[0]]
                    if system == "agentic" and question.id.endswith(("1", "3", "5")):
                        cited.append("not-relevant")
                rows.append(
                    {
                        "question_id": question.id,
                        "system": system,
                        "model": config.model,
                        "model_settings_hash": config.settings_hash(),
                        "common_prompt_hash": prompts["common"],
                        "agentic_prompt_hash": prompts["agentic"],
                        "guardrail_hash": GuardrailConfig().settings_hash(),
                        "m5_config_hash": M5_CONFIG_HASH,
                        "finalizer_hash": finalizer or finalizer_hash(),
                        "concurrency": 4,
                        "status": "completed",
                        "answer": "답",
                        "citations": [{"kind": "doc", "doc_id": d} for d in cited],
                        "dropped_citations": [],
                        "refused": False,
                        "refuse_reason": None,
                        "tool_refs": [],
                        "cost_usd": 0.01,
                        "latency_s": 1.0,
                        "tokens": {},
                        "steps": 1,
                        "cumulative_tokens": 10,
                        "attempts": [],
                    }
                )
    return rows


def test_citation_precision_is_computed_per_model_and_system() -> None:
    from kodocs.eval.m6_dev import analyze_dev

    questions = load_m6_questions(Path("."), split="dev")
    configs = [load_answer_config("luna"), load_answer_config("sol")]

    result = analyze_dev(
        questions,
        _dev_rows(questions, configs),
        docs_meta={},
        canaries={},
        model_configs=configs,
        concurrency=4,
        init_s=0.0,
        input_manifest_sha256="0" * 64,
        code_commit=None,
    )

    luna = result["citation_precision"]["gpt-6-luna"]
    sol = result["citation_precision"]["gpt-6.1-sol"]
    assert sol["simple"]["denominator"] == 0 and sol["simple"]["mean"] is None
    assert luna["simple"]["denominator"] > 0
    assert luna["simple"]["mean"] == 1.0
    assert 0.0 < luna["agentic"]["mean"] < 1.0
    assert luna["agentic"]["mean"] != luna["simple"]["mean"]


def test_dev_rows_from_an_older_finalizer_are_rejected() -> None:
    from kodocs.eval.m6_dev import analyze_dev

    questions = load_m6_questions(Path("."), split="dev")
    configs = [load_answer_config("luna"), load_answer_config("sol")]

    with pytest.raises(RuntimeError, match="finalizer"):
        analyze_dev(
            questions,
            _dev_rows(questions, configs, finalizer="0" * 64),
            docs_meta={},
            canaries={},
            model_configs=configs,
            concurrency=4,
            init_s=0.0,
            input_manifest_sha256="0" * 64,
            code_commit=None,
        )


def test_run_dev_refuses_to_mix_rows_from_an_older_finalizer(tmp_path: Path) -> None:
    from kodocs.eval.m6_dev import _require_current_finalizer_runs

    runs = tmp_path / "runs.jsonl"
    _require_current_finalizer_runs(runs)  # absent file is fine
    runs.write_text('{"finalizer_hash": "old"}\n', encoding="utf-8")

    with pytest.raises(RuntimeError, match="run-NN-invalid"):
        _require_current_finalizer_runs(runs)
