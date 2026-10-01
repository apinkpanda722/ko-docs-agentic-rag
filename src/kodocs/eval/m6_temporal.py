"""Compare M5 retrieval with current-version ordering disabled and enabled."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from kodocs.chunking.corpus import load_corpus
from kodocs.eval.m5_analysis import temporal_prior_vs_current
from kodocs.eval.m5_dev import verify_config
from kodocs.eval.m5_inputs import (
    EvalQuestion,
    build_eval_questions,
    load_retrieval_targets,
)
from kodocs.eval.m5_runner import QuestionRun, RunConfig, run_questions
from kodocs.retrieval.embedding import (
    Bge3Embedder,
    CachedEmbedder,
    Embedder,
    embedder_settings_hash,
)
from kodocs.retrieval.index_schema import index_name
from kodocs.retrieval.opensearch import OpenSearchClient

_CONFIG_PATH = Path("data/manifest/m5_config.json")
_PRIOR_VERSIONS_PATH = Path("data/manifest/m5_prior_versions.json")
_TABLE_GOLDEN_PATH = Path("data/manifest/table_golden.json")
_OUTPUT_PATH = Path("data/manifest/m6_temporal.json")
_EMBEDDING_CACHE = Path("data/interim/m5/embedding_cache")
_M5_CONFIG_HASH = "455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d"
_GROUP_NAMES = ("gold_dev", "gold_test", "silver_dev", "silver_test")
_EXPECTED_COUNTS = {
    "gold_dev": 14,
    "gold_test": 21,
    "silver_dev": 43,
    "silver_test": 65,
}


class TemporalRunFunction(Protocol):
    """M5 runner shape with the M6 ordering switch exposed."""

    def __call__(
        self,
        client: object,
        embedder: object,
        questions: Sequence[EvalQuestion],
        config: RunConfig,
        *,
        prefer_current: bool = False,
    ) -> list[QuestionRun]: ...


@dataclass(frozen=True)
class TemporalInputs:
    """All metric-bearing M5 questions and the five prior-version pairs."""

    groups: Mapping[str, Sequence[EvalQuestion]]
    prior_versions: Mapping[str, object]


def evaluate_temporal(
    client: object,
    embedder: object,
    inputs: TemporalInputs,
    config: RunConfig,
    *,
    m5_config_hash: str,
    run_fn: TemporalRunFunction = run_questions,
) -> dict[str, object]:
    """Run every M5 metric question twice and summarize rank-only changes."""
    unknown_groups = set(inputs.groups) - set(_GROUP_NAMES)
    missing_groups = set(_GROUP_NAMES) - set(inputs.groups)
    if unknown_groups or missing_groups:
        raise ValueError(
            f"temporal input groups mismatch: missing={sorted(missing_groups)}, "
            f"unknown={sorted(unknown_groups)}"
        )

    before_by_group: dict[str, tuple[QuestionRun, ...]] = {}
    after_by_group: dict[str, tuple[QuestionRun, ...]] = {}
    changes_by_group: dict[str, dict[str, object]] = {}
    all_question_ids: list[str] = []
    changed_ids: list[str] = []

    for group_name in _GROUP_NAMES:
        questions = tuple(inputs.groups[group_name])
        before = tuple(
            run_fn(
                client,
                embedder,
                questions,
                config,
                prefer_current=False,
            )
        )
        after = tuple(
            run_fn(
                client,
                embedder,
                questions,
                config,
                prefer_current=True,
            )
        )
        _require_complete(before, questions, label=f"{group_name}/off")
        _require_complete(after, questions, label=f"{group_name}/on")
        before_by_group[group_name] = before
        after_by_group[group_name] = after
        group_changed = [
            left.qid
            for left, right in zip(before, after, strict=True)
            if left.ranked_docs[:10] != right.ranked_docs[:10]
        ]
        all_question_ids.extend(item.question.id for item in questions)
        changed_ids.extend(group_changed)
        changes_by_group[group_name] = {
            "question_count": len(questions),
            "changed_count": len(group_changed),
        }
        print(
            f"M6 temporal 진행 group={group_name} "
            f"questions={len(questions)} searches={len(questions) * 2}",
            flush=True,
        )

    if len(set(all_question_ids)) != len(all_question_ids):
        raise ValueError("M6 temporal inputs contain duplicate question ids")

    gold_questions = (
        *inputs.groups["gold_dev"],
        *inputs.groups["gold_test"],
    )
    temporal_questions = tuple(
        item for item in gold_questions if "temporal:revision" in item.question.tags
    )
    temporal_ids = sorted(item.question.id for item in temporal_questions)
    temporal_splits = {
        split: sum(item.question.split == split for item in temporal_questions)
        for split in ("dev", "test")
    }
    if len(temporal_questions) != 5 or temporal_splits != {"dev": 2, "test": 3}:
        raise ValueError(
            "gold temporal questions must be the preregistered dev 2 + test 3"
        )

    before_gold = (
        *before_by_group["gold_dev"],
        *before_by_group["gold_test"],
    )
    after_gold = (
        *after_by_group["gold_dev"],
        *after_by_group["gold_test"],
    )
    before_temporal = temporal_prior_vs_current(
        {config: before_gold}, inputs.prior_versions, temporal_questions
    )
    after_temporal = temporal_prior_vs_current(
        {config: after_gold}, inputs.prior_versions, temporal_questions
    )

    changed = sorted(changed_ids)
    temporal_id_set = set(temporal_ids)
    unexpected = [qid for qid in changed if qid not in temporal_id_set]
    counts = {name: len(inputs.groups[name]) for name in _GROUP_NAMES}
    return {
        "m5_config_hash": m5_config_hash,
        "question_counts": {
            **counts,
            "all": sum(counts.values()),
            "gold_temporal": len(temporal_questions),
        },
        "temporal": {
            "prefer_current_off": _rank_only_temporal(before_temporal),
            "prefer_current_on": _rank_only_temporal(after_temporal),
        },
        "top10_changes": {
            "changed_count": len(changed),
            "changed_question_ids": changed,
            "expected_temporal_question_ids": temporal_ids,
            "unexpected_change_count": len(unexpected),
            "unexpected_changes": unexpected,
            "by_group": changes_by_group,
        },
    }


def run_temporal(
    *,
    root: Path = Path("."),
    output_path: Path | None = None,
    client_factory: Callable[[], OpenSearchClient] = OpenSearchClient,
    embedder_factory: Callable[[Path], Embedder] | None = None,
    input_loader: Callable[[Path], TemporalInputs] | None = None,
    run_fn: TemporalRunFunction = run_questions,
) -> dict[str, object]:
    """Validate frozen M5 inputs, run the comparison, and write one manifest."""
    root = Path(root)
    config_path = root / _CONFIG_PATH
    check = verify_config(config_path)
    if not check.ok:
        raise RuntimeError(
            f"M6 temporal config 검증 실패: {', '.join(check.mismatches)}"
        )
    raw_config = _read_json_object(config_path)
    config_hash = raw_config.get("config_hash")
    if config_hash != _M5_CONFIG_HASH:
        raise RuntimeError("M6 temporal은 사전 등록한 M5 config_hash만 허용합니다")
    config = _run_config(raw_config)

    load_inputs = input_loader or _load_inputs
    inputs = load_inputs(root)
    counts = {name: len(inputs.groups.get(name, ())) for name in _GROUP_NAMES}
    if counts != _EXPECTED_COUNTS:
        raise RuntimeError(f"M6 temporal 검색 지표 대상 수가 M5와 다릅니다: {counts}")

    make_embedder = embedder_factory or _make_embedder
    embedder = make_embedder(root)
    embedder.prepare()
    configured_embedder = raw_config.get("embedder")
    if not isinstance(configured_embedder, Mapping):
        raise TypeError("m5_config.embedder must be an object")
    if embedder_settings_hash(embedder) != configured_embedder.get("settings_hash"):
        raise RuntimeError("M6 temporal 임베더 설정 해시가 M5 config와 다릅니다")

    with client_factory() as client:
        result = evaluate_temporal(
            client,
            embedder,
            inputs,
            config,
            m5_config_hash=config_hash,
            run_fn=run_fn,
        )

    destination = output_path or root / _OUTPUT_PATH
    _write_json_atomic(destination, result)
    print(f"M6 temporal 완료 result={destination}", flush=True)
    return result


def _load_inputs(root: Path) -> TemporalInputs:
    targets = load_retrieval_targets(root)
    documents, _report = load_corpus("composite", root=root)
    documents_by_id = {document.doc_id: document for document in documents}
    table_golden = _read_json_object(root / _TABLE_GOLDEN_PATH)
    groups = {
        name: tuple(
            build_eval_questions(
                targets[name],
                documents_by_id,
                table_golden=table_golden,
            )
        )
        for name in _GROUP_NAMES
    }
    return TemporalInputs(
        groups=groups,
        prior_versions=_read_json_object(root / _PRIOR_VERSIONS_PATH),
    )


def _run_config(value: Mapping[str, object]) -> RunConfig:
    method = value.get("hybrid_method")
    candidate_chunks = value.get("candidate_chunks")
    bm25_weight = value.get("bm25_weight")
    rank_constant = value.get("rank_constant")
    if method != "hybrid_weighted":
        raise RuntimeError("M6 temporal M5 hybrid_method must be hybrid_weighted")
    if candidate_chunks != 50 or bm25_weight != 0.7 or rank_constant != 60:
        raise RuntimeError("M6 temporal M5 search parameters differ from ADR-029")
    indexes = value.get("indexes")
    expected_index = index_name("composite", "structural")
    if not isinstance(indexes, Mapping) or expected_index not in indexes:
        raise RuntimeError("M6 temporal structural index is missing from M5 config")
    return RunConfig(
        condition="composite",
        chunking="structural",
        method="hybrid_weighted",
        analyzer="nori",
        candidate_chunks=50,
        bm25_weight=0.7,
    )


def _rank_only_temporal(value: Mapping[str, object]) -> dict[str, object]:
    raw_rows = value.get("rows")
    by_split = value.get("by_split")
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
        raise TypeError("temporal analysis rows must be a sequence")
    if not isinstance(by_split, Mapping):
        raise TypeError("temporal analysis by_split must be an object")
    rows = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping):
            raise TypeError("temporal analysis row must be an object")
        rows.append(
            {
                key: raw_row[key]
                for key in (
                    "qid",
                    "split",
                    "current_rank",
                    "prior_rank",
                    "prior_above_current",
                )
            }
        )
    return {"rows": rows, "by_split": dict(by_split)}


def _require_complete(
    runs: Sequence[QuestionRun],
    questions: Sequence[EvalQuestion],
    *,
    label: str,
) -> None:
    expected = [item.question.id for item in questions]
    actual = [run.qid for run in runs]
    if actual != expected:
        raise RuntimeError(f"{label} run is incomplete or reordered")


def _make_embedder(root: Path) -> Embedder:
    return CachedEmbedder(Bge3Embedder(), root / _EMBEDDING_CACHE)


def _read_json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON object required: {path}")
    return value


def _write_json_atomic(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the operator-run M6 temporal experiment."""
    parser = argparse.ArgumentParser(description="M6 현행 우선 처리 전후 순위를 비교")
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    try:
        run_temporal(root=args.root)
        return 0
    except Exception as exc:  # noqa: BLE001 - bounded CLI failure report
        detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        print(f"M6 temporal 실행 실패: {detail}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
