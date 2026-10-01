"""Shared, resumable M6 answer execution loop."""

from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import asdict, is_dataclass, replace
from pathlib import Path
from typing import Protocol

from kodocs.agent.answer import AnswerSystem, answer
from kodocs.agent.finalize import finalizer_hash
from kodocs.agent.guardrails import GuardrailConfig
from kodocs.agent.prompts import prompt_hashes
from kodocs.agent.types import AgentRuntime, AnswerResult, canonical_hash
from kodocs.costs import BudgetExceededError
from kodocs.eval.golden import Question
from kodocs.llm.openai_chat import BillingUncertainError, FatalLLMError
from kodocs.tools import ToolDeps, UserContext
from kodocs.tools.deps import M5_CONFIG_HASH

_STATUS_VALUES = frozenset({"completed", "billing_uncertain", "infra_error"})
_TERMINAL_CACHE_STATUSES = frozenset({"completed", "billing_uncertain"})
_TOKEN_FIELDS = ("input", "cached_input", "cache_write", "output", "reasoning")
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+\S+")
_OPENAI_KEY_VALUE = re.compile(r"\bsk-[A-Za-z0-9_-]+")
_WRITE_LOCK = threading.Lock()


class ModelConfigLike(Protocol):
    """The non-secret answer setting surface used by the runner."""

    model: str

    def record(self) -> dict[str, object]: ...

    def settings_hash(self) -> str: ...


AnswerCallable = Callable[..., AnswerResult]
RuntimeFactory = Callable[[str, ModelConfigLike], AgentRuntime]
PreCallCheck = Callable[[Question, AnswerSystem, ModelConfigLike], None]


def run_answers(
    questions: Sequence[Question],
    *,
    systems: Sequence[AnswerSystem],
    model_configs: Mapping[str, ModelConfigLike],
    runtime_factory: RuntimeFactory,
    output_path: Path,
    m5_config_hash: str,
    concurrency: int = 4,
    run_seq: int = 1,
    progress_every: int = 10,
    answer_fn: AnswerCallable = answer,
    pre_call_check: PreCallCheck | None = None,
    h5_pool_ids: set[str] | frozenset[str] = frozenset(),
) -> list[dict[str, object]]:
    """Execute or resume a question/system/model matrix and append one line per try."""
    _require_positive_int(concurrency, "concurrency")
    _require_positive_int(run_seq, "run_seq")
    _require_positive_int(progress_every, "progress_every")
    if m5_config_hash != M5_CONFIG_HASH:
        raise ValueError("M5 config hash does not match the frozen M6 configuration")
    if not questions:
        raise ValueError("questions must not be empty")
    if len({question.id for question in questions}) != len(questions):
        raise ValueError("question IDs must be unique")
    if not systems or any(system not in {"simple", "agentic"} for system in systems):
        raise ValueError("systems must contain simple or agentic")
    if len(set(systems)) != len(systems):
        raise ValueError("systems must not contain duplicates")
    if not model_configs:
        raise ValueError("model_configs must not be empty")

    prompts = prompt_hashes()
    runtimes: dict[str, AgentRuntime] = {}
    specs: list[tuple[str, ModelConfigLike, str]] = []
    guard_hash: str | None = None
    for config_name, config in model_configs.items():
        model = _model_id(config)
        settings_hash = _settings_hash(config)
        runtime = runtime_factory(config_name, config)
        if runtime.m5_config_hash != m5_config_hash:
            raise ValueError("AgentRuntime M5 config hash mismatch")
        current_guard_hash = runtime.guardrails.settings_hash()
        if guard_hash is None:
            guard_hash = current_guard_hash
        elif guard_hash != current_guard_hash:
            raise ValueError("models must use identical guardrail settings")
        if model in runtimes:
            raise ValueError(f"duplicate answer model ID: {model}")
        runtimes[model] = runtime
        specs.append((model, config, settings_hash))
    assert guard_hash is not None
    if guard_hash != GuardrailConfig().settings_hash():
        raise ValueError("runtime guardrails differ from the preregistered M6 settings")

    existing = _load_records(output_path)
    terminal: dict[str, dict[str, object]] = {}
    infra_counts: Counter[str] = Counter()
    for row in existing:
        cache_key = row.get("cache_key")
        status = row.get("status")
        if not isinstance(cache_key, str) or status not in _STATUS_VALUES:
            continue
        if status == "infra_error":
            infra_counts[cache_key] += 1
        elif status in _TERMINAL_CACHE_STATUSES:
            terminal[cache_key] = row

    jobs: list[tuple[Question, AnswerSystem, str, ModelConfigLike, str, str]] = []
    ordered_keys: list[str] = []
    selected_rows: dict[str, dict[str, object]] = {}
    for question in questions:
        for system in systems:
            for model, config, settings_hash in specs:
                cache_key = run_cache_key(
                    question_id=question.id,
                    system=system,
                    model_settings_hash=settings_hash,
                    common_prompt_hash=prompts["common"],
                    agentic_prompt_hash=prompts["agentic"],
                    guardrail_hash=guard_hash,
                    m5_config_hash=m5_config_hash,
                    run_seq=run_seq,
                    finalizer_hash=finalizer_hash(),
                )
                ordered_keys.append(cache_key)
                if cache_key in terminal:
                    selected_rows[cache_key] = terminal[cache_key]
                else:
                    jobs.append(
                        (question, system, model, config, settings_hash, cache_key)
                    )

    total = len(ordered_keys)
    completed_count = len(selected_rows)
    if jobs:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures: dict[Future[dict[str, object]], str] = {}
            for job in jobs:
                future = executor.submit(
                    _run_one_and_append,
                    job,
                    output_path=output_path,
                    runtime=runtimes[job[2]],
                    answer_fn=answer_fn,
                    pre_call_check=pre_call_check,
                    prompts=prompts,
                    guard_hash=guard_hash,
                    m5_config_hash=m5_config_hash,
                    concurrency=concurrency,
                    run_seq=run_seq,
                    infra_attempt=infra_counts[job[5]] + 1,
                    h5_pool_ids=h5_pool_ids,
                )
                futures[future] = job[5]
            try:
                for future in as_completed(futures):
                    cache_key = futures[future]
                    row = future.result()
                    selected_rows[cache_key] = row
                    completed_count += 1
                    if (
                        completed_count % progress_every == 0
                        or completed_count == total
                    ):
                        print(
                            f"M6 progress completed={completed_count}/{total}",
                            flush=True,
                        )
            except (FatalLLMError, BudgetExceededError):
                for pending in futures:
                    pending.cancel()
                raise
    return [selected_rows[key] for key in ordered_keys if key in selected_rows]


def run_cache_key(
    *,
    question_id: str,
    system: str,
    model_settings_hash: str,
    common_prompt_hash: str,
    agentic_prompt_hash: str,
    guardrail_hash: str,
    m5_config_hash: str,
    run_seq: int,
    finalizer_hash: str,
) -> str:
    """Hash every preregistered cache dimension without storing model inputs."""
    return canonical_hash(
        {
            "question_id": question_id,
            "system": system,
            "model_settings_hash": model_settings_hash,
            "common_prompt_hash": common_prompt_hash,
            "agentic_prompt_hash": agentic_prompt_hash,
            "guardrail_hash": guardrail_hash,
            "m5_config_hash": m5_config_hash,
            "run_seq": run_seq,
            "finalizer_hash": finalizer_hash,
        }
    )


def require_m5_config(path: Path) -> Mapping[str, object]:
    """Load and enforce ADR-029's immutable M5 hash before initialization."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise TypeError(f"M5 config must be a JSON object: {path}")
    if value.get("config_hash") != M5_CONFIG_HASH:
        raise ValueError("M5 config hash does not match the frozen M6 configuration")
    return value


class LockedEmbedder:
    """Serialize local model/tokenizer calls for thread-safe shared use."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self._lock = threading.Lock()

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def prepare(self) -> float:
        with self._lock:
            return float(self._inner.prepare())  # type: ignore[attr-defined]

    def count_tokens(self, texts: Sequence[str]) -> list[int]:
        with self._lock:
            return self._inner.count_tokens(texts)  # type: ignore[attr-defined,no-any-return]

    def embed(self, texts: Sequence[str], *, kind: str) -> list[list[float]]:
        with self._lock:
            return self._inner.embed(texts, kind=kind)  # type: ignore[attr-defined,no-any-return]

    def embed_allowing_truncation(
        self, texts: Sequence[str], *, kind: str
    ) -> list[list[float]]:
        with self._lock:
            return self._inner.embed_allowing_truncation(  # type: ignore[attr-defined,no-any-return]
                texts, kind=kind
            )


def serialize_embedder(deps: ToolDeps) -> ToolDeps:
    """Return the same dependency bundle with embedding calls locked."""
    return replace(deps, embedder=LockedEmbedder(deps.embedder))


def _run_one(
    job: tuple[Question, AnswerSystem, str, ModelConfigLike, str, str],
    *,
    runtime: AgentRuntime,
    answer_fn: AnswerCallable,
    pre_call_check: PreCallCheck | None,
    prompts: Mapping[str, str],
    guard_hash: str,
    m5_config_hash: str,
    concurrency: int,
    run_seq: int,
    infra_attempt: int,
    h5_pool_ids: set[str] | frozenset[str],
) -> dict[str, object]:
    question, system, model, config, settings_hash, cache_key = job
    started = time.perf_counter()
    if pre_call_check is not None:
        pre_call_check(question, system, config)
    try:
        result = answer_fn(
            question.question,
            UserContext(question.asker.team, question.asker.grade),
            system=system,
            runtime=runtime,
            question_id=question.id,
        )
    except BillingUncertainError as exc:
        return _failure_record(
            question=question,
            system=system,
            model=model,
            settings_hash=settings_hash,
            cache_key=cache_key,
            status="billing_uncertain",
            elapsed_s=time.perf_counter() - started,
            attempts=exc.attempts,
            prompts=prompts,
            guard_hash=guard_hash,
            m5_config_hash=m5_config_hash,
            concurrency=concurrency,
            run_seq=run_seq,
            infra_attempt=infra_attempt,
            h5_pool_ids=h5_pool_ids,
        )
    except (FatalLLMError, BudgetExceededError):
        raise
    except Exception as exc:  # noqa: BLE001 - one question must not stop the matrix
        return _failure_record(
            question=question,
            system=system,
            model=model,
            settings_hash=settings_hash,
            cache_key=cache_key,
            status="infra_error",
            elapsed_s=time.perf_counter() - started,
            attempts=getattr(exc, "attempts", ()),
            prompts=prompts,
            guard_hash=guard_hash,
            m5_config_hash=m5_config_hash,
            concurrency=concurrency,
            run_seq=run_seq,
            infra_attempt=infra_attempt,
            h5_pool_ids=h5_pool_ids,
        )
    return _completed_record(
        question=question,
        system=system,
        model=model,
        settings_hash=settings_hash,
        cache_key=cache_key,
        result=result,
        elapsed_s=time.perf_counter() - started,
        prompts=prompts,
        guard_hash=guard_hash,
        m5_config_hash=m5_config_hash,
        concurrency=concurrency,
        run_seq=run_seq,
        infra_attempt=infra_attempt,
        h5_pool_ids=h5_pool_ids,
    )


def _run_one_and_append(
    job: tuple[Question, AnswerSystem, str, ModelConfigLike, str, str],
    *,
    output_path: Path,
    **kwargs: object,
) -> dict[str, object]:
    """Persist each non-fatal result in its worker before another future can abort."""
    row = _run_one(job, **kwargs)  # type: ignore[arg-type]
    _append_record(output_path, row)
    return row


def _completed_record(
    *,
    question: Question,
    system: AnswerSystem,
    model: str,
    settings_hash: str,
    cache_key: str,
    result: AnswerResult,
    elapsed_s: float,
    prompts: Mapping[str, str],
    guard_hash: str,
    m5_config_hash: str,
    concurrency: int,
    run_seq: int,
    infra_attempt: int,
    h5_pool_ids: set[str] | frozenset[str],
) -> dict[str, object]:
    trace = result.trace if isinstance(result.trace, Mapping) else {}
    model_steps = [
        step
        for step in _trace_steps(trace)
        if isinstance(step, Mapping) and step.get("kind") == "model"
    ]
    token_totals = {name: 0 for name in _TOKEN_FIELDS}
    attempts: list[object] = []
    for step in model_steps:
        raw_tokens = step.get("tokens", {})
        if isinstance(raw_tokens, Mapping):
            for name in _TOKEN_FIELDS:
                value = raw_tokens.get(name, 0)
                if (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                ):
                    token_totals[name] += value
        raw_attempts = step.get("attempts", ())
        if isinstance(raw_attempts, Sequence) and not isinstance(
            raw_attempts, (str, bytes)
        ):
            attempts.extend(raw_attempts)
    timings = trace.get("timings", {})
    guardrail = trace.get("guardrail", {})
    trace_id = trace.get("trace_id")
    return {
        **_base_record(
            question=question,
            system=system,
            model=model,
            settings_hash=settings_hash,
            cache_key=cache_key,
            status="completed",
            prompts=prompts,
            guard_hash=guard_hash,
            m5_config_hash=m5_config_hash,
            concurrency=concurrency,
            run_seq=run_seq,
            infra_attempt=infra_attempt,
            h5_pool_ids=h5_pool_ids,
        ),
        "answer": _redact(result.answer),
        "citations": [
            citation.model_dump(mode="json") for citation in result.citations
        ],
        "dropped_citations": _dropped_citations(trace),
        "refused": result.refused,
        "refuse_reason": result.refuse_reason,
        "tokens": token_totals,
        "cost_usd": sum(float(step.get("cost_usd", 0.0)) for step in model_steps),
        "latency_s": _timing_seconds(timings, "total_ms", elapsed_s),
        "embed_s": _timing_seconds(timings, "embed_ms", None),
        "search_s": _timing_seconds(timings, "search_ms", None),
        "steps": _non_negative_mapping_int(guardrail, "steps_used"),
        "cumulative_tokens": _non_negative_mapping_int(guardrail, "tokens_used"),
        "trace_id": trace_id if isinstance(trace_id, str) else None,
        "attempts": _json_attempts(attempts),
        "tool_refs": _tool_refs(trace),
    }


def _failure_record(
    *,
    question: Question,
    system: AnswerSystem,
    model: str,
    settings_hash: str,
    cache_key: str,
    status: str,
    elapsed_s: float,
    attempts: Sequence[object],
    prompts: Mapping[str, str],
    guard_hash: str,
    m5_config_hash: str,
    concurrency: int,
    run_seq: int,
    infra_attempt: int,
    h5_pool_ids: set[str] | frozenset[str],
) -> dict[str, object]:
    return {
        **_base_record(
            question=question,
            system=system,
            model=model,
            settings_hash=settings_hash,
            cache_key=cache_key,
            status=status,
            prompts=prompts,
            guard_hash=guard_hash,
            m5_config_hash=m5_config_hash,
            concurrency=concurrency,
            run_seq=run_seq,
            infra_attempt=infra_attempt,
            h5_pool_ids=h5_pool_ids,
        ),
        "answer": "",
        "citations": [],
        "dropped_citations": [],
        "refused": False,
        "refuse_reason": None,
        "tokens": {name: 0 for name in _TOKEN_FIELDS},
        "cost_usd": 0.0,
        "latency_s": max(0.0, elapsed_s),
        "embed_s": None,
        "search_s": None,
        "steps": 0,
        "cumulative_tokens": 0,
        "trace_id": None,
        "attempts": _json_attempts(attempts),
        "tool_refs": [],
    }


def _base_record(
    *,
    question: Question,
    system: str,
    model: str,
    settings_hash: str,
    cache_key: str,
    status: str,
    prompts: Mapping[str, str],
    guard_hash: str,
    m5_config_hash: str,
    concurrency: int,
    run_seq: int,
    infra_attempt: int,
    h5_pool_ids: set[str] | frozenset[str],
) -> dict[str, object]:
    return {
        "question_id": question.id,
        "system": system,
        "model": model,
        "model_settings_hash": settings_hash,
        "run_seq": run_seq,
        "status": status,
        "cache_key": cache_key,
        "common_prompt_hash": prompts["common"],
        "agentic_prompt_hash": prompts["agentic"],
        "guardrail_hash": guard_hash,
        "m5_config_hash": m5_config_hash,
        "finalizer_hash": finalizer_hash(),
        "concurrency": concurrency,
        "infra_attempt": infra_attempt,
        "h5_pool": question.id in h5_pool_ids,
    }


def _model_id(config: ModelConfigLike) -> str:
    value = getattr(config, "model", None)
    if not isinstance(value, str) or not value:
        raise TypeError("model config must contain a model ID")
    return value


def _settings_hash(config: ModelConfigLike) -> str:
    method = getattr(config, "settings_hash", None)
    if callable(method):
        value = method()
        if isinstance(value, str) and value:
            return value
    record_method = getattr(config, "record", None)
    if not callable(record_method):
        raise TypeError("model config must expose record() or settings_hash()")
    record = record_method()
    if not isinstance(record, Mapping):
        raise TypeError("model config record must be an object")
    return canonical_hash(record)


def _dropped_citations(trace: Mapping[str, object]) -> list[object]:
    value = trace.get("dropped_citations", ())
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return list(value)


def _trace_steps(trace: Mapping[str, object]) -> Sequence[object]:
    value = trace.get("steps", ())
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return value


def _tool_refs(trace: Mapping[str, object]) -> list[dict[str, object]]:
    refs: list[dict[str, object]] = []
    for step in _trace_steps(trace):
        if not isinstance(step, Mapping) or step.get("kind") != "tool":
            continue
        raw_refs = step.get("refs", ())
        if not isinstance(raw_refs, Sequence) or isinstance(raw_refs, (str, bytes)):
            continue
        for ref in raw_refs:
            if isinstance(ref, Mapping):
                refs.append({str(key): value for key, value in ref.items()})
    return refs


def _timing_seconds(value: object, key: str, fallback: float | None) -> float | None:
    if isinstance(value, Mapping):
        raw = value.get(key)
        if isinstance(raw, int | float) and not isinstance(raw, bool) and raw >= 0:
            return float(raw) / 1000.0
    return fallback


def _non_negative_mapping_int(value: object, key: str) -> int:
    if isinstance(value, Mapping):
        raw = value.get(key, 0)
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            return raw
    return 0


def _json_attempts(values: Sequence[object]) -> list[object]:
    result: list[object] = []
    for value in values:
        if is_dataclass(value) and not isinstance(value, type):
            result.append(asdict(value))
        elif isinstance(value, Mapping):
            result.append(dict(value))
        else:
            result.append(type(value).__name__)
    return result


def _load_records(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    result = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid M6 runs JSONL at line {line_number}: {exc.msg}"
                ) from exc
            if not isinstance(value, dict):
                raise TypeError(f"M6 runs line {line_number} must be an object")
            result.append(value)
    return result


def _append_record(path: Path, row: Mapping[str, object]) -> None:
    serialized = json.dumps(
        row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    with _WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as destination:
            destination.write(serialized + "\n")
            destination.flush()


def _redact(value: str) -> str:
    return _OPENAI_KEY_VALUE.sub("[REDACTED]", _BEARER_VALUE.sub("[REDACTED]", value))


def _require_positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value
