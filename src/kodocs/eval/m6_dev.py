"""M6 answer-model dry-run, dev execution, and preregistered selection."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path

from kodocs.agent.guardrails import GuardrailConfig
from kodocs.agent.prompts import (
    AGENTIC_TOOL_PROMPT,
    COMMON_SYSTEM_PROMPT,
    prompt_hashes,
)
from kodocs.agent.types import AgentRuntime, canonical_hash
from kodocs.config import load_settings
from kodocs.costs import (
    DEFAULT_LEDGER_PATH,
    PRICES,
    BudgetExceededError,
    CostLedger,
    budget_estimate_usd,
    estimate_usd,
)
from kodocs.eval.golden import Question
from kodocs.eval.m5_stats import BootstrapResult, paired_bootstrap
from kodocs.eval.m6_inputs import (
    M6_INPUTS_PATH,
    load_m6_questions,
    require_input_manifest,
)
from kodocs.eval.m6_metrics import (
    aggregate_by_system_model,
    answer_values_match,
    citation_precision,
    leak_check_ab,
    refusal_rate,
)
from kodocs.eval.m6_runner import (
    AnswerCallable,
    ModelConfigLike,
    RuntimeFactory,
    require_m5_config,
    run_answers,
    serialize_embedder,
)
from kodocs.ingest.manifest import append_record, now_iso, sha256_file
from kodocs.llm.answer_settings import AnswerModelConfig, load_answer_config
from kodocs.tools import build_tool_deps
from kodocs.tools.deps import M5_CONFIG_HASH, M5_CONFIG_PATH

MODEL_CONFIG_NAMES = ("luna", "sol")
SYSTEMS = ("simple", "agentic")
DEFAULT_CONCURRENCY = 4
DRY_RUN_PATH = Path("data/manifest/m6_dry_runs.jsonl")
DEV_MANIFEST_PATH = Path("data/manifest/m6_dev.json")
PILOT_MANIFEST_PATH = Path("data/manifest/m6_pilot.json")
DEV_RUNS_PATH = Path("data/interim/m6/dev/runs.jsonl")
PILOT_RUNS_PATH = Path("data/interim/m6/pilot/runs.jsonl")
CANARIES_PATH = Path("data/synthetic/canaries.json")
DOCS_PATH = Path("data/synthetic/docs")

EXPECTED_OUTPUT_TOKENS_PER_CALL = 1_000
AGENTIC_TOOL_TURNS = 3
EVIDENCE_CHARS_PER_RESULT = 6_000
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20260930


def select_model(
    model_stats: Mapping[str, Mapping[str, object]],
    *,
    comparison: Mapping[str, object] | BootstrapResult,
    model_order: Sequence[str],
) -> dict[str, str | None]:
    """Apply M6 rules 12-13 exactly, without inspecting answer text."""
    if len(model_order) != 2 or len(set(model_order)) != 2:
        raise ValueError("model_order must contain exactly two distinct models")
    if any(model not in model_stats for model in model_order):
        raise ValueError("model_stats is missing a compared model")
    candidates = [
        model for model in model_order if bool(model_stats[model].get("gate_passed"))
    ]
    if not candidates:
        return {"selection": None, "reason": "gate_failed"}
    if len(candidates) == 1:
        return {"selection": candidates[0], "reason": "gate_failed"}

    ci_low = _comparison_number(comparison, "ci_low")
    ci_high = _comparison_number(comparison, "ci_high")
    mean_diff = _comparison_number(comparison, "mean_diff")
    left, right = model_order
    if ci_low > 0 or ci_high < 0:
        return {
            "selection": left if mean_diff > 0 else right,
            "reason": "ci_excludes_zero",
        }

    left_cost = _stat_number(model_stats[left], "mean_cost_usd")
    right_cost = _stat_number(model_stats[right], "mean_cost_usd")
    if not math.isclose(left_cost, right_cost, rel_tol=0.0, abs_tol=1e-12):
        return {
            "selection": left if left_cost < right_cost else right,
            "reason": "cheaper_on_tie",
        }

    left_concurrency = model_stats[left].get("concurrency")
    right_concurrency = model_stats[right].get("concurrency")
    if left_concurrency != right_concurrency:
        return {
            "selection": None,
            "reason": "latency_unavailable_concurrency_mismatch",
        }
    left_latency = _stat_number(model_stats[left], "latency_p50_s")
    right_latency = _stat_number(model_stats[right], "latency_p50_s")
    if math.isclose(left_latency, right_latency, rel_tol=0.0, abs_tol=1e-12):
        return {"selection": None, "reason": "complete_tie"}
    return {
        "selection": left if left_latency < right_latency else right,
        "reason": "latency_on_cost_tie",
    }


def estimate_dev_dry_run(
    questions: Sequence[Question],
    model_configs: Sequence[AnswerModelConfig],
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    remaining_budget_usd: float | None = None,
) -> dict[str, object]:
    """Estimate the fixed 300-call dev comparison without creating a client."""
    if len(questions) != 75:
        raise ValueError(f"M6 dry-run requires 75 dev questions, got {len(questions)}")
    if len(model_configs) != 2:
        raise ValueError("M6 dry-run requires exactly two answer models")
    _require_positive_int(concurrency, "concurrency")
    _validate_model_comparison(model_configs)
    guardrails = GuardrailConfig()
    if guardrails.max_tokens_per_question > min(
        config.max_input_tokens for config in model_configs
    ):
        raise ValueError("upper-bound input exceeds the 272K standard-price tier")

    by_model: dict[str, dict[str, object]] = {}
    expected_guarded_total = 0.0
    expected_total = 0.0
    upper_guarded_total = 0.0
    upper_total = 0.0
    for config in model_configs:
        per_system: dict[str, object] = {}
        for system in SYSTEMS:
            input_tokens = sum(
                _expected_input_tokens(question, system) for question in questions
            )
            output_calls = 1 if system == "simple" else AGENTIC_TOOL_TURNS + 1
            output_tokens = (
                len(questions) * output_calls * EXPECTED_OUTPUT_TOKENS_PER_CALL
            )
            if (
                _maximum_single_request_chars(questions, system)
                > config.max_input_tokens
            ):
                raise ValueError(
                    f"{config.model} {system} estimated request exceeds 272K tokens"
                )
            expected_cost = _token_cost(config.model, input_tokens, output_tokens)
            expected_guarded = _guarded_token_cost(
                config.model, input_tokens, output_tokens
            )
            upper_input = len(questions) * guardrails.max_tokens_per_question
            upper_output = len(questions) * guardrails.max_output_tokens
            upper_cost = _token_cost(config.model, upper_input, upper_output)
            upper_guarded = _guarded_token_cost(config.model, upper_input, upper_output)
            per_system[system] = {
                "question_count": len(questions),
                "model_call_count": len(questions) * output_calls,
                "estimated_input_tokens": input_tokens,
                "estimated_output_tokens": output_tokens,
                "estimated_cost_usd": expected_cost,
                "guarded_estimated_cost_usd": expected_guarded,
                "upper_input_tokens": upper_input,
                "upper_output_tokens": upper_output,
                "upper_cost_usd": upper_cost,
                "guarded_upper_cost_usd": upper_guarded,
            }
            expected_total += expected_cost
            expected_guarded_total += expected_guarded
            upper_total += upper_cost
            upper_guarded_total += upper_guarded
        input_price = PRICES[f"openai.{config.model}.input"]
        output_price = PRICES[f"openai.{config.model}.output"]
        by_model[config.model] = {
            "pricing": {
                "input_usd_per_million": input_price.unit_price_usd * 1_000_000,
                "output_usd_per_million": output_price.unit_price_usd * 1_000_000,
                "input_billing_margin": input_price.billing_margin,
                "output_billing_margin": output_price.billing_margin,
                "checked_on": input_price.checked_on,
            },
            "systems": per_system,
        }
    if (
        remaining_budget_usd is not None
        and expected_guarded_total > remaining_budget_usd
    ):
        raise BudgetExceededError(
            "M6 dev guarded estimate exceeds the remaining monthly budget"
        )
    checked_on = sorted(
        {PRICES[f"openai.{config.model}.input"].checked_on for config in model_configs}
    )
    return {
        "mode": "sync",
        "question_count": len(questions),
        "system_count": 2,
        "model_count": 2,
        "execution_count": len(questions) * 2 * 2,
        "concurrency": concurrency,
        "pricing_checked_on": checked_on,
        "assumptions": {
            "characters_per_input_token": 1,
            "simple_model_calls": 1,
            "agentic_tool_turns": AGENTIC_TOOL_TURNS,
            "agentic_final_calls": 1,
            "evidence_chars": EVIDENCE_CHARS_PER_RESULT,
            "expected_output_tokens_per_call": EXPECTED_OUTPUT_TOKENS_PER_CALL,
            "upper_input_tokens_per_question": guardrails.max_tokens_per_question,
            "upper_output_tokens_per_question": guardrails.max_output_tokens,
            "standard_tier_max_input_tokens": min(
                config.max_input_tokens for config in model_configs
            ),
        },
        "models": by_model,
        "totals": {
            "estimated_cost_usd": expected_total,
            "guarded_estimated_cost_usd": expected_guarded_total,
            "upper_cost_usd": upper_total,
            "guarded_upper_cost_usd": upper_guarded_total,
            "remaining_budget_usd": remaining_budget_usd,
        },
    }


def write_dry_run(
    *,
    root: Path = Path("."),
    concurrency: int = DEFAULT_CONCURRENCY,
    remaining_budget_usd: float | None = None,
    ran_at: str | None = None,
) -> dict[str, object]:
    """Append one commit-safe dry-run estimate after budget preflight."""
    require_m5_config(root / M5_CONFIG_PATH)
    require_input_manifest(root)
    questions = load_m6_questions(root, split="dev")
    configs = [load_answer_config(name) for name in MODEL_CONFIG_NAMES]
    if remaining_budget_usd is None:
        settings = load_settings()
        ledger = CostLedger(root / DEFAULT_LEDGER_PATH)
        remaining_budget_usd = settings.monthly_budget_usd - ledger.month_total_usd()
    payload = estimate_dev_dry_run(
        questions,
        configs,
        concurrency=concurrency,
        remaining_budget_usd=remaining_budget_usd,
    )
    payload.update(
        {
            "ran_at": ran_at or now_iso(),
            "dev_input_hash": _dev_input_hash(questions, configs),
            "m5_config_hash": M5_CONFIG_HASH,
        }
    )
    append_record(
        root / DRY_RUN_PATH,
        payload,
        required=(
            "ran_at",
            "mode",
            "dev_input_hash",
            "m5_config_hash",
            "concurrency",
            "assumptions",
            "totals",
        ),
    )
    return payload


def analyze_dev(
    questions: Sequence[Question],
    rows: Sequence[Mapping[str, object]],
    *,
    docs_meta: Mapping[str, Mapping[str, object]],
    canaries: Mapping[str, object],
    model_configs: Sequence[AnswerModelConfig],
    concurrency: int,
    init_s: float,
    input_manifest_sha256: str,
    code_commit: str | None,
) -> dict[str, object]:
    """Create the commit-safe dev comparison and model selection manifest."""
    if len(questions) != 75:
        raise ValueError("full M6 dev analysis requires 75 questions")
    models = tuple(config.model for config in model_configs)
    _require_matrix(rows, questions, models)
    _require_frozen_run_hashes(rows, model_configs, concurrency)
    denominator = [
        question
        for question in questions
        if question.expected_response == "answer" and question.answer_values
    ]
    if len(denominator) != 56:
        raise ValueError("M6 answer-match denominator must remain 56")
    by_key = {
        (str(row["question_id"]), str(row["system"]), str(row["model"])): row
        for row in rows
    }
    answer_metrics: dict[str, dict[str, object]] = defaultdict(dict)
    model_vectors: dict[str, list[float]] = {}
    model_stats: dict[str, dict[str, object]] = {}
    permission = [question for question in questions if question.type == "permission"]
    unanswerable_ids = {
        question.id for question in questions if question.type == "unanswerable"
    }
    if len(permission) != 8 or len(unanswerable_ids) != 8:
        raise ValueError("M6 dev permission/unanswerable counts changed")
    question_by_id = {question.id: question for question in questions}
    gate_results: dict[str, object] = {}
    refusal_results: dict[str, object] = {}
    citation_results: dict[str, object] = {}

    for model in models:
        vector: list[float] = []
        for question in denominator:
            values = [
                float(_row_answer_match(by_key[(question.id, system, model)], question))
                for system in SYSTEMS
            ]
            vector.append(math.fsum(values) / len(values))
        model_vectors[model] = vector
        for system in SYSTEMS:
            model_system_rows = [
                by_key[(question.id, system, model)] for question in questions
            ]
            target_rows = [
                by_key[(question.id, system, model)] for question in denominator
            ]
            matched = [
                _row_answer_match(row, question_by_id[str(row["question_id"])])
                for row in target_rows
            ]
            source_metrics = {}
            for source in ("real", "synthetic"):
                indexes = [
                    index
                    for index, question in enumerate(denominator)
                    if question.source == source
                ]
                numerator = sum(matched[index] for index in indexes)
                source_metrics[source] = {
                    "numerator": numerator,
                    "denominator": len(indexes),
                    "rate": numerator / len(indexes) if indexes else None,
                }
            answer_metrics[model][system] = {
                "numerator": sum(matched),
                "denominator": len(matched),
                "rate": sum(matched) / len(matched),
                "by_source": source_metrics,
            }
            refusal_results.setdefault(model, {})[system] = refusal_rate(
                model_system_rows, unanswerable_ids
            )
            citation_scores = []
            excluded = 0
            for row in model_system_rows:
                question = question_by_id[str(row["question_id"])]
                score = citation_precision(row, question.qrels)
                if score is None:
                    excluded += 1
                else:
                    citation_scores.append(score)
            citation_results.setdefault(model, {})[system] = {
                "mean": (
                    math.fsum(citation_scores) / len(citation_scores)
                    if citation_scores
                    else None
                ),
                "denominator": len(citation_scores),
                "excluded": excluded,
            }

        leak_rows = []
        a_count = 0
        b_count = 0
        for question in permission:
            for system in SYSTEMS:
                row = by_key[(question.id, system, model)]
                leak = leak_check_ab(row, question.asker, docs_meta, canaries)
                a_count += int(bool(leak["a"]))
                b_count += int(bool(leak["b"]))
                leak_rows.append(
                    {
                        "question_id": question.id,
                        "system": system,
                        "a": leak["a"],
                        "b": leak["b"],
                    }
                )
        gate_results[model] = {
            "permission_question_count": len(permission),
            "execution_count": len(leak_rows),
            "a_count": a_count,
            "b_count": b_count,
            "passed": a_count + b_count == 0,
            "results": leak_rows,
        }

        model_rows = [row for row in rows if row.get("model") == model]
        costs = [float(row.get("cost_usd", 0.0)) for row in model_rows]
        latencies = [float(row.get("latency_s", 0.0)) for row in model_rows]
        run_concurrency = {row.get("concurrency") for row in model_rows}
        model_stats[model] = {
            "gate_passed": gate_results[model]["passed"],  # type: ignore[index]
            "mean_answer_match": math.fsum(vector) / len(vector),
            "mean_cost_usd": math.fsum(costs) / len(model_rows),
            "latency_p50_s": statistics.median(latencies),
            "concurrency": (
                next(iter(run_concurrency)) if len(run_concurrency) == 1 else None
            ),
        }

    comparison_result = paired_bootstrap(
        model_vectors[models[0]],
        model_vectors[models[1]],
        n_boot=BOOTSTRAP_RESAMPLES,
        seed=BOOTSTRAP_SEED,
    )
    comparison = asdict(comparison_result)
    selected = select_model(
        model_stats,
        comparison=comparison_result,
        model_order=models,
    )
    settings = {
        config.model: {
            "settings": config.record(),
            "settings_hash": config.settings_hash(),
        }
        for config in model_configs
    }
    guardrails = GuardrailConfig()
    return {
        "schema_version": 1,
        "status": "completed",
        "question_count": len(questions),
        "systems": list(SYSTEMS),
        "concurrency": concurrency,
        "answer_match": dict(answer_metrics),
        "gate": gate_results,
        "unanswerable_refusal": refusal_results,
        "citation_precision": citation_results,
        "operations": aggregate_by_system_model(rows, init_s=init_s),
        "guardrails_summary": _guardrail_summary(rows),
        "model_stats": model_stats,
        "paired_bootstrap": {
            "left_model": models[0],
            "right_model": models[1],
            **comparison,
        },
        "selection": selected["selection"],
        "selection_reason": selected["reason"],
        "models": settings,
        "prompt_hashes": prompt_hashes(),
        "guardrails": {
            "settings": guardrails.record(),
            "settings_hash": guardrails.settings_hash(),
        },
        "m5_config_hash": M5_CONFIG_HASH,
        "m6_eval_inputs_sha256": input_manifest_sha256,
        "code_commit": code_commit,
    }


def run_dev(
    *,
    root: Path,
    runtime_factory: RuntimeFactory,
    model_configs: Sequence[AnswerModelConfig],
    init_s: float,
    concurrency: int = DEFAULT_CONCURRENCY,
    limit: int | None = None,
    answer_fn: AnswerCallable,
) -> dict[str, object]:
    """Run a pilot or the full dev matrix with injected runtimes."""
    require_m5_config(root / M5_CONFIG_PATH)
    inputs_path = root / M6_INPUTS_PATH
    require_input_manifest(root)
    if len(model_configs) != 2:
        raise ValueError("M6 dev requires exactly two answer models")
    _validate_model_comparison(model_configs)
    questions = load_m6_questions(root, split="dev")
    if len(questions) != 75:
        raise ValueError("M6 dev requires 75 questions")
    config_mapping = {config.model: config for config in model_configs}
    if limit is not None:
        _require_positive_int(limit, "limit")
        if limit > len(questions):
            raise ValueError("limit exceeds the dev question count")
        selected_questions = questions[:limit]
        output_path = root / PILOT_RUNS_PATH
    else:
        selected_questions = questions
        output_path = root / DEV_RUNS_PATH
    rows = run_answers(
        selected_questions,
        systems=SYSTEMS,
        model_configs=config_mapping,
        runtime_factory=runtime_factory,
        output_path=output_path,
        m5_config_hash=M5_CONFIG_HASH,
        concurrency=concurrency,
        answer_fn=answer_fn,
    )
    summary = {
        "schema_version": 1,
        "status": "completed",
        "question_count": len(selected_questions),
        "question_ids": [question.id for question in selected_questions],
        "systems": list(SYSTEMS),
        "models": [config.model for config in model_configs],
        "concurrency": concurrency,
        "guardrails_summary": _guardrail_summary(rows),
        "operations": aggregate_by_system_model(rows, init_s=init_s),
        "m5_config_hash": M5_CONFIG_HASH,
    }
    if limit is not None:
        _write_json_atomic(root / PILOT_MANIFEST_PATH, summary)
        return summary
    full = analyze_dev(
        questions,
        rows,
        docs_meta=_load_docs_meta(root / DOCS_PATH),
        canaries=_json_object(root / CANARIES_PATH),
        model_configs=model_configs,
        concurrency=concurrency,
        init_s=init_s,
        input_manifest_sha256=sha256_file(inputs_path),
        code_commit=_git_commit(root),
    )
    _write_json_atomic(root / DEV_MANIFEST_PATH, full)
    return full


def _guardrail_summary(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for system in SYSTEMS:
        system_rows = [row for row in rows if row.get("system") == system]
        reasons = Counter(str(row.get("refuse_reason")) for row in system_rows)
        result[system] = {
            "guardrail_max_steps": reasons["guardrail_max_steps"],
            "guardrail_tokens": reasons["guardrail_tokens"],
            "guardrail_timeout": reasons["guardrail_timeout"],
            "max_cumulative_tokens": max(
                (int(row.get("cumulative_tokens", 0)) for row in system_rows),
                default=0,
            ),
        }
    return result


def _expected_input_tokens(question: Question, system: str) -> int:
    if system == "simple":
        return (
            len(COMMON_SYSTEM_PROMPT)
            + len(question.question)
            + EVIDENCE_CHARS_PER_RESULT
        )
    prefix = (
        len(COMMON_SYSTEM_PROMPT) + len(AGENTIC_TOOL_PROMPT) + len(question.question)
    )
    tool_turns = sum(
        prefix + turn * EVIDENCE_CHARS_PER_RESULT for turn in range(AGENTIC_TOOL_TURNS)
    )
    final = (
        len(COMMON_SYSTEM_PROMPT) + len(question.question) + EVIDENCE_CHARS_PER_RESULT
    )
    return tool_turns + final


def _maximum_single_request_chars(questions: Sequence[Question], system: str) -> int:
    if system == "simple":
        return max(_expected_input_tokens(question, system) for question in questions)
    return max(
        len(COMMON_SYSTEM_PROMPT)
        + len(AGENTIC_TOOL_PROMPT)
        + len(question.question)
        + (AGENTIC_TOOL_TURNS - 1) * EVIDENCE_CHARS_PER_RESULT
        for question in questions
    )


def _token_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    return estimate_usd(f"openai.{model}.input", input_tokens) + estimate_usd(
        f"openai.{model}.output", output_tokens
    )


def _guarded_token_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    return budget_estimate_usd(
        f"openai.{model}.input", input_tokens
    ) + budget_estimate_usd(f"openai.{model}.output", output_tokens)


def _validate_model_comparison(configs: Sequence[AnswerModelConfig]) -> None:
    records = []
    for config in configs:
        record = config.record()
        record.pop("model")
        records.append(record)
    if any(record != records[0] for record in records[1:]):
        raise ValueError("M6 dev candidates may differ only by model ID")
    guardrails = GuardrailConfig()
    for config in configs:
        if config.max_output_tokens != guardrails.max_output_tokens:
            raise ValueError("model max_output_tokens differs from guardrails")
        if config.request_timeout_s != guardrails.request_timeout_s:
            raise ValueError("model request timeout differs from guardrails")
        if config.max_input_tokens != 272_000:
            raise ValueError("model max_input_tokens must remain 272000")


def _row_answer_match(row: Mapping[str, object], question: Question) -> bool:
    answer = row.get("answer")
    return (
        row.get("status") == "completed"
        and isinstance(answer, str)
        and answer_values_match(answer, question.answer_values)
    )


def _require_matrix(
    rows: Sequence[Mapping[str, object]],
    questions: Sequence[Question],
    models: Sequence[str],
) -> None:
    expected = {
        (question.id, system, model)
        for question in questions
        for system in SYSTEMS
        for model in models
    }
    actual = {
        (str(row.get("question_id")), str(row.get("system")), str(row.get("model")))
        for row in rows
    }
    if len(rows) != len(expected) or actual != expected:
        raise RuntimeError("M6 dev result matrix is incomplete or duplicated")


def _require_frozen_run_hashes(
    rows: Sequence[Mapping[str, object]],
    configs: Sequence[AnswerModelConfig],
    concurrency: int,
) -> None:
    expected_settings = {config.model: config.settings_hash() for config in configs}
    prompts = prompt_hashes()
    guard_hash = GuardrailConfig().settings_hash()
    for row in rows:
        model = row.get("model")
        if row.get("model_settings_hash") != expected_settings.get(model):
            raise RuntimeError("M6 dev run model settings hash mismatch")
        if (
            row.get("common_prompt_hash") != prompts["common"]
            or row.get("agentic_prompt_hash") != prompts["agentic"]
        ):
            raise RuntimeError("M6 dev run prompt hash mismatch")
        if row.get("guardrail_hash") != guard_hash:
            raise RuntimeError("M6 dev run guardrail hash mismatch")
        if row.get("m5_config_hash") != M5_CONFIG_HASH:
            raise RuntimeError("M6 dev run M5 config hash mismatch")
        if row.get("concurrency") != concurrency:
            raise RuntimeError("M6 dev run concurrency mismatch")


def _dev_input_hash(
    questions: Sequence[Question], configs: Sequence[AnswerModelConfig]
) -> str:
    return canonical_hash(
        {
            "question_ids": [question.id for question in questions],
            "systems": list(SYSTEMS),
            "models": [config.record() for config in configs],
            "prompts": prompt_hashes(),
            "guardrails": GuardrailConfig().record(),
            "m5_config_hash": M5_CONFIG_HASH,
        }
    )


def _require_dry_run(
    root: Path,
    questions: Sequence[Question],
    configs: Sequence[AnswerModelConfig],
    concurrency: int,
) -> None:
    path = root / DRY_RUN_PATH
    if not path.exists():
        raise ValueError("make m6-dry must run before make m6-dev")
    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    expected_hash = _dev_input_hash(questions, configs)
    if not any(
        isinstance(record, Mapping)
        and record.get("dev_input_hash") == expected_hash
        and record.get("concurrency") == concurrency
        for record in records
    ):
        raise ValueError("matching m6-dry record is missing for this dev run")


def _load_docs_meta(path: Path) -> dict[str, Mapping[str, object]]:
    documents: dict[str, Mapping[str, object]] = {}
    for document_path in sorted(path.glob("*.json")):
        document = _json_object(document_path)
        doc_id = document.get("doc_id")
        if isinstance(doc_id, str):
            documents[doc_id] = document
    return documents


def _comparison_number(
    value: Mapping[str, object] | BootstrapResult, key: str
) -> float:
    raw = value.get(key) if isinstance(value, Mapping) else getattr(value, key)
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise TypeError(f"comparison {key} must be numeric")
    return float(raw)


def _stat_number(value: Mapping[str, object], key: str) -> float:
    raw = value.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise TypeError(f"model stat {key} must be numeric")
    return float(raw)


def _json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON object required: {path}")
    return value


def _write_json_atomic(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _git_commit(root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() or None


def _require_positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M6 답변 모델 dev 비교를 실행합니다.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--opensearch-url", default="http://127.0.0.1:9200")
    parser.add_argument("--model-path")
    args = parser.parse_args(argv)
    try:
        if args.dry_run:
            payload = write_dry_run(root=args.root, concurrency=args.concurrency)
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            return 0

        configs = [load_answer_config(name) for name in MODEL_CONFIG_NAMES]
        questions = load_m6_questions(args.root, split="dev")
        _require_dry_run(args.root, questions, configs, args.concurrency)

        # Real clients and the local embedder are intentionally created only here.
        from kodocs.agent.answer import answer as real_answer
        from kodocs.llm.openai_chat import ManagedChat

        require_m5_config(args.root / M5_CONFIG_PATH)
        settings = load_settings()
        started = time.perf_counter()
        deps = build_tool_deps(
            root=args.root,
            base_url=args.opensearch_url,
            model_path_or_id=args.model_path,
        )
        init_s = time.perf_counter() - started
        deps = serialize_embedder(deps)

        def runtime_factory(_name: str, config: ModelConfigLike) -> AgentRuntime:
            if not isinstance(config, AnswerModelConfig):
                raise TypeError("CLI requires AnswerModelConfig")
            chat = ManagedChat(
                config,
                settings,
                ledger_path=args.root / DEFAULT_LEDGER_PATH,
                run_id="m6-dev",
            )
            return AgentRuntime(
                deps=deps,
                chat=chat,
                model_record={
                    **config.record(),
                    "settings_hash": config.settings_hash(),
                },
                m5_config_hash=M5_CONFIG_HASH,
                trace_dir=args.root / "logs/traces",
            )

        try:
            result = run_dev(
                root=args.root,
                runtime_factory=runtime_factory,
                model_configs=configs,
                init_s=init_s,
                concurrency=args.concurrency,
                limit=args.limit,
                answer_fn=real_answer,
            )
        finally:
            deps.client.close()
        guard_summary = result["guardrails_summary"]
        print(
            "M6 dev 완료: "
            f"questions={result['question_count']} "
            f"guardrails={json.dumps(guard_summary, ensure_ascii=False, sort_keys=True)}",
            flush=True,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - bounded CLI failure report
        print(f"M6 dev 실행 실패: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
