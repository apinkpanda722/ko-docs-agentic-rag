"""Tune and freeze preregistered M5 hybrid retrieval settings.

The immutable dev grid is ``hybrid_rrf(rank_constant=60)`` plus
``hybrid_weighted(bm25_weight in {0.3, 0.4, 0.5, 0.6, 0.7})``, crossed with
``candidate_chunks in {50, 100, 200}``: 18 settings.  Selection uses the equal
average of gold-dev and silver-dev mean NDCG@10.  Scores tie after rounding to
10 decimal places; ties prefer RRF, smaller candidate counts, then smaller
BM25 weights.  Test questions are rejected before any search.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from kodocs.chunking.corpus import load_corpus
from kodocs.eval.m5_inputs import (
    EvalQuestion,
    build_eval_questions,
    load_retrieval_targets,
)
from kodocs.eval.m5_runner import QuestionRun, RunConfig, run_questions
from kodocs.ingest.manifest import now_iso, sha256_file
from kodocs.retrieval.embedding import (
    Bge3Embedder,
    CachedEmbedder,
    Embedder,
    FakeEmbedder,
    embedder_settings_hash,
)
from kodocs.retrieval.index_build import m5_index_specs
from kodocs.retrieval.index_schema import index_name
from kodocs.retrieval.opensearch import OpenSearchClient

HybridMethod = Literal["hybrid_rrf", "hybrid_weighted"]

DEV_CANDIDATE_CHUNKS = (50, 100, 200)
DEV_BM25_WEIGHTS = (0.3, 0.4, 0.5, 0.6, 0.7)
RANK_CONSTANT = 60

_MANIFEST_DIR = Path("data/manifest")
_DEV_PATH = _MANIFEST_DIR / "m5_dev.json"
_CONFIG_PATH = _MANIFEST_DIR / "m5_config.json"
_INDEX_PATH = _MANIFEST_DIR / "m5_index.json"
_EVAL_INPUTS_PATH = _MANIFEST_DIR / "m5_eval_inputs.json"
_EXPECTED_GOLD_DEV = 14
_EXPECTED_SILVER_DEV = 43


@dataclass(frozen=True)
class TuningSetting:
    """One setting in the fixed 18-cell dev grid."""

    method: HybridMethod
    candidate_chunks: int
    bm25_weight: float | None

    def __post_init__(self) -> None:
        if self.candidate_chunks not in DEV_CANDIDATE_CHUNKS:
            raise ValueError("candidate_chunks is outside the preregistered dev grid")
        if self.method == "hybrid_weighted" and self.bm25_weight not in (
            DEV_BM25_WEIGHTS
        ):
            raise ValueError("bm25_weight is outside the preregistered dev grid")
        RunConfig(
            condition="composite",
            chunking="fixed",
            method=self.method,
            analyzer="nori",
            candidate_chunks=self.candidate_chunks,
            bm25_weight=self.bm25_weight,
        )


@dataclass(frozen=True)
class DevGridRow:
    """Mean dev metrics and selection score for one grid setting."""

    method: HybridMethod
    candidate_chunks: int
    bm25_weight: float | None
    gold_dev_mean_ndcg10: float
    silver_dev_mean_ndcg10: float
    selection_score: float


@dataclass(frozen=True)
class DevTuningResult:
    """Complete dev grid and its preregistered winner."""

    rows: tuple[DevGridRow, ...]
    selected: DevGridRow
    gold_dev_count: int
    silver_dev_count: int


@dataclass(frozen=True)
class ConfigCheck:
    """Verification result for a frozen M5 configuration."""

    mismatches: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.mismatches

    @property
    def valid(self) -> bool:
        """Alias used by callers that describe the check as validity."""
        return self.ok


RunFunction = Callable[
    [object, object, Sequence[EvalQuestion], RunConfig], list[QuestionRun]
]


def tuning_grid() -> list[TuningSetting]:
    """Return the preregistered 3 RRF + 15 weighted settings."""
    return [
        TuningSetting("hybrid_rrf", candidate_chunks, None)
        for candidate_chunks in DEV_CANDIDATE_CHUNKS
    ] + [
        TuningSetting("hybrid_weighted", candidate_chunks, weight)
        for weight in DEV_BM25_WEIGHTS
        for candidate_chunks in DEV_CANDIDATE_CHUNKS
    ]


def select_setting(rows: Sequence[DevGridRow]) -> DevGridRow:
    """Select by rounded score and the three preregistered tie breakers."""
    if not rows:
        raise ValueError("dev grid rows must not be empty")
    for row in rows:
        if not math.isfinite(row.selection_score):
            raise ValueError("selection scores must be finite")
    return min(
        rows,
        key=lambda row: (
            -round(row.selection_score, 10),
            0 if row.method == "hybrid_rrf" else 1,
            row.candidate_chunks,
            row.bm25_weight if row.bm25_weight is not None else 0.0,
        ),
    )


def run_dev_tuning(
    client: object,
    embedder: object,
    gold_dev: Sequence[EvalQuestion],
    silver_dev: Sequence[EvalQuestion],
    *,
    run_fn: RunFunction = run_questions,
) -> DevTuningResult:
    """Evaluate the fixed grid on dev-only inputs and choose one setting."""
    _validate_dev_questions(gold_dev, expected_set="gold")
    _validate_dev_questions(silver_dev, expected_set="silver")
    if not gold_dev or not silver_dev:
        raise ValueError("gold and silver dev questions must not be empty")

    rows: list[DevGridRow] = []
    for setting in tuning_grid():
        config = RunConfig(
            condition="composite",
            chunking="fixed",
            method=setting.method,
            analyzer="nori",
            candidate_chunks=setting.candidate_chunks,
            bm25_weight=setting.bm25_weight,
        )
        gold_runs = run_fn(client, embedder, gold_dev, config)
        silver_runs = run_fn(client, embedder, silver_dev, config)
        _require_complete_runs(gold_runs, gold_dev, label="gold_dev")
        _require_complete_runs(silver_runs, silver_dev, label="silver_dev")
        gold_mean = math.fsum(run.ndcg10 for run in gold_runs) / len(gold_runs)
        silver_mean = math.fsum(run.ndcg10 for run in silver_runs) / len(silver_runs)
        rows.append(
            DevGridRow(
                method=setting.method,
                candidate_chunks=setting.candidate_chunks,
                bm25_weight=setting.bm25_weight,
                gold_dev_mean_ndcg10=gold_mean,
                silver_dev_mean_ndcg10=silver_mean,
                selection_score=(gold_mean + silver_mean) / 2.0,
            )
        )
    return DevTuningResult(
        rows=tuple(rows),
        selected=select_setting(rows),
        gold_dev_count=len(gold_dev),
        silver_dev_count=len(silver_dev),
    )


def write_dev_manifest(
    result: DevTuningResult,
    embedder: Embedder,
    *,
    root: Path = Path("."),
    generated_at: str | None = None,
    code_commit: str | None = None,
) -> dict[str, object]:
    """Write the complete tuning grid without freezing the selected config."""
    _validate_tuning_result(result)
    if result.gold_dev_count != _EXPECTED_GOLD_DEV:
        raise ValueError(
            f"gold dev question count must be {_EXPECTED_GOLD_DEV}, "
            f"got {result.gold_dev_count}"
        )
    if result.silver_dev_count != _EXPECTED_SILVER_DEV:
        raise ValueError(
            f"silver dev question count must be {_EXPECTED_SILVER_DEV}, "
            f"got {result.silver_dev_count}"
        )
    index_path = root / _INDEX_PATH
    payload: dict[str, object] = {
        "generated_at": generated_at or now_iso(),
        "code_commit": code_commit if code_commit is not None else _git_commit(root),
        "grid_definition": {
            "rank_constant": RANK_CONSTANT,
            "candidate_chunks": list(DEV_CANDIDATE_CHUNKS),
            "bm25_weights": list(DEV_BM25_WEIGHTS),
            "setting_count": 18,
            "condition": "composite",
            "chunking": "fixed",
            "analyzer": "nori",
        },
        "selection_criterion": {
            "score": "(gold_dev_mean_ndcg10 + silver_dev_mean_ndcg10) / 2",
            "tie_score_round_digits": 10,
            "tie_breakers": [
                "hybrid_rrf",
                "smaller_candidate_chunks",
                "smaller_bm25_weight",
            ],
        },
        "question_counts": {
            "gold_dev": result.gold_dev_count,
            "silver_dev": result.silver_dev_count,
        },
        "grid": [asdict(row) for row in result.rows],
        "selected": _selected_payload(result.selected),
        "embedder": {
            "model_id": embedder.model_id,
            "revision": embedder.revision,
            "settings_hash": embedder_settings_hash(embedder),
        },
        "m5_index_sha256": sha256_file(index_path),
    }
    _write_json_atomic(root / _DEV_PATH, payload)
    return payload


def freeze_config(
    *,
    root: Path = Path("."),
    frozen_at: str | None = None,
    code_commit: str | None = None,
) -> dict[str, object]:
    """Create ``m5_config.json`` once from dev selection and fixed manifests."""
    destination = root / _CONFIG_PATH
    if destination.exists():
        raise FileExistsError(f"frozen config already exists: {destination}")

    dev_path = root / _DEV_PATH
    index_path = root / _INDEX_PATH
    eval_inputs_path = root / _EVAL_INPUTS_PATH
    dev = _read_json_object(dev_path)
    index_manifest = _read_json_object(index_path)
    recorded_index_hash = dev.get("m5_index_sha256")
    current_index_hash = sha256_file(index_path)
    if (
        not isinstance(recorded_index_hash, str)
        or recorded_index_hash != current_index_hash
    ):
        raise ValueError("m5_index.json changed after dev tuning")
    selected = _selected_setting(dev)
    chunking = _required_mapping(index_manifest, "chunking", source=index_path)
    fixed_size = _positive_int(chunking.get("fixed_size"), "fixed_size")
    fixed_overlap = _non_negative_int(chunking.get("fixed_overlap"), "fixed_overlap")
    if fixed_overlap >= fixed_size:
        raise ValueError("fixed_overlap must be smaller than fixed_size")
    embedder = _required_mapping(dev, "embedder", source=dev_path)
    embedder_payload = {
        "model_id": _non_empty_str(embedder.get("model_id"), "embedder.model_id"),
        "revision": _non_empty_str(embedder.get("revision"), "embedder.revision"),
        "settings_hash": _non_empty_str(
            embedder.get("settings_hash"), "embedder.settings_hash"
        ),
    }
    payload: dict[str, object] = {
        "hybrid_method": selected.method,
        "bm25_weight": selected.bm25_weight,
        "candidate_chunks": selected.candidate_chunks,
        "rank_constant": RANK_CONSTANT,
        "fixed_size": fixed_size,
        "fixed_overlap": fixed_overlap,
        "embedder": embedder_payload,
        "indexes": _frozen_indexes(index_manifest, source=index_path),
        "m5_eval_inputs_sha256": sha256_file(eval_inputs_path),
        "m5_dev_sha256": sha256_file(dev_path),
        "code_commit": code_commit if code_commit is not None else _git_commit(root),
        "frozen_at": frozen_at or now_iso(),
    }
    payload["config_hash"] = _config_hash(payload)
    _write_json_exclusive(destination, payload)
    return payload


def verify_config(path: Path | str) -> ConfigCheck:
    """Check canonical config and current dev/eval-input artifact hashes."""
    config_path = Path(path)
    mismatches: list[str] = []
    try:
        config = _read_json_object(config_path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return ConfigCheck(("config_file",))

    recorded_hash = config.get("config_hash")
    unhashed = {key: value for key, value in config.items() if key != "config_hash"}
    if not isinstance(recorded_hash, str) or recorded_hash != _config_hash(unhashed):
        mismatches.append("config_hash")

    for field, filename in (
        ("m5_eval_inputs_sha256", "m5_eval_inputs.json"),
        ("m5_dev_sha256", "m5_dev.json"),
    ):
        current_path = config_path.parent / filename
        recorded = config.get(field)
        try:
            current = sha256_file(current_path)
        except OSError:
            current = None
        if not isinstance(recorded, str) or current != recorded:
            mismatches.append(field)
    return ConfigCheck(tuple(mismatches))


def _validate_dev_questions(
    questions: Sequence[EvalQuestion], *, expected_set: str
) -> None:
    for question in questions:
        raw = question.question
        if raw.split != "dev":
            raise ValueError(f"dev tuning rejects non-dev question: {raw.id}")
        if raw.set != expected_set:
            raise ValueError(
                f"{expected_set} dev input contains another set: {raw.id}:{raw.set}"
            )


def _require_complete_runs(
    runs: Sequence[QuestionRun],
    questions: Sequence[EvalQuestion],
    *,
    label: str,
) -> None:
    expected = [question.question.id for question in questions]
    actual = [run.qid for run in runs]
    if actual != expected:
        raise RuntimeError(f"{label} run is incomplete or reordered")


def _validate_tuning_result(result: DevTuningResult) -> None:
    expected = {
        (setting.method, setting.candidate_chunks, setting.bm25_weight)
        for setting in tuning_grid()
    }
    actual = {
        (row.method, row.candidate_chunks, row.bm25_weight) for row in result.rows
    }
    if len(result.rows) != len(expected) or actual != expected:
        raise ValueError("dev result does not contain the fixed 18-setting grid")
    for row in result.rows:
        expected_score = (row.gold_dev_mean_ndcg10 + row.silver_dev_mean_ndcg10) / 2.0
        if row.selection_score != expected_score:
            raise ValueError("dev result contains an invalid selection score")
    if result.selected != select_setting(result.rows):
        raise ValueError("dev result selected setting does not follow the tie rules")


def _selected_payload(row: DevGridRow) -> dict[str, object]:
    return {
        "method": row.method,
        "candidate_chunks": row.candidate_chunks,
        "bm25_weight": row.bm25_weight,
    }


def _selected_setting(dev: Mapping[str, object]) -> TuningSetting:
    selected = _required_mapping(dev, "selected", source=Path("m5_dev.json"))
    method = selected.get("method")
    if method not in {"hybrid_rrf", "hybrid_weighted"}:
        raise ValueError("m5_dev selected.method is invalid")
    candidate_chunks = _positive_int(
        selected.get("candidate_chunks"), "selected.candidate_chunks"
    )
    weight = selected.get("bm25_weight")
    if weight is not None and (
        isinstance(weight, bool) or not isinstance(weight, int | float)
    ):
        raise TypeError("selected.bm25_weight must be numeric or null")
    return TuningSetting(method, candidate_chunks, weight)  # type: ignore[arg-type]


def _frozen_indexes(
    manifest: Mapping[str, object], *, source: Path
) -> dict[str, dict[str, object]]:
    indexes = _required_mapping(manifest, "indexes", source=source)
    result: dict[str, dict[str, object]] = {}
    for spec in m5_index_specs():
        name = index_name(spec.condition, spec.chunking)
        raw = indexes.get(name)
        if not isinstance(raw, Mapping):
            raise TypeError(f"missing M5 index manifest entry: {name}")
        schema_hash = _non_empty_str(
            raw.get("index_field_schema_hash"),
            f"indexes.{name}.index_field_schema_hash",
        )
        chunk_count = _positive_int(
            raw.get("chunk_count"), f"indexes.{name}.chunk_count"
        )
        result[name] = {
            "schema_hash": schema_hash,
            "chunk_count": chunk_count,
        }
    return result


def _config_hash(value: Mapping[str, object]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON object required: {path}")
    return value


def _required_mapping(
    value: Mapping[str, object], key: str, *, source: Path
) -> Mapping[str, object]:
    result = value.get(key)
    if not isinstance(result, Mapping):
        raise TypeError(f"{key} object required: {source}")
    return result


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _non_negative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _non_empty_str(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
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


def _write_json_exclusive(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with path.open("x", encoding="utf-8") as destination:
        destination.write(serialized)


def _git_commit(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    return commit or None


def _load_dev_questions(root: Path) -> tuple[list[EvalQuestion], list[EvalQuestion]]:
    targets = load_retrieval_targets(root)
    raw_gold = targets["gold_dev"]
    raw_silver = targets["silver_dev"]
    documents, _report = load_corpus("composite", root=root)
    docs_by_id = {document.doc_id: document for document in documents}
    table_manifest = _read_json_object(root / "data/manifest/table_golden.json")
    return (
        build_eval_questions(raw_gold, docs_by_id, table_golden=table_manifest),
        build_eval_questions(raw_silver, docs_by_id, table_golden=table_manifest),
    )


def _require_registered_dev_counts(
    gold_dev: Sequence[EvalQuestion], silver_dev: Sequence[EvalQuestion]
) -> None:
    if len(gold_dev) != _EXPECTED_GOLD_DEV:
        raise ValueError(
            f"gold dev question count must be {_EXPECTED_GOLD_DEV}, got {len(gold_dev)}"
        )
    if len(silver_dev) != _EXPECTED_SILVER_DEV:
        raise ValueError(
            "silver dev question count must be "
            f"{_EXPECTED_SILVER_DEV}, got {len(silver_dev)}"
        )


def _make_embedder(name: str, *, root: Path) -> Embedder:
    inner: Embedder = FakeEmbedder() if name == "fake" else Bge3Embedder()
    return CachedEmbedder(inner, root / "data/interim/m5/embedding_cache")


def main(argv: Sequence[str] | None = None) -> int:
    """Run dev selection, or separately freeze an already-selected config."""
    parser = argparse.ArgumentParser(description="M5 dev hybrid 튜닝과 설정 고정")
    parser.add_argument("--embedder", choices=("bge-m3", "fake"), default="bge-m3")
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    try:
        if args.freeze:
            payload = freeze_config(root=args.root)
            print(
                f"M5 설정 고정 config_hash={payload['config_hash']} "
                f"path={args.root / _CONFIG_PATH}",
                flush=True,
            )
            return 0

        gold_dev, silver_dev = _load_dev_questions(args.root)
        _require_registered_dev_counts(gold_dev, silver_dev)
        embedder = _make_embedder(args.embedder, root=args.root)
        embedder.prepare()
        with OpenSearchClient() as client:
            result = run_dev_tuning(
                client,
                embedder,
                gold_dev,
                silver_dev,
            )
        payload = write_dev_manifest(result, embedder, root=args.root)
        selected = payload["selected"]
        print(
            "M5 dev 선택 " + json.dumps(selected, ensure_ascii=False, sort_keys=True),
            flush=True,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - bounded CLI failure report
        detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        print(f"M5 dev 실행 실패: {detail}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
