"""Run the frozen M6 gold test (plus the silver direction set) once under one lock."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from kodocs.agent.types import AgentRuntime
from kodocs.costs import DEFAULT_LEDGER_PATH
from kodocs.eval.golden import Question
from kodocs.eval.m6_config import CONFIG_PATH, verify_config
from kodocs.eval.m6_dev import SYSTEMS
from kodocs.eval.m6_inputs import (
    EXPECTED_H5_SILVER_DIRECTION_COUNT,
    EXPECTED_SILVER_TEST_COUNT,
    load_m6_questions,
    require_input_manifest,
)
from kodocs.eval.m6_metrics import (
    aggregate_by_system_model,
    answer_values_match,
    citation_precision,
    refusal_rate,
)
from kodocs.eval.m6_runner import (
    AnswerCallable,
    ModelConfigLike,
    RuntimeFactory,
    run_answers,
    serialize_embedder,
)
from kodocs.ingest.manifest import (
    append_record,
    latest_records,
    load_records,
    now_iso,
    sha256_file,
)
from kodocs.llm.answer_settings import AnswerModelConfig, load_answer_config
from kodocs.tools import build_tool_deps
from kodocs.tools.deps import M5_CONFIG_HASH

RUNS_PATH = Path("data/manifest/m6_test_runs.jsonl")
RESULTS_DIR = Path("data/manifest/m6_test_results")
INTERIM_DIR = Path("data/interim/m6/test")
DEFAULT_CONCURRENCY = 4


class PreflightError(RuntimeError):
    """A frozen M6 test prerequisite failed before local/API initialization."""


class TestLockError(PreflightError):
    """The first-run/rerun-reason contract was violated."""


@dataclass(frozen=True)
class Preflight:
    root: Path
    output_root: Path
    config: Mapping[str, object]
    seq: int
    run_id: str
    ran_at: str
    rerun_reason: str | None
    previous_result_sha256: str | None
    result_path: Path
    raw_dir: Path
    runs_path: Path


@dataclass(frozen=True)
class ExperimentOutput:
    """Commit-safe metrics and the hash of preserved local answer rows."""

    result: Mapping[str, object]
    runs_sha256: str


ExperimentFunction = Callable[[Preflight], ExperimentOutput]


def run_test(
    *,
    root: Path = Path("."),
    out_root: Path | None = None,
    rerun_reason: str | None = None,
    experiment_fn: ExperimentFunction | None = None,
    now_fn: Callable[[], str] = now_iso,
) -> dict[str, object]:
    """Validate, execute, and immutably record one M6 test attempt."""
    output_root = Path(out_root) if out_root is not None else root
    preflight = _preflight(
        root=Path(root),
        output_root=output_root,
        rerun_reason=rerun_reason,
        ran_at=now_fn(),
    )
    execute = experiment_fn
    if execute is None:
        raise TypeError("the CLI must inject the real M6 experiment")
    completed = False
    try:
        output = execute(preflight)
        if not isinstance(output, ExperimentOutput):
            raise TypeError("experiment_fn must return ExperimentOutput")
        result = {
            **dict(output.result),
            "seq": preflight.seq,
            "run_id": preflight.run_id,
            "ran_at": preflight.ran_at,
            "config_hash": preflight.config["config_hash"],
            "rerun_reason": preflight.rerun_reason,
            "previous_result_sha256": preflight.previous_result_sha256,
            "runs_sha256": output.runs_sha256,
        }
        _write_json_exclusive(preflight.result_path, result)
        result_hash = sha256_file(preflight.result_path)
        _append_completed(preflight, result_hash)
        completed = True
        return result
    except Exception as exc:
        if not completed:
            if preflight.result_path.exists():
                preflight.result_path.unlink()
            _append_failed(preflight, exc)
        raise


def execute_experiment(
    preflight: Preflight,
    *,
    runtime_factory: RuntimeFactory,
    model_config: AnswerModelConfig,
    init_s: float,
    answer_fn: AnswerCallable,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> ExperimentOutput:
    """Run gold test 30 and silver test 83 through both frozen systems once each.

    The gold set keeps its original files and result shape (H5 verdict input). The
    silver test set is auxiliary direction evidence (not a verdict): it is stored
    separately but runs in the same unit, so one lock and one result cover both.
    """
    questions = load_m6_questions(preflight.root, split="test", set_name="gold")
    if len(questions) != 30:
        raise PreflightError(f"M6 gold test count must be 30, got {len(questions)}")
    h5_ids = {
        question.id
        for question in questions
        if question.type in {"identifier", "sql", "multi_doc"}
    }
    if len(h5_ids) != 13:
        raise PreflightError(f"M6 H5 pool count must be 13, got {len(h5_ids)}")
    runs_path = preflight.raw_dir / "runs.jsonl"
    rows = run_answers(
        questions,
        systems=SYSTEMS,
        model_configs={model_config.model: model_config},
        runtime_factory=runtime_factory,
        output_path=runs_path,
        m5_config_hash=M5_CONFIG_HASH,
        concurrency=concurrency,
        run_seq=preflight.seq,
        answer_fn=answer_fn,
        h5_pool_ids=h5_ids,
    )
    expected = len(questions) * len(SYSTEMS)
    if len(rows) != expected:
        raise RuntimeError(f"M6 test matrix incomplete: {len(rows)}/{expected}")
    summary = analyze_test(
        questions,
        rows,
        init_s=init_s,
        concurrency=concurrency,
    )

    silver_questions = load_m6_questions(
        preflight.root, split="test", set_name="silver"
    )
    if len(silver_questions) != EXPECTED_SILVER_TEST_COUNT:
        raise PreflightError(
            f"M6 silver test count must be {EXPECTED_SILVER_TEST_COUNT}, "
            f"got {len(silver_questions)}"
        )
    direction_ids = [
        question.id
        for question in silver_questions
        if question.type in {"identifier", "multi_doc"}
    ]
    if len(direction_ids) != EXPECTED_H5_SILVER_DIRECTION_COUNT:
        raise PreflightError(
            "M6 silver direction pool count must be "
            f"{EXPECTED_H5_SILVER_DIRECTION_COUNT}, got {len(direction_ids)}"
        )
    silver_runs_path = preflight.raw_dir / "silver" / "runs.jsonl"
    silver_rows = run_answers(
        silver_questions,
        systems=SYSTEMS,
        model_configs={model_config.model: model_config},
        runtime_factory=runtime_factory,
        output_path=silver_runs_path,
        m5_config_hash=M5_CONFIG_HASH,
        concurrency=concurrency,
        run_seq=preflight.seq,
        answer_fn=answer_fn,
        h5_pool_ids=frozenset(),
    )
    silver_expected = len(silver_questions) * len(SYSTEMS)
    if len(silver_rows) != silver_expected:
        raise RuntimeError(
            f"M6 silver test matrix incomplete: {len(silver_rows)}/{silver_expected}"
        )
    summary["silver_test"] = analyze_test(
        silver_questions,
        silver_rows,
        init_s=init_s,
        concurrency=concurrency,
    )
    summary["set_counts"] = {
        "gold_test": {
            "question_count": len(questions),
            "execution_count": len(rows),
        },
        "silver_test": {
            "question_count": len(silver_questions),
            "execution_count": len(silver_rows),
        },
    }
    summary["silver_runs_sha256"] = sha256_file(silver_runs_path)
    return ExperimentOutput(result=summary, runs_sha256=sha256_file(runs_path))


def analyze_test(
    questions: Sequence[Question],
    rows: Sequence[Mapping[str, object]],
    *,
    init_s: float,
    concurrency: int,
) -> dict[str, object]:
    """Build ID/count/hash-safe deterministic test metrics (no P95 verdict)."""
    by_id = {question.id: question for question in questions}
    answer_target = {
        question.id
        for question in questions
        if question.expected_response == "answer" and question.answer_values
    }
    unanswerable = {
        question.id for question in questions if question.type == "unanswerable"
    }
    permission = {
        question.id for question in questions if question.type == "permission"
    }
    per_system: dict[str, object] = {}
    for system in SYSTEMS:
        system_rows = [row for row in rows if row.get("system") == system]
        matches = [
            answer_values_match(
                str(row.get("answer", "")),
                by_id[str(row["question_id"])].answer_values,
            )
            if row.get("status") == "completed"
            else False
            for row in system_rows
            if row.get("question_id") in answer_target
        ]
        citation_scores = []
        citation_excluded = 0
        for row in system_rows:
            question = by_id[str(row["question_id"])]
            score = citation_precision(row, question.qrels)
            if score is None:
                citation_excluded += 1
            else:
                citation_scores.append(score)
        per_system[system] = {
            "answer_match": {
                "numerator": sum(matches),
                "denominator": len(answer_target),
                "rate": sum(matches) / len(answer_target) if answer_target else None,
            },
            "citation_precision": {
                "mean": math.fsum(citation_scores) / len(citation_scores)
                if citation_scores
                else None,
                "denominator": len(citation_scores),
                "excluded": citation_excluded,
            },
            "unanswerable_refusal": refusal_rate(system_rows, unanswerable),
            "permission_refusal": refusal_rate(
                system_rows, permission, permission=True
            ),
        }
    guards = Counter(
        str(row.get("refuse_reason"))
        for row in rows
        if str(row.get("refuse_reason", "")).startswith("guardrail_")
    )
    statuses = Counter(str(row.get("status")) for row in rows)
    return {
        "schema_version": 1,
        "question_count": len(questions),
        "execution_count": len(rows),
        "concurrency": concurrency,
        "deterministic_metrics": per_system,
        "operations": aggregate_by_system_model(rows, init_s=init_s),
        "guardrail_terminations": dict(sorted(guards.items())),
        "status_counts": {
            name: statuses[name]
            for name in ("completed", "billing_uncertain", "infra_error")
        },
        "billing_uncertain_count": statuses["billing_uncertain"],
        "m5_config_hash": M5_CONFIG_HASH,
    }


def _preflight(
    *, root: Path, output_root: Path, rerun_reason: str | None, ran_at: str
) -> Preflight:
    config_path = root / CONFIG_PATH
    check = verify_config(config_path)
    if not check.ok:
        raise PreflightError(
            "M6 test 사전 검사 실패: config (" + ", ".join(check.mismatches) + ")"
        )
    config = _read_json_object(config_path)
    reason = _normalize_reason(rerun_reason)
    runs_path = output_root / RUNS_PATH
    records = load_records(runs_path)
    current = latest_records(records, key=_record_seq)
    completed = sorted(
        (record for record in current if record.get("status") == "completed"),
        key=_record_seq,
    )
    if completed and reason is None:
        raise TestLockError(
            "이전 실행이 있습니다. 재실행은 --rerun-reason이 필요합니다"
        )
    if not completed and reason is not None:
        raise TestLockError(
            "이전 성공 실행이 없습니다. 첫 실행에는 --rerun-reason을 지정할 수 없습니다"
        )
    previous_hash: str | None = None
    if completed:
        previous = completed[-1]
        previous_path_value = previous.get("result_path")
        if not isinstance(previous_path_value, str):
            raise PreflightError("이전 M6 test result_path가 없습니다")
        previous_path = output_root / previous_path_value
        if not previous_path.is_file():
            raise PreflightError("이전 M6 test 결과 파일이 없습니다")
        previous_hash = sha256_file(previous_path)
        if previous.get("result_sha256") != previous_hash:
            raise PreflightError("이전 M6 test 결과 파일 해시가 다릅니다")
    seq = max((_record_seq(record) for record in completed), default=0) + 1
    result_path = output_root / RESULTS_DIR / f"run-{seq:02d}.json"
    raw_dir = output_root / INTERIM_DIR / f"run-{seq:02d}"
    if result_path.exists():
        raise FileExistsError(f"M6 test result already exists: {result_path}")
    return Preflight(
        root=root,
        output_root=output_root,
        config=config,
        seq=seq,
        run_id=f"m6-test-{seq:02d}",
        ran_at=ran_at,
        rerun_reason=reason,
        previous_result_sha256=previous_hash,
        result_path=result_path,
        raw_dir=raw_dir,
        runs_path=runs_path,
    )


def _append_completed(preflight: Preflight, result_hash: str) -> None:
    append_record(
        preflight.runs_path,
        {
            **_run_record_base(preflight),
            "result_sha256": result_hash,
            "status": "completed",
        },
        required=(
            "seq",
            "run_id",
            "ran_at",
            "config_hash",
            "rerun_reason",
            "previous_result_sha256",
            "result_path",
            "result_sha256",
            "status",
        ),
    )


def _append_failed(preflight: Preflight, exc: Exception) -> None:
    append_record(
        preflight.runs_path,
        {
            **_run_record_base(preflight),
            "result_sha256": None,
            "status": "failed",
            "error_type": type(exc).__name__,
        },
        required=(
            "seq",
            "run_id",
            "ran_at",
            "config_hash",
            "rerun_reason",
            "previous_result_sha256",
            "result_path",
            "result_sha256",
            "status",
        ),
    )


def _run_record_base(preflight: Preflight) -> dict[str, object]:
    return {
        "seq": preflight.seq,
        "run_id": preflight.run_id,
        "ran_at": preflight.ran_at,
        "config_hash": preflight.config["config_hash"],
        "rerun_reason": preflight.rerun_reason,
        "previous_result_sha256": preflight.previous_result_sha256,
        "result_path": preflight.result_path.relative_to(
            preflight.output_root
        ).as_posix(),
    }


def _selected_model_config(config: Mapping[str, object]) -> AnswerModelConfig:
    answer = config.get("answer")
    if not isinstance(answer, Mapping):
        raise PreflightError("M6 frozen answer settings are missing")
    model = answer.get("model")
    for name in ("luna", "sol"):
        candidate = load_answer_config(name)
        if candidate.model == model:
            return candidate
    raise PreflightError(f"unsupported frozen M6 model: {model}")


def _normalize_reason(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _record_seq(record: Mapping[str, object]) -> int:
    value = record.get("seq")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PreflightError("M6 test run seq is invalid")
    return value


def _read_json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON object required: {path}")
    return value


def _write_json_exclusive(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as destination:
        json.dump(value, destination, ensure_ascii=False, indent=2, sort_keys=True)
        destination.write("\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path = Path("."),
    out_root: Path | None = None,
    experiment_fn: ExperimentFunction | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="M6 frozen gold test와 실버 test 방향 확인 세트를 실행합니다."
    )
    parser.add_argument("--rerun-reason")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--opensearch-url", default="http://127.0.0.1:9200")
    parser.add_argument("--model-path")
    args = parser.parse_args(argv)
    selected_root = args.root or root
    try:
        execute = experiment_fn
        if execute is None:
            # Real clients and BGE-M3 are initialized only after run_test preflight.
            def execute(preflight: Preflight) -> ExperimentOutput:
                from kodocs.agent.answer import answer as real_answer
                from kodocs.config import load_settings
                from kodocs.llm.openai_chat import ManagedChat

                model_config = _selected_model_config(preflight.config)
                require_input_manifest(preflight.root)
                settings = load_settings()
                started = time.perf_counter()
                deps = build_tool_deps(
                    root=preflight.root,
                    base_url=args.opensearch_url,
                    model_path_or_id=args.model_path,
                )
                init_s = time.perf_counter() - started
                deps = serialize_embedder(deps)

                def runtime_factory(
                    _name: str, config: ModelConfigLike
                ) -> AgentRuntime:
                    if not isinstance(config, AnswerModelConfig):
                        raise TypeError("CLI requires AnswerModelConfig")
                    chat = ManagedChat(
                        config,
                        settings,
                        ledger_path=preflight.root / DEFAULT_LEDGER_PATH,
                        run_id=preflight.run_id,
                    )
                    return AgentRuntime(
                        deps=deps,
                        chat=chat,
                        model_record={
                            **config.record(),
                            "settings_hash": config.settings_hash(),
                        },
                        m5_config_hash=M5_CONFIG_HASH,
                        trace_dir=preflight.root / "logs/traces",
                    )

                try:
                    return execute_experiment(
                        preflight,
                        runtime_factory=runtime_factory,
                        model_config=model_config,
                        init_s=init_s,
                        answer_fn=real_answer,
                        concurrency=args.concurrency,
                    )
                finally:
                    deps.client.close()

        result = run_test(
            root=selected_root,
            out_root=out_root,
            rerun_reason=args.rerun_reason,
            experiment_fn=execute,
        )
        print(
            f"M6 test 완료: run={result['run_id']} "
            f"statuses={json.dumps(result.get('status_counts'), sort_keys=True)}",
            flush=True,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - bounded CLI failure report
        print(f"M6 test 실행 실패: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
