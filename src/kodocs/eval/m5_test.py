"""Run the preregistered M5 test matrix with an append-only rerun lock.

All local artifact checks and lock checks complete before an embedder, an
OpenSearch client, or a search runner is invoked.  Successful runs get an
immutable result file and consume the lock.  Failed attempts are appended to
the run ledger but do not advance the successful result sequence.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from kodocs.chunking.corpus import load_corpus
from kodocs.eval.m5_analysis import (
    QuestionMeta,
    RunKey,
    ablation,
    corpus_v2_rule,
    full_grid,
    h2_breakdown,
    h3_breakdown,
    hypotheses,
    ladder,
    lexical_mismatch_subset,
    parser_compare,
    silver_direction,
    temporal_prior_vs_current,
)
from kodocs.eval.m5_dev import verify_config
from kodocs.eval.m5_inputs import (
    RETRIEVAL_TYPES,
    EvalQuestion,
    build_eval_questions,
    load_retrieval_targets,
)
from kodocs.eval.m5_runner import (
    HybridSetting,
    QuestionRun,
    RunConfig,
    all_combo_configs,
    run_questions,
)
from kodocs.ingest.manifest import (
    append_record,
    latest_records,
    load_records,
    now_iso,
    sha256_file,
)
from kodocs.retrieval.access import UserContext
from kodocs.retrieval.embedding import (
    Bge3Embedder,
    CachedEmbedder,
    Embedder,
    FakeEmbedder,
    embedder_settings_hash,
)
from kodocs.retrieval.index_build import m5_index_specs
from kodocs.retrieval.index_schema import index_name
from kodocs.retrieval.lookup import authorized_lookup_id, extract_query_identifiers
from kodocs.retrieval.opensearch import OpenSearchClient

_CONFIG_PATH = Path("data/manifest/m5_config.json")
_INDEX_PATH = Path("data/manifest/m5_index.json")
_EVAL_INPUTS_PATH = Path("data/manifest/m5_eval_inputs.json")
_PRIOR_VERSIONS_PATH = Path("data/manifest/m5_prior_versions.json")
_TABLE_GOLDEN_PATH = Path("data/manifest/table_golden.json")
_RUNS_PATH = Path("data/manifest/m5_test_runs.jsonl")
_RESULTS_DIR = Path("data/manifest/m5_test_results")
_INTERIM_DIR = Path("data/interim/m5")
_LEDGER_PATH = Path("data/synthetic/ledger.sqlite")
_COMPOSE_PATH = Path("infra/docker-compose.yml")

_EXPECTED_COUNTS = {
    "gold_test": 21,
    "silver_test": 65,
    "real_gold_all": 19,
    "real_gold_test": 13,
    "real_gold_dev": 6,
    "gold_identifier_test": 4,
    "silver_identifier_test": 15,
}
_BOOTSTRAP_SEED = 20260930
_BOOTSTRAP_RESAMPLES = 10_000
_RETRIEVAL_TYPE_SET = frozenset(RETRIEVAL_TYPES)


class PreflightError(RuntimeError):
    """A frozen local prerequisite failed before any retrieval work."""


class TestLockError(PreflightError):
    """The first-run/rerun-reason contract was violated."""


@dataclass(frozen=True)
class Preflight:
    """Validated paths and immutable settings for one attempted sequence."""

    root: Path
    output_root: Path
    config: Mapping[str, object]
    index_manifest: Mapping[str, object]
    eval_inputs: Mapping[str, object]
    seq: int
    run_id: str
    ran_at: str
    rerun_reason: str | None
    previous_result_sha256: str | None
    result_path: Path
    raw_dir: Path
    runs_path: Path
    embedder_name: str


@dataclass(frozen=True)
class ExperimentOutput:
    """Commit-safe summary plus non-committed question-level evidence."""

    result: Mapping[str, object]
    raw: Mapping[str, object]


@dataclass(frozen=True)
class AnalysisInputs:
    """Already-computed retrieval data consumed by pure analysis functions."""

    runs: Mapping[RunKey, Sequence[QuestionRun]]
    metadata: Mapping[str, QuestionMeta]
    lookup_top1: Mapping[str, bool]
    prior_versions: Mapping[str, object]
    gold_questions: Sequence[EvalQuestion]
    gold_test_ids: tuple[str, ...]
    parser_runs: Mapping[str, Sequence[QuestionRun]]
    real_all_ids: tuple[str, ...]
    real_test_ids: tuple[str, ...]
    silver_test_ids: tuple[str, ...]
    silver_temporal_ids: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class _InputBundle:
    gold_test: tuple[EvalQuestion, ...]
    silver_test: tuple[EvalQuestion, ...]
    real_gold_dev: tuple[EvalQuestion, ...]
    real_gold_test: tuple[EvalQuestion, ...]
    pypdf_real_all: tuple[EvalQuestion, ...]
    upstage_real_all: tuple[EvalQuestion, ...]

    @property
    def real_gold_all(self) -> tuple[EvalQuestion, ...]:
        return (*self.real_gold_dev, *self.real_gold_test)


ExperimentFunction = Callable[[Preflight], ExperimentOutput]
RunFunction = Callable[
    [object, object, Sequence[EvalQuestion], RunConfig], list[QuestionRun]
]
AnalysisFunction = Callable[[AnalysisInputs], Mapping[str, object]]


def run_test(
    *,
    root: Path = Path("."),
    out_root: Path | None = None,
    rerun_reason: str | None = None,
    embedder_name: str = "bge-m3",
    experiment_fn: ExperimentFunction | None = None,
    now_fn: Callable[[], str] = now_iso,
) -> dict[str, object]:
    """Validate, execute, and immutably record one M5 test attempt."""
    root = Path(root)
    output_root = Path(out_root) if out_root is not None else root
    preflight = _preflight(
        root=root,
        output_root=output_root,
        rerun_reason=rerun_reason,
        embedder_name=embedder_name,
        ran_at=now_fn(),
    )
    execute = experiment_fn or execute_experiment
    completed = False
    try:
        output = execute(preflight)
        if not isinstance(output, ExperimentOutput):
            raise TypeError("experiment_fn must return ExperimentOutput")
        result = _result_payload(preflight, output.result)
        _write_raw_exclusive(preflight.raw_dir, output.raw)
        try:
            _write_json_exclusive(preflight.result_path, result)
        except Exception:
            _remove_generated_raw(preflight.raw_dir)
            raise
        result_hash = sha256_file(preflight.result_path)
        _append_completed_record(preflight, result_hash)
        completed = True
        return result
    except Exception as exc:
        if not completed:
            if preflight.result_path.exists():
                preflight.result_path.unlink()
            if preflight.raw_dir.exists():
                _remove_generated_raw(preflight.raw_dir)
            _append_failed_record(preflight, exc)
        raise


def execute_experiment(
    context: Preflight,
    *,
    run_fn: RunFunction = run_questions,
    analysis_fn: AnalysisFunction | None = None,
    client_factory: Callable[[], OpenSearchClient] = OpenSearchClient,
) -> ExperimentOutput:
    """Execute all 14 test combinations, lookups, and parser comparisons."""
    inputs = _load_inputs(context.root)
    configs, final_config = _run_configs(context.root, context.config)
    embedder = _make_embedder(
        context.embedder_name,
        cache_root=context.output_root,
    )
    init_s = float(embedder.prepare())
    actual_embedder_hash = embedder_settings_hash(embedder)
    configured_embedder = _required_mapping(
        context.config, "embedder", source=_CONFIG_PATH
    )
    expected_embedder_hash = _required_str(
        configured_embedder.get("settings_hash"), "config.embedder.settings_hash"
    )
    if actual_embedder_hash != expected_embedder_hash:
        raise PreflightError(
            "M5 test 사전 검사 실패: 실행 임베더 설정 해시가 config와 다릅니다"
        )

    runs: dict[RunKey, Sequence[QuestionRun]] = {}
    raw_runs: list[dict[str, object]] = []
    parser_runs: dict[str, Sequence[QuestionRun]] = {}
    with client_factory() as client:
        opensearch_version = _opensearch_version(client)
        for config in configs:
            selected = _hybrid_identity(config) == _hybrid_identity(final_config)
            for question_set, questions in (
                ("gold", inputs.gold_test),
                ("silver", inputs.silver_test),
            ):
                values = run_fn(client, embedder, questions, config)
                _require_complete(values, questions, label=f"{question_set}_test")
                key = RunKey(
                    question_set=question_set,
                    split="test",
                    config=config,
                    selected_hybrid=selected,
                )
                runs[key] = tuple(values)
                raw_runs.append(_raw_run(key, values))

        dev_values = run_fn(client, embedder, inputs.real_gold_dev, final_config)
        _require_complete(dev_values, inputs.real_gold_dev, label="real_gold_dev")
        dev_key = RunKey(
            question_set="gold",
            split="dev",
            config=final_config,
            selected_hybrid=True,
        )
        runs[dev_key] = tuple(dev_values)
        raw_runs.append(_raw_run(dev_key, dev_values))

        final_test_values = runs[
            RunKey("gold", "test", final_config, selected_hybrid=True)
        ]
        test_by_qid = {run.qid: run for run in final_test_values}
        composite_parser = [
            *dev_values,
            *(test_by_qid[item.question.id] for item in inputs.real_gold_test),
        ]
        parser_runs["composite"] = tuple(composite_parser)
        for condition, questions in (
            ("pypdf", inputs.pypdf_real_all),
            ("upstage", inputs.upstage_real_all),
        ):
            parser_config = RunConfig(
                condition=condition,  # type: ignore[arg-type]
                chunking="structural",
                method=final_config.method,
                analyzer="nori",
                candidate_chunks=final_config.candidate_chunks,
                bm25_weight=final_config.bm25_weight,
            )
            values = run_fn(client, embedder, questions, parser_config)
            _require_complete(values, questions, label=f"parser_{condition}")
            parser_runs[condition] = tuple(values)
            raw_runs.append(
                {
                    "question_set": "gold",
                    "split": "dev+test",
                    "selected_hybrid": True,
                    "config": asdict(parser_config),
                    "questions": [asdict(value) for value in values],
                }
            )

        service_names = _load_service_names(context.root / _LEDGER_PATH)
        lookup_questions = [
            item
            for item in (*inputs.gold_test, *inputs.silver_test)
            if item.question.type == "identifier"
        ]
        lookup_top1 = _run_identifier_lookups(
            client,
            lookup_questions,
            service_names=service_names,
        )

    metadata = _metadata(
        (*inputs.gold_test, *inputs.silver_test, *inputs.real_gold_dev)
    )
    gold_questions = (*inputs.real_gold_dev, *inputs.gold_test)
    real_all_ids = tuple(item.question.id for item in inputs.real_gold_all)
    real_test_ids = tuple(item.question.id for item in inputs.real_gold_test)
    silver_ids = tuple(item.question.id for item in inputs.silver_test)
    analysis_inputs = AnalysisInputs(
        runs=runs,
        metadata=metadata,
        lookup_top1=lookup_top1,
        prior_versions=_read_json_object(context.root / _PRIOR_VERSIONS_PATH),
        gold_questions=gold_questions,
        gold_test_ids=tuple(item.question.id for item in inputs.gold_test),
        parser_runs=parser_runs,
        real_all_ids=real_all_ids,
        real_test_ids=real_test_ids,
        silver_test_ids=silver_ids,
        silver_temporal_ids={
            tag: tuple(
                item.question.id
                for item in inputs.silver_test
                if tag in item.question.tags
            )
            for tag in ("temporal:revision", "temporal:as_of_event")
        },
    )
    analyze = analysis_fn or analyze_results
    analysis = dict(analyze(analysis_inputs))
    environment = _environment(
        context,
        embedder,
        init_s=init_s,
        opensearch_version=opensearch_version,
    )
    result = {
        "fixed_configuration": {
            "hybrid_method": context.config["hybrid_method"],
            "candidate_chunks": context.config["candidate_chunks"],
            "bm25_weight": context.config.get("bm25_weight"),
            "rank_constant": context.config["rank_constant"],
            "combination_count": len(configs),
        },
        "question_counts": _question_counts(inputs),
        "counting_units": {
            "retrieval": "unique question_id with at least one visible rel >= 1 document",
            "document_metrics": "ranked unique doc_id; top 10 documents",
            "span_metric": "any top-10 chunk overlaps a visible rel >= 1 answer range",
            "lookup_top1": "first authorized exact-identifier document has rel >= 1",
        },
        "environment": environment,
        "analysis": analysis,
    }
    raw = {
        "seq": context.seq,
        "run_id": context.run_id,
        "runs": raw_runs,
        "lookup_top1": dict(sorted(lookup_top1.items())),
    }
    return ExperimentOutput(result=result, raw=raw)


def analyze_results(inputs: AnalysisInputs) -> dict[str, object]:
    """Call every preregistered and labeled auxiliary M5 analysis."""
    silver_groups: dict[str, tuple[str, ...]] = {
        "all": inputs.silver_test_ids,
        **inputs.silver_temporal_ids,
    }
    return {
        "ladder": ladder(inputs.runs, inputs.gold_test_ids),
        "ablation": ablation(inputs.runs, inputs.gold_test_ids),
        "full_grid": full_grid(inputs.runs, inputs.gold_test_ids),
        "hypotheses": hypotheses(
            inputs.runs,
            inputs.metadata,
            inputs.lookup_top1,
        ),
        "h2_breakdown": h2_breakdown(inputs.runs, inputs.metadata),
        "h3_breakdown": h3_breakdown(inputs.runs, inputs.metadata),
        "lexical_mismatch_subset": lexical_mismatch_subset(
            inputs.runs, inputs.metadata
        ),
        "corpus_v2_rule": corpus_v2_rule(inputs.runs, inputs.metadata),
        "temporal_prior_vs_current": temporal_prior_vs_current(
            inputs.runs,
            inputs.prior_versions,
            inputs.gold_questions,
        ),
        "parser_comparison": parser_compare(
            inputs.parser_runs,
            inputs.real_all_ids,
            inputs.real_test_ids,
        ),
        "silver_direction": silver_direction(inputs.runs, silver_groups),
    }


def _preflight(
    *,
    root: Path,
    output_root: Path,
    rerun_reason: str | None,
    embedder_name: str,
    ran_at: str,
) -> Preflight:
    config_path = root / _CONFIG_PATH
    check = verify_config(config_path)
    if not check.ok:
        details = ", ".join(check.mismatches)
        raise PreflightError(f"M5 test 사전 검사 실패: config ({details})")
    config = _read_json_object(config_path)
    index_manifest = _read_json_object(root / _INDEX_PATH)
    eval_inputs = _read_json_object(root / _EVAL_INPUTS_PATH)
    _check_indexes(config, index_manifest)
    _check_eval_inputs(eval_inputs)

    normalized_reason = _normalize_reason(rerun_reason)
    runs_path = output_root / _RUNS_PATH
    records = load_records(runs_path)
    current = latest_records(records, key=_record_seq)
    completed = [record for record in current if record.get("status") == "completed"]
    completed.sort(key=_record_seq)
    if completed and normalized_reason is None:
        raise TestLockError(
            "이전 실행이 있습니다. 재실행은 --rerun-reason이 필요합니다"
        )
    if not completed and normalized_reason is not None:
        raise TestLockError(
            "이전 성공 실행이 없습니다. 첫 실행에는 --rerun-reason을 지정할 수 없습니다"
        )

    previous_hash: str | None = None
    if completed:
        previous = completed[-1]
        previous_path_value = _required_str(
            previous.get("result_path"), "previous result_path"
        )
        previous_path = output_root / previous_path_value
        if not previous_path.is_file():
            raise PreflightError("M5 test 사전 검사 실패: 이전 결과 파일이 없습니다")
        previous_hash = sha256_file(previous_path)
        recorded_previous_hash = _required_str(
            previous.get("result_sha256"), "previous result_sha256"
        )
        if previous_hash != recorded_previous_hash:
            raise PreflightError(
                "M5 test 사전 검사 실패: 이전 결과 파일 해시가 기록과 다릅니다"
            )
    seq = max((_record_seq(record) for record in completed), default=0) + 1
    result_path = output_root / _RESULTS_DIR / f"run-{seq:02d}.json"
    raw_dir = output_root / _INTERIM_DIR / f"run-{seq:02d}"
    if result_path.exists():
        raise FileExistsError(f"이전 M5 test 결과 파일이 이미 있습니다: {result_path}")
    if raw_dir.exists():
        raise FileExistsError(
            f"이전 M5 test 원시 결과 디렉터리가 이미 있습니다: {raw_dir}"
        )
    return Preflight(
        root=root,
        output_root=output_root,
        config=config,
        index_manifest=index_manifest,
        eval_inputs=eval_inputs,
        seq=seq,
        run_id=f"m5-test-{seq:02d}",
        ran_at=ran_at,
        rerun_reason=normalized_reason,
        previous_result_sha256=previous_hash,
        result_path=result_path,
        raw_dir=raw_dir,
        runs_path=runs_path,
        embedder_name=embedder_name,
    )


def _check_indexes(
    config: Mapping[str, object], index_manifest: Mapping[str, object]
) -> None:
    frozen = _required_mapping(config, "indexes", source=_CONFIG_PATH)
    current = _required_mapping(index_manifest, "indexes", source=_INDEX_PATH)
    expected = {index_name(spec.condition, spec.chunking) for spec in m5_index_specs()}
    if set(frozen) != expected or not expected <= set(current):
        raise PreflightError("M5 test 사전 검사 실패: config/index의 4개 인덱스")
    embedder = _required_mapping(config, "embedder", source=_CONFIG_PATH)
    expected_embedder_hash = _required_str(
        embedder.get("settings_hash"), "config.embedder.settings_hash"
    )
    for name in sorted(expected):
        frozen_entry = _mapping_value(frozen, name, source=_CONFIG_PATH)
        current_entry = _mapping_value(current, name, source=_INDEX_PATH)
        if current_entry.get("status") != "ok":
            raise PreflightError(
                f"M5 test 사전 검사 실패: 인덱스 상태가 ok가 아닙니다 ({name})"
            )
        if current_entry.get("failure_count") != 0 or current_entry.get(
            "failures"
        ) not in ([], ()):
            raise PreflightError(
                f"M5 test 사전 검사 실패: 색인 실패가 있습니다 ({name})"
            )
        if frozen_entry.get("schema_hash") != current_entry.get(
            "index_field_schema_hash"
        ):
            raise PreflightError(f"M5 test 사전 검사 실패: 스키마 해시 불일치 ({name})")
        current_embedder = _mapping_value(current_entry, "embedder", source=_INDEX_PATH)
        if current_embedder.get("settings_hash") != expected_embedder_hash:
            raise PreflightError(
                f"M5 test 사전 검사 실패: 임베더 설정 해시 불일치 ({name})"
            )


def _check_eval_inputs(value: Mapping[str, object]) -> None:
    if value.get("status") != "ok":
        raise PreflightError("M5 test 사전 검사 실패: m5_eval_inputs 상태")
    for key, label in (
        ("visibility", "권한 전제"),
        ("answer_range_mapping", "구간 매핑"),
    ):
        section = _required_mapping(value, key, source=_EVAL_INPUTS_PATH)
        failures = section.get("failures")
        if section.get("failure_count") != 0 or failures not in ([], ()):
            raise PreflightError(f"M5 test 사전 검사 실패: {label} 실패")
    count_errors = value.get("count_validation_errors", [])
    if count_errors not in ([], ()):
        raise PreflightError("M5 test 사전 검사 실패: 문항 수 검증 실패")


def _load_inputs(root: Path) -> _InputBundle:
    targets = load_retrieval_targets(root)
    table_golden = _read_json_object(root / _TABLE_GOLDEN_PATH)

    composite_docs, _ = load_corpus("composite", root=root)
    composite_by_id = {document.doc_id: document for document in composite_docs}
    gold_test = tuple(
        build_eval_questions(
            targets["gold_test"],
            composite_by_id,
            table_golden=table_golden,
        )
    )
    silver_test = tuple(
        build_eval_questions(
            targets["silver_test"],
            composite_by_id,
            table_golden=table_golden,
        )
    )
    real_dev_raw = tuple(
        question for question in targets["gold_dev"] if question.source == "real"
    )
    real_test_raw = tuple(
        question for question in targets["gold_test"] if question.source == "real"
    )
    real_dev = tuple(
        build_eval_questions(real_dev_raw, composite_by_id, table_golden=table_golden)
    )
    real_test = tuple(item for item in gold_test if item.question.source == "real")
    all_real_raw = (*real_dev_raw, *real_test_raw)
    parser_questions: dict[str, tuple[EvalQuestion, ...]] = {}
    for condition in ("pypdf", "upstage"):
        documents, _ = load_corpus(condition, root=root)  # type: ignore[arg-type]
        docs_by_id = {document.doc_id: document for document in documents}
        parser_questions[condition] = tuple(
            build_eval_questions(
                all_real_raw,
                docs_by_id,
                table_golden=table_golden,
            )
        )
    bundle = _InputBundle(
        gold_test=gold_test,
        silver_test=silver_test,
        real_gold_dev=real_dev,
        real_gold_test=real_test,
        pypdf_real_all=parser_questions["pypdf"],
        upstage_real_all=parser_questions["upstage"],
    )
    counts = _question_counts(bundle)
    for label, expected in _EXPECTED_COUNTS.items():
        if counts[label] != expected:
            raise PreflightError(
                f"M5 test 사전 검사 실패: {label}={counts[label]} (expected {expected})"
            )
    return bundle


def _question_counts(inputs: _InputBundle) -> dict[str, int]:
    return {
        "gold_test": len(inputs.gold_test),
        "silver_test": len(inputs.silver_test),
        "real_gold_all": len(inputs.real_gold_all),
        "real_gold_test": len(inputs.real_gold_test),
        "real_gold_dev": len(inputs.real_gold_dev),
        "gold_identifier_test": sum(
            item.question.type == "identifier" for item in inputs.gold_test
        ),
        "silver_identifier_test": sum(
            item.question.type == "identifier" for item in inputs.silver_test
        ),
    }


def _run_configs(
    root: Path, config: Mapping[str, object]
) -> tuple[list[RunConfig], RunConfig]:
    method = config.get("hybrid_method")
    if method not in {"hybrid_rrf", "hybrid_weighted"}:
        raise PreflightError("M5 test 사전 검사 실패: hybrid_method")
    candidate_chunks = _positive_int(
        config.get("candidate_chunks"), "config.candidate_chunks"
    )
    configured_weight = config.get("bm25_weight")
    selected_weight = (
        _weight(configured_weight, "config.bm25_weight")
        if method == "hybrid_weighted"
        else None
    )
    matrix_weight = (
        selected_weight
        if selected_weight is not None
        else _best_weighted_weight(root, candidate_chunks)
    )
    configs = all_combo_configs(HybridSetting(candidate_chunks, matrix_weight))
    final = RunConfig(
        condition="composite",
        chunking="structural",
        method=method,  # type: ignore[arg-type]
        analyzer="nori",
        candidate_chunks=candidate_chunks,
        bm25_weight=selected_weight,
    )
    return configs, final


def _best_weighted_weight(root: Path, candidate_chunks: int) -> float:
    dev = _read_json_object(root / "data/manifest/m5_dev.json")
    grid = dev.get("grid")
    if not isinstance(grid, Sequence) or isinstance(grid, (str, bytes)):
        raise PreflightError(
            "M5 test 사전 검사 실패: RRF 선택 시 weighted 비교 설정을 dev grid에서 읽을 수 없습니다"
        )
    candidates: list[tuple[float, float]] = []
    for row in grid:
        if not isinstance(row, Mapping):
            continue
        if (
            row.get("method") != "hybrid_weighted"
            or row.get("candidate_chunks") != candidate_chunks
        ):
            continue
        score = row.get("selection_score")
        weight = row.get("bm25_weight")
        if (
            isinstance(score, bool)
            or not isinstance(score, int | float)
            or isinstance(weight, bool)
            or not isinstance(weight, int | float)
        ):
            continue
        candidates.append((float(score), float(weight)))
    if not candidates:
        raise PreflightError(
            "M5 test 사전 검사 실패: 확정 candidate_chunks의 weighted dev 설정이 없습니다"
        )
    return min(candidates, key=lambda item: (-round(item[0], 10), item[1]))[1]


def _run_identifier_lookups(
    client: OpenSearchClient,
    questions: Sequence[EvalQuestion],
    *,
    service_names: Sequence[str],
) -> dict[str, bool]:
    result: dict[str, bool] = {}
    index = index_name("composite", "structural")
    for item in questions:
        question = item.question
        extracted = extract_query_identifiers(
            question.question,
            service_names=service_names,
        )
        if not extracted:
            result[question.id] = False
            continue
        kind, value = extracted[0]
        hits = authorized_lookup_id(
            client,
            index,
            UserContext(question.asker.team, question.asker.grade),
            kind,
            value,
            rank_query=question.question,
        )
        result[question.id] = bool(hits and item.rel_by_doc.get(hits[0].doc_id, 0) >= 1)
    return result


def _load_service_names(path: Path) -> tuple[str, ...]:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        rows = connection.execute(
            "SELECT service FROM services ORDER BY service"
        ).fetchall()
    finally:
        if connection is not None:
            connection.close()
    if not all(
        isinstance(row, tuple) and len(row) == 1 and isinstance(row[0], str)
        for row in rows
    ):
        raise TypeError("services table contains an invalid service name")
    return tuple(row[0] for row in rows)


def _metadata(questions: Sequence[EvalQuestion]) -> dict[str, QuestionMeta]:
    result: dict[str, QuestionMeta] = {}
    for item in questions:
        question = item.question
        meta = QuestionMeta(
            qid=question.id,
            question_set=question.set,
            source=question.source,
            question_type=question.type,
            tags=tuple(question.tags),
            repeated=item.repeated,
            split=question.split or "",
        )
        previous = result.setdefault(question.id, meta)
        if previous != meta:
            raise ValueError(f"conflicting question metadata: {question.id}")
    return result


def _raw_run(key: RunKey, values: Sequence[QuestionRun]) -> dict[str, object]:
    return {
        "question_set": key.question_set,
        "split": key.split,
        "selected_hybrid": key.selected_hybrid,
        "config": asdict(key.config),
        "questions": [asdict(value) for value in values],
    }


def _require_complete(
    runs: Sequence[QuestionRun],
    questions: Sequence[EvalQuestion],
    *,
    label: str,
) -> None:
    expected = [item.question.id for item in questions]
    actual = [run.qid for run in runs]
    if actual != expected:
        raise RuntimeError(f"{label} search run is incomplete or reordered")


def _make_embedder(name: str, *, cache_root: Path) -> Embedder:
    inner: Embedder
    if name == "fake":
        inner = FakeEmbedder()
    elif name == "bge-m3":
        inner = Bge3Embedder()
    else:
        raise ValueError(f"unsupported embedder: {name}")
    return CachedEmbedder(inner, cache_root / _INTERIM_DIR / "embedding_cache")


def _environment(
    context: Preflight,
    embedder: Embedder,
    *,
    init_s: float,
    opensearch_version: str,
) -> dict[str, object]:
    settings = dict(embedder.settings)
    return {
        "ran_at": context.ran_at,
        "code_commit": _git_commit(context.root),
        "config_hash": context.config["config_hash"],
        "embedder": {
            "model_id": embedder.model_id,
            "revision": embedder.revision,
            "settings_hash": embedder_settings_hash(embedder),
            "device": settings.get("device"),
            "init_s": init_s,
        },
        "opensearch": {
            "version": opensearch_version,
            "heap": _opensearch_heap(context.root / _COMPOSE_PATH),
        },
        "platform": platform.platform(),
        "bootstrap": {
            "seed": _BOOTSTRAP_SEED,
            "resamples": _BOOTSTRAP_RESAMPLES,
            "interval": "percentile 95%",
        },
    }


def _opensearch_version(client: OpenSearchClient) -> str:
    response = client.request("GET", "/")
    version = response.get("version")
    if not isinstance(version, Mapping):
        raise TypeError("OpenSearch root response has no version object")
    return _required_str(version.get("number"), "OpenSearch version.number")


def _opensearch_heap(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r'OPENSEARCH_JAVA_OPTS:\s*["\']([^"\']+)["\']', text)
    if match is None:
        raise ValueError("OPENSEARCH_JAVA_OPTS not found in docker-compose.yml")
    return match.group(1)


def _git_commit(root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value or None


def _result_payload(
    context: Preflight, experiment: Mapping[str, object]
) -> dict[str, object]:
    result = dict(experiment)
    result.update(
        {
            "seq": context.seq,
            "run_id": context.run_id,
            "ran_at": context.ran_at,
            "config_hash": context.config["config_hash"],
            "rerun_reason": context.rerun_reason,
            "previous_result_sha256": context.previous_result_sha256,
        }
    )
    return result


def _append_completed_record(context: Preflight, result_hash: str) -> None:
    append_record(
        context.runs_path,
        {
            "seq": context.seq,
            "run_id": context.run_id,
            "ran_at": context.ran_at,
            "config_hash": context.config["config_hash"],
            "rerun_reason": context.rerun_reason,
            "previous_result_sha256": context.previous_result_sha256,
            "result_sha256": result_hash,
            "result_path": context.result_path.relative_to(
                context.output_root
            ).as_posix(),
            "status": "completed",
        },
        required=(
            "seq",
            "run_id",
            "ran_at",
            "config_hash",
            "rerun_reason",
            "previous_result_sha256",
            "result_sha256",
            "result_path",
        ),
    )


def _append_failed_record(context: Preflight, exc: Exception) -> None:
    reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    append_record(
        context.runs_path,
        {
            "seq": context.seq,
            "run_id": context.run_id,
            "ran_at": context.ran_at,
            "config_hash": context.config["config_hash"],
            "rerun_reason": context.rerun_reason,
            "previous_result_sha256": context.previous_result_sha256,
            "result_sha256": None,
            "result_path": context.result_path.relative_to(
                context.output_root
            ).as_posix(),
            "status": "failed",
            "error": f"{type(exc).__name__}: {reason}"[:300],
        },
        required=(
            "seq",
            "run_id",
            "ran_at",
            "config_hash",
            "rerun_reason",
            "previous_result_sha256",
            "result_sha256",
            "result_path",
            "status",
        ),
    )


def _write_json_exclusive(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as destination:
        json.dump(value, destination, ensure_ascii=False, indent=2, sort_keys=True)
        destination.write("\n")


def _write_raw_exclusive(path: Path, value: Mapping[str, object]) -> None:
    path.mkdir(parents=True, exist_ok=False)
    try:
        _write_json_exclusive(path / "results.json", value)
    except Exception:
        _remove_generated_raw(path)
        raise


def _remove_generated_raw(path: Path) -> None:
    result = path / "results.json"
    if result.exists():
        result.unlink()
    if path.exists():
        path.rmdir()


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError(f"M5 test 사전 검사 실패: {path}") from exc
    if not isinstance(value, dict):
        raise PreflightError(f"M5 test 사전 검사 실패: JSON object required: {path}")
    return value


def _required_mapping(
    value: Mapping[str, object], key: str, *, source: Path
) -> Mapping[str, object]:
    result = value.get(key)
    if not isinstance(result, Mapping):
        raise PreflightError(
            f"M5 test 사전 검사 실패: {source}의 {key} object가 없습니다"
        )
    return result


def _mapping_value(
    value: Mapping[str, object], key: str, *, source: Path
) -> Mapping[str, object]:
    return _required_mapping(value, key, source=source)


def _required_str(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise PreflightError(f"M5 test 사전 검사 실패: {label}")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PreflightError(f"M5 test 사전 검사 실패: {label}")
    return value


def _weight(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not 0.0 <= float(value) <= 1.0
    ):
        raise PreflightError(f"M5 test 사전 검사 실패: {label}")
    return float(value)


def _normalize_reason(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TestLockError("--rerun-reason은 문자열이어야 합니다")
    normalized = value.strip()
    return normalized or None


def _record_seq(record: Mapping[str, object]) -> int:
    value = record.get("seq")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PreflightError("M5 test 사전 검사 실패: 실행 기록 seq")
    return value


def _hybrid_identity(config: RunConfig) -> tuple[str, int, float | None]:
    return config.method, config.candidate_chunks, config.bm25_weight


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path = Path("."),
    experiment_fn: ExperimentFunction | None = None,
) -> int:
    """CLI entry point for the locked M5 test run."""
    parser = argparse.ArgumentParser(description="M5 확정 설정 test를 실행합니다.")
    parser.add_argument("--rerun-reason")
    parser.add_argument("--embedder", choices=("bge-m3", "fake"), default="bge-m3")
    parser.add_argument("--out-root", type=Path)
    args = parser.parse_args(argv)
    if args.embedder == "fake" and args.out_root is None:
        print(
            "M5 test 실행 실패: fake 실행은 잠금을 격리할 --out-root가 필요합니다",
            file=sys.stderr,
        )
        return 2
    try:
        result = run_test(
            root=root,
            out_root=args.out_root,
            rerun_reason=args.rerun_reason,
            embedder_name=args.embedder,
            experiment_fn=experiment_fn,
        )
        output_root = args.out_root if args.out_root is not None else root
        destination = output_root / _RESULTS_DIR / f"run-{result['seq']:02d}.json"
        print(
            f"M5 test 완료 run_id={result['run_id']} result={destination}",
            flush=True,
        )
        return 0
    except TestLockError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 2
    except Exception as exc:  # noqa: BLE001 - bounded CLI failure report
        detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        print(f"M5 test 실행 실패: {detail}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
