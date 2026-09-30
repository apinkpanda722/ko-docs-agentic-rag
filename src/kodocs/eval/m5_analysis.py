"""Pure analysis functions for preregistered M5 retrieval experiments.

The module accepts already-computed :class:`QuestionRun` values and returns
JSON-serializable dictionaries.  It deliberately performs no file, network,
OpenSearch, or model access.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from kodocs.eval.m5_runner import QuestionRun, RunConfig
from kodocs.eval.m5_stats import (
    BootstrapResult,
    describe,
    independent_bootstrap,
    paired_bootstrap,
    verdict,
)

_RETRIEVAL_TYPES = frozenset(
    {"identifier", "semantic", "table", "multi_doc", "temporal"}
)
_H7_TYPES = frozenset({"identifier", "semantic", "multi_doc"})
_TEMPORAL_TAGS = ("temporal:revision", "temporal:as_of_event")
_CONDITIONS = ("pypdf", "upstage", "composite")


@dataclass(frozen=True)
class RunKey:
    """Identify one configuration run for a question set and split.

    ``selected_hybrid`` marks the dev-frozen hybrid method and parameters.
    Both its fixed and structural runs carry the mark; analyzer and chunking
    are intentionally not part of the selected-setting identity.
    """

    question_set: str
    split: str
    config: RunConfig
    selected_hybrid: bool = False


@dataclass(frozen=True)
class QuestionMeta:
    """Only the immutable question attributes needed by M5 analysis."""

    qid: str
    question_set: str
    source: str
    question_type: str
    tags: tuple[str, ...]
    repeated: bool
    split: str


RunMapping = Mapping[RunKey | RunConfig, Sequence[QuestionRun]]
MetaMapping = Mapping[str, QuestionMeta | Mapping[str, Any] | object]


def ladder(runs: RunMapping, qids: Sequence[str]) -> dict[str, Any]:
    """Return the four-step M5 ladder and adjacent paired differences."""
    ids = _qid_tuple(qids)
    matrix, selected = _prepare_runs(runs)
    configs = (
        (
            "standard_bm25_fixed",
            _find_config(matrix, "fixed", "bm25", "standard"),
        ),
        ("nori_bm25_fixed", _find_config(matrix, "fixed", "bm25", "nori")),
        (
            "nori_hybrid_fixed",
            _find_selected_hybrid(matrix, selected, "fixed", "nori"),
        ),
        (
            "nori_hybrid_structural",
            _find_selected_hybrid(matrix, selected, "structural", "nori"),
        ),
    )
    rows: list[dict[str, Any]] = []
    previous: RunConfig | None = None
    for identifier, config in configs:
        aligned = _aligned(matrix, config, ids)
        row: dict[str, Any] = {
            "id": identifier,
            "mean_recall10": _mean(run.recall10 for run in aligned),
            "mean_ndcg10": _mean(run.ndcg10 for run in aligned),
            "delta_from_previous": None,
        }
        if previous is not None:
            row["delta_from_previous"] = {
                "recall10": _paired_payload(matrix, config, previous, ids, "recall10"),
                "ndcg10": _paired_payload(matrix, config, previous, ids, "ndcg10"),
            }
        rows.append(row)
        previous = config
    return {"n": len(ids), "stages": rows}


def ablation(runs: RunMapping, qids: Sequence[str]) -> dict[str, Any]:
    """Remove structural chunking, hybrid search, and nori one at a time."""
    ids = _qid_tuple(qids)
    matrix, selected = _prepare_runs(runs)
    final = _find_selected_hybrid(matrix, selected, "structural", "nori")
    removals = (
        (
            "structural",
            "nori_hybrid_fixed",
            _find_selected_hybrid(matrix, selected, "fixed", "nori"),
        ),
        (
            "hybrid",
            "nori_bm25_structural",
            _find_config(matrix, "structural", "bm25", "nori"),
        ),
        (
            "nori",
            "standard_hybrid_structural",
            _find_selected_hybrid(matrix, selected, "structural", "standard"),
        ),
    )
    final_runs = _aligned(matrix, final, ids)
    rows = []
    for element, label, removed in removals:
        removed_runs = _aligned(matrix, removed, ids)
        rows.append(
            {
                "element": element,
                "configuration": label,
                "mean_recall10": _mean(run.recall10 for run in removed_runs),
                "mean_ndcg10": _mean(run.ndcg10 for run in removed_runs),
                "delta_final_minus_removed": {
                    "recall10": _paired_payload(
                        matrix, final, removed, ids, "recall10"
                    ),
                    "ndcg10": _paired_payload(matrix, final, removed, ids, "ndcg10"),
                },
            }
        )
    return {
        "n": len(ids),
        "final": {
            "configuration": "nori_hybrid_structural",
            "mean_recall10": _mean(run.recall10 for run in final_runs),
            "mean_ndcg10": _mean(run.ndcg10 for run in final_runs),
        },
        "removals": rows,
    }


def full_grid(runs: RunMapping, qids: Sequence[str]) -> dict[str, Any]:
    """Aggregate the complete 14-run composite matrix."""
    ids = _qid_tuple(qids)
    matrix, selected = _prepare_runs(runs)
    configs = [
        config
        for config in matrix
        if config.condition == "composite" and _covers(matrix[config], ids)
    ]
    if len(configs) != 14:
        raise ValueError(f"full grid must contain 14 combinations, got {len(configs)}")
    rows = []
    for config in sorted(configs, key=_config_sort_key):
        aligned = _aligned(matrix, config, ids)
        span_values = [
            float(run.span_hit) for run in aligned if run.span_hit is not None
        ]
        rows.append(
            {
                "condition": config.condition,
                "chunking": config.chunking,
                "method": config.method,
                "analyzer": config.analyzer,
                "candidate_chunks": config.candidate_chunks,
                "bm25_weight": config.bm25_weight,
                "selected_hybrid": _hybrid_identity(config) == selected,
                "n": len(ids),
                "mean_recall10": _mean(run.recall10 for run in aligned),
                "mean_ndcg10": _mean(run.ndcg10 for run in aligned),
                "span_hit_rate": _mean(span_values) if span_values else None,
                "span_n": len(span_values),
                "top1_hit_rate": _mean(float(run.top1) for run in aligned),
            }
        )
    return {"n": len(ids), "rows": rows}


def hypotheses(
    runs: RunMapping,
    meta: MetaMapping,
    lookup_top1: Mapping[str, bool],
) -> dict[str, Any]:
    """Apply the preregistered H1, H2, H3, H4, and H7 decision rules."""
    metadata = _metadata(meta)
    gold_ids = _meta_qids(
        metadata,
        question_set="gold",
        split="test",
        question_types=_RETRIEVAL_TYPES,
    )
    matrix, selected = _prepare_runs(runs)
    standard = _find_config(matrix, "fixed", "bm25", "standard")
    nori = _find_config(matrix, "fixed", "bm25", "nori")
    hybrid_fixed = _find_selected_hybrid(matrix, selected, "fixed", "nori")
    hybrid_structural = _find_selected_hybrid(matrix, selected, "structural", "nori")

    h1 = _comparison(
        matrix,
        nori,
        standard,
        gold_ids,
        "recall10",
        "nori BM25 고정 - standard BM25 고정",
        with_verdict=True,
    )
    h2 = _comparison(
        matrix,
        hybrid_fixed,
        nori,
        gold_ids,
        "ndcg10",
        "nori hybrid 고정 - nori BM25 고정",
        with_verdict=True,
    )
    h3 = _span_comparison(
        matrix,
        hybrid_structural,
        hybrid_fixed,
        gold_ids,
        with_verdict=True,
    )

    gold_identifier = tuple(
        qid for qid in gold_ids if metadata[qid].question_type == "identifier"
    )
    h4 = _h4(
        matrix,
        hybrid_structural,
        gold_identifier,
        _meta_qids(
            metadata,
            question_set="silver",
            split="test",
            question_types=frozenset({"identifier"}),
        ),
        lookup_top1,
    )

    h7_ids = tuple(qid for qid in gold_ids if metadata[qid].question_type in _H7_TYPES)
    synthetic = tuple(qid for qid in h7_ids if metadata[qid].source == "synthetic")
    real = tuple(qid for qid in h7_ids if metadata[qid].source == "real")
    if len(synthetic) != 8 or len(real) != 6:
        raise ValueError(
            "H7 requires preregistered counts synthetic=8 and real=6, "
            f"got synthetic={len(synthetic)} and real={len(real)}"
        )
    final_by_qid = _run_index(_aligned(matrix, hybrid_structural, gold_ids))
    synthetic_values = [final_by_qid[qid].recall10 for qid in synthetic]
    real_values = [final_by_qid[qid].recall10 for qid in real]
    h7_statistic = independent_bootstrap(synthetic_values, real_values)
    all_source = {
        source: _describe_or_empty(
            [
                final_by_qid[qid].recall10
                for qid in gold_ids
                if metadata[qid].source == source
            ]
        )
        for source in ("synthetic", "real")
    }
    h7 = {
        "comparison": "합성 - 실제",
        "metric": "recall10",
        "n_synthetic": len(synthetic),
        "n_real": len(real),
        "synthetic": describe(synthetic_values),
        "real": describe(real_values),
        "statistic": _bootstrap_dict(h7_statistic),
        "verdict": verdict(h7_statistic),
        "all_types_descriptive": all_source,
    }
    return {"H1": h1, "H2": h2, "H3": h3, "H4": h4, "H7": h7}


def h2_breakdown(runs: RunMapping, meta: MetaMapping) -> dict[str, Any]:
    """Describe H2 item differences by set, source, and repetition tag."""
    metadata = _metadata(meta)
    matrix, selected = _prepare_runs(runs)
    nori = _find_config(matrix, "fixed", "bm25", "nori")
    hybrid = _find_selected_hybrid(matrix, selected, "fixed", "nori")
    result: dict[str, Any] = {"label": "기술 통계"}
    for question_set in ("gold", "silver"):
        ids = _meta_qids(
            metadata,
            question_set=question_set,
            split="test",
            question_types=_RETRIEVAL_TYPES,
        )
        differences = _differences(matrix, hybrid, nori, ids, "ndcg10")
        groups: dict[str, dict[str, dict[str, int | float | None]]] = {}
        for source in ("real", "synthetic"):
            groups[source] = {}
            for repeated, label in ((True, "repeated"), (False, "not_repeated")):
                values = [
                    differences[qid]
                    for qid in ids
                    if metadata[qid].source == source
                    and metadata[qid].repeated is repeated
                ]
                groups[source][label] = _describe_or_empty(values)
        result[f"{question_set}_test"] = {
            "n": len(ids),
            "by_source_and_answer_repeated": groups,
        }
    return result


def h3_breakdown(runs: RunMapping, meta: MetaMapping) -> dict[str, Any]:
    """Describe H3 span-hit effects by source without a hypothesis verdict."""
    metadata = _metadata(meta)
    matrix, selected = _prepare_runs(runs)
    structural = _find_selected_hybrid(matrix, selected, "structural", "nori")
    fixed = _find_selected_hybrid(matrix, selected, "fixed", "nori")
    result: dict[str, Any] = {"label": "기술 통계"}
    for question_set in ("gold", "silver"):
        ids = _meta_qids(
            metadata,
            question_set=question_set,
            split="test",
            question_types=_RETRIEVAL_TYPES,
        )
        structural_by_qid = _run_index(_aligned(matrix, structural, ids))
        fixed_by_qid = _run_index(_aligned(matrix, fixed, ids))
        by_source: dict[str, Any] = {}
        for source in ("real", "synthetic"):
            source_ids = tuple(qid for qid in ids if metadata[qid].source == source)
            excluded = [
                qid
                for qid in source_ids
                if structural_by_qid[qid].span_hit is None
                or fixed_by_qid[qid].span_hit is None
            ]
            valid = tuple(qid for qid in source_ids if qid not in set(excluded))
            structural_values = [
                float(structural_by_qid[qid].span_hit) for qid in valid
            ]
            fixed_values = [float(fixed_by_qid[qid].span_hit) for qid in valid]
            by_source[source] = {
                "n": len(source_ids),
                "metric_n": len(valid),
                "structural": _describe_or_empty(structural_values),
                "fixed": _describe_or_empty(fixed_values),
                "difference": _describe_or_empty(
                    [
                        left - right
                        for left, right in zip(structural_values, fixed_values)
                    ]
                ),
                "excluded_no_span": {"n": len(excluded), "qids": excluded},
            }
        result[f"{question_set}_test"] = by_source
    return result


def corpus_v2_rule(silver_test_runs: RunMapping, meta: MetaMapping) -> dict[str, Any]:
    """Apply the preregistered silver-test corpus-v2 initiation rule."""
    metadata = _metadata(meta)
    ids = _meta_qids(
        metadata,
        question_set="silver",
        split="test",
        question_types=_RETRIEVAL_TYPES,
    )
    matrix, selected = _prepare_runs(silver_test_runs)
    nori = _find_config(matrix, "fixed", "bm25", "nori")
    hybrid = _find_selected_hybrid(matrix, selected, "fixed", "nori")
    differences = _differences(matrix, hybrid, nori, ids, "ndcg10")
    not_repeated = [differences[qid] for qid in ids if not metadata[qid].repeated]
    repeated = [differences[qid] for qid in ids if metadata[qid].repeated]
    statistic = (
        independent_bootstrap(not_repeated, repeated)
        if not_repeated and repeated
        else None
    )
    if len(not_repeated) < 10 or len(repeated) < 10:
        decision = "판정 불가(표본 부족) — 코퍼스 v2 미착수, 한계로 기록"
    elif statistic is not None and statistic.ci_low >= 0.10:
        decision = "코퍼스 v2 별도 phase 제안"
    else:
        decision = "코퍼스 v2 미착수, 한계로만 기록"
    return {
        "comparison": "반복 아님 - 반복",
        "group_sizes": {
            "not_repeated": len(not_repeated),
            "repeated": len(repeated),
        },
        "not_repeated": _describe_or_empty(not_repeated),
        "repeated": _describe_or_empty(repeated),
        "statistic": _bootstrap_dict(statistic) if statistic is not None else None,
        "ci_low": statistic.ci_low if statistic is not None else None,
        "decision": decision,
    }


def temporal_prior_vs_current(
    runs: RunMapping,
    prior_map: Mapping[str, Any],
    questions: Mapping[str, Any] | Sequence[Any],
) -> dict[str, Any]:
    """Report prior/current ranks for every gold temporal-revision question."""
    pairs = _prior_pairs(prior_map)
    records = _question_records(questions)
    selected_questions = [
        (qid, value)
        for qid, value in records.items()
        if "temporal:revision" in _question_tags(value)
    ]
    ids = tuple(sorted(qid for qid, _value in selected_questions))
    matrix, selected = _prepare_runs(runs)
    final = _find_selected_hybrid(matrix, selected, "structural", "nori")
    by_qid = _run_index(_aligned(matrix, final, ids))
    rows = []
    for qid, question in sorted(selected_questions):
        positive_docs = _positive_qrel_docs(question)
        current_candidates = [doc_id for doc_id in positive_docs if doc_id in pairs]
        if len(current_candidates) != 1:
            raise ValueError(
                f"temporal question {qid} must identify one current document in prior_map"
            )
        current = current_candidates[0]
        prior = pairs[current]
        ranking = by_qid[qid].ranked_docs
        current_rank = _rank(ranking, current)
        prior_rank = _rank(ranking, prior)
        rows.append(
            {
                "qid": qid,
                "split": _question_split(question),
                "current_doc_id": current,
                "prior_doc_id": prior,
                "current_rank": current_rank,
                "prior_rank": prior_rank,
                "prior_above_current": prior_rank is not None
                and (current_rank is None or prior_rank < current_rank),
            }
        )
    by_split = {}
    for split in ("test", "dev"):
        subset = [row for row in rows if row["split"] == split]
        by_split[split] = {
            "n": len(subset),
            "prior_above_current_count": sum(
                bool(row["prior_above_current"]) for row in subset
            ),
        }
    return {"label": "기술 통계", "rows": rows, "by_split": by_split}


def parser_compare(
    runs_by_condition: Mapping[str, Sequence[QuestionRun]],
    qids_real19: Sequence[str],
    qids_real_test13: Sequence[str],
) -> dict[str, Any]:
    """Compare the three parser conditions as a labeled exploratory analysis."""
    all_ids = _qid_tuple(qids_real19)
    test_ids = _qid_tuple(qids_real_test13)
    if len(all_ids) != 19:
        raise ValueError(
            f"parser comparison requires 19 real questions, got {len(all_ids)}"
        )
    if len(test_ids) != 13:
        raise ValueError(
            f"parser test comparison requires 13 real questions, got {len(test_ids)}"
        )
    if not set(test_ids) <= set(all_ids):
        raise ValueError(
            "the 13 test questions must be a subset of the 19 real questions"
        )
    if any(condition not in runs_by_condition for condition in _CONDITIONS):
        raise ValueError("parser comparison requires pypdf, upstage, and composite")
    indexed = {
        condition: _run_index(runs_by_condition[condition]) for condition in _CONDITIONS
    }
    for condition, values in indexed.items():
        missing = sorted(set(all_ids) - values.keys())
        if missing:
            raise ValueError(f"{condition} is missing parser question runs: {missing}")
    return {
        "label": "탐색적 비교(사전 등록 가설 아님)",
        "subsets": {
            "real_all_19": _parser_subset(indexed, all_ids),
            "real_test_13": _parser_subset(indexed, test_ids),
        },
    }


def silver_direction(
    runs: RunMapping,
    qids: Sequence[str] | Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    """Compute silver-test ladder and H1-H3 comparisons without verdicts.

    A mapping may provide ``all`` plus the two temporal tag keys.  A plain
    sequence remains accepted for callers that only need the overall table;
    its temporal groups are reported as empty because tags cannot be inferred
    from retrieval runs alone.
    """
    if isinstance(qids, Mapping):
        all_ids = _qid_tuple(qids.get("all", ()))
        tag_ids = {
            tag: _qid_tuple(qids.get(tag, ()), allow_empty=True)
            for tag in _TEMPORAL_TAGS
        }
    else:
        all_ids = _qid_tuple(qids)
        tag_ids = {tag: () for tag in _TEMPORAL_TAGS}
    return {
        "label": "방향 확인용",
        "all": _silver_group(runs, all_ids),
        "temporal_tags": {
            tag: _silver_group(runs, ids) if ids else {"n": 0}
            for tag, ids in tag_ids.items()
        },
    }


def lexical_mismatch_subset(runs: RunMapping, meta: MetaMapping) -> dict[str, Any]:
    """Describe H2 metrics on lexical-mismatch-tagged questions only."""
    metadata = _metadata(meta)
    matrix, selected = _prepare_runs(runs)
    nori = _find_config(matrix, "fixed", "bm25", "nori")
    hybrid = _find_selected_hybrid(matrix, selected, "fixed", "nori")
    result: dict[str, Any] = {"label": "기술 통계"}
    for question_set in ("gold", "silver"):
        ids = tuple(
            qid
            for qid in _meta_qids(
                metadata,
                question_set=question_set,
                split="test",
                question_types=_RETRIEVAL_TYPES,
            )
            if "lexical_mismatch" in metadata[qid].tags
        )
        if not ids:
            result[f"{question_set}_test"] = {
                "n": 0,
                "bm25": _describe_or_empty([]),
                "hybrid": _describe_or_empty([]),
                "difference": _describe_or_empty([]),
            }
            continue
        bm25_runs = _run_index(_aligned(matrix, nori, ids))
        hybrid_runs = _run_index(_aligned(matrix, hybrid, ids))
        bm25_values = [bm25_runs[qid].ndcg10 for qid in ids]
        hybrid_values = [hybrid_runs[qid].ndcg10 for qid in ids]
        result[f"{question_set}_test"] = {
            "n": len(ids),
            "bm25": describe(bm25_values),
            "hybrid": describe(hybrid_values),
            "difference": describe(
                [left - right for left, right in zip(hybrid_values, bm25_values)]
            ),
        }
    return result


def _prepare_runs(
    runs: RunMapping,
) -> tuple[dict[RunConfig, dict[str, QuestionRun]], tuple[str, int, float | None]]:
    matrix: dict[RunConfig, dict[str, QuestionRun]] = {}
    marked: set[tuple[str, int, float | None]] = set()
    for key, values in runs.items():
        if isinstance(key, RunKey):
            config = key.config
            if key.selected_hybrid and config.method.startswith("hybrid_"):
                marked.add(_hybrid_identity(config))
        elif isinstance(key, RunConfig):
            config = key
        else:
            raise TypeError("run keys must be RunKey or RunConfig")
        destination = matrix.setdefault(config, {})
        for qid, run in _run_index(values).items():
            if qid in destination:
                raise ValueError(f"duplicate run for configuration and qid: {qid}")
            destination[qid] = run
    if len(marked) > 1:
        raise ValueError("runs mark more than one selected hybrid setting")
    if marked:
        selected = next(iter(marked))
    else:
        available = {
            _hybrid_identity(config)
            for config in matrix
            if config.method.startswith("hybrid_")
        }
        if len(available) != 1:
            raise ValueError("selected hybrid setting is ambiguous")
        selected = next(iter(available))
    return matrix, selected


def _find_config(
    matrix: Mapping[RunConfig, object],
    chunking: str,
    method: str,
    analyzer: str,
) -> RunConfig:
    matches = [
        config
        for config in matrix
        if config.condition == "composite"
        and config.chunking == chunking
        and config.method == method
        and config.analyzer == analyzer
    ]
    if len(matches) != 1:
        raise ValueError(
            "expected one composite configuration for "
            f"{chunking}/{method}/{analyzer}, got {len(matches)}"
        )
    return matches[0]


def _find_selected_hybrid(
    matrix: Mapping[RunConfig, object],
    selected: tuple[str, int, float | None],
    chunking: str,
    analyzer: str,
) -> RunConfig:
    matches = [
        config
        for config in matrix
        if config.condition == "composite"
        and config.chunking == chunking
        and config.analyzer == analyzer
        and _hybrid_identity(config) == selected
    ]
    if len(matches) != 1:
        raise ValueError(
            "expected one selected hybrid configuration for "
            f"{chunking}/{analyzer}, got {len(matches)}"
        )
    return matches[0]


def _hybrid_identity(config: RunConfig) -> tuple[str, int, float | None]:
    return (config.method, config.candidate_chunks, config.bm25_weight)


def _aligned(
    matrix: Mapping[RunConfig, Mapping[str, QuestionRun]],
    config: RunConfig,
    qids: Sequence[str],
) -> list[QuestionRun]:
    values = matrix[config]
    missing = [qid for qid in qids if qid not in values]
    if missing:
        raise ValueError(f"configuration is missing question runs: {missing}")
    return [values[qid] for qid in qids]


def _covers(values: Mapping[str, QuestionRun], qids: Sequence[str]) -> bool:
    return all(qid in values for qid in qids)


def _paired_payload(
    matrix: Mapping[RunConfig, Mapping[str, QuestionRun]],
    left: RunConfig,
    right: RunConfig,
    qids: Sequence[str],
    metric: str,
) -> dict[str, int | float]:
    left_runs = _aligned(matrix, left, qids)
    right_runs = _aligned(matrix, right, qids)
    result = paired_bootstrap(
        [float(getattr(run, metric)) for run in left_runs],
        [float(getattr(run, metric)) for run in right_runs],
    )
    return _bootstrap_dict(result)


def _comparison(
    matrix: Mapping[RunConfig, Mapping[str, QuestionRun]],
    left: RunConfig,
    right: RunConfig,
    qids: Sequence[str],
    metric: str,
    label: str,
    *,
    with_verdict: bool,
) -> dict[str, Any]:
    ids = _qid_tuple(qids)
    left_values = [float(getattr(run, metric)) for run in _aligned(matrix, left, ids)]
    right_values = [float(getattr(run, metric)) for run in _aligned(matrix, right, ids)]
    statistic = paired_bootstrap(left_values, right_values)
    result: dict[str, Any] = {
        "comparison": label,
        "metric": metric,
        "n": len(ids),
        "left_mean": _mean(left_values),
        "right_mean": _mean(right_values),
        "statistic": _bootstrap_dict(statistic),
    }
    if with_verdict:
        result["verdict"] = verdict(statistic)
    return result


def _span_comparison(
    matrix: Mapping[RunConfig, Mapping[str, QuestionRun]],
    structural: RunConfig,
    fixed: RunConfig,
    qids: Sequence[str],
    *,
    with_verdict: bool,
) -> dict[str, Any]:
    structural_by_qid = _run_index(_aligned(matrix, structural, qids))
    fixed_by_qid = _run_index(_aligned(matrix, fixed, qids))
    excluded = [
        qid
        for qid in qids
        if structural_by_qid[qid].span_hit is None or fixed_by_qid[qid].span_hit is None
    ]
    valid = tuple(qid for qid in qids if qid not in set(excluded))
    if not valid:
        raise ValueError("H3 has no questions with answer spans")
    structural_values = [float(structural_by_qid[qid].span_hit) for qid in valid]
    fixed_values = [float(fixed_by_qid[qid].span_hit) for qid in valid]
    statistic = paired_bootstrap(structural_values, fixed_values)
    result: dict[str, Any] = {
        "comparison": "nori hybrid 구조 - nori hybrid 고정",
        "metric": "span_hit",
        "n": len(valid),
        "structural_mean": _mean(structural_values),
        "fixed_mean": _mean(fixed_values),
        "statistic": _bootstrap_dict(statistic),
        "excluded_no_span": {"n": len(excluded), "qids": excluded},
    }
    if with_verdict:
        result["verdict"] = verdict(statistic)
    return result


def _h4(
    matrix: Mapping[RunConfig, Mapping[str, QuestionRun]],
    final: RunConfig,
    gold_ids: Sequence[str],
    silver_ids: Sequence[str],
    lookup_top1: Mapping[str, bool],
) -> dict[str, Any]:
    gold = _qid_tuple(gold_ids)
    missing = [qid for qid in [*gold, *silver_ids] if qid not in lookup_top1]
    if missing:
        raise ValueError(f"lookup_top1 is missing identifier questions: {missing}")
    final_gold = _run_index(_aligned(matrix, final, gold))
    keyword = [float(lookup_top1[qid]) for qid in gold]
    body = [float(final_gold[qid].top1) for qid in gold]
    statistic = paired_bootstrap(keyword, body)
    silver = _qid_tuple(silver_ids)
    final_silver = _run_index(_aligned(matrix, final, silver)) if silver else {}
    silver_keyword = [float(lookup_top1[qid]) for qid in silver]
    silver_body = [float(final_silver[qid].top1) for qid in silver]
    return {
        "comparison": "keyword - 최종 구성 본문 검색",
        "metric": "top1_hit",
        "n": len(gold),
        "keyword_top1_rate": _mean(keyword),
        "body_top1_rate": _mean(body),
        "statistic": _bootstrap_dict(statistic),
        "verdict": verdict(statistic),
        "note": "표본 4문항이라 CI가 넓어 사실상 판정 불가",
        "silver_test_direction": {
            "label": "기술 통계",
            "n": len(silver),
            "keyword_top1_rate": _mean(silver_keyword) if silver else None,
            "body_top1_rate": _mean(silver_body) if silver else None,
            "difference": _describe_or_empty(
                [left - right for left, right in zip(silver_keyword, silver_body)]
            ),
        },
    }


def _silver_group(runs: RunMapping, qids: Sequence[str]) -> dict[str, Any]:
    ids = _qid_tuple(qids)
    matrix, selected = _prepare_runs(runs)
    standard = _find_config(matrix, "fixed", "bm25", "standard")
    nori = _find_config(matrix, "fixed", "bm25", "nori")
    fixed = _find_selected_hybrid(matrix, selected, "fixed", "nori")
    structural = _find_selected_hybrid(matrix, selected, "structural", "nori")
    return {
        "n": len(ids),
        "ladder": ladder(runs, ids),
        "H1": _comparison(
            matrix,
            nori,
            standard,
            ids,
            "recall10",
            "nori BM25 고정 - standard BM25 고정",
            with_verdict=False,
        ),
        "H2": _comparison(
            matrix,
            fixed,
            nori,
            ids,
            "ndcg10",
            "nori hybrid 고정 - nori BM25 고정",
            with_verdict=False,
        ),
        "H3": _span_comparison(matrix, structural, fixed, ids, with_verdict=False),
    }


def _differences(
    matrix: Mapping[RunConfig, Mapping[str, QuestionRun]],
    left: RunConfig,
    right: RunConfig,
    qids: Sequence[str],
    metric: str,
) -> dict[str, float]:
    left_by_qid = _run_index(_aligned(matrix, left, qids))
    right_by_qid = _run_index(_aligned(matrix, right, qids))
    return {
        qid: float(getattr(left_by_qid[qid], metric))
        - float(getattr(right_by_qid[qid], metric))
        for qid in qids
    }


def _parser_subset(
    indexed: Mapping[str, Mapping[str, QuestionRun]], qids: Sequence[str]
) -> dict[str, Any]:
    conditions = {}
    for condition in _CONDITIONS:
        values = indexed[condition]
        conditions[condition] = {
            "recall10": describe([values[qid].recall10 for qid in qids]),
            "ndcg10": describe([values[qid].ndcg10 for qid in qids]),
        }
    pairs = (
        ("composite_minus_pypdf", "composite", "pypdf"),
        ("composite_minus_upstage", "composite", "upstage"),
        ("pypdf_minus_upstage", "pypdf", "upstage"),
    )
    comparisons = {}
    for label, left, right in pairs:
        comparisons[label] = {
            metric: _bootstrap_dict(
                paired_bootstrap(
                    [float(getattr(indexed[left][qid], metric)) for qid in qids],
                    [float(getattr(indexed[right][qid], metric)) for qid in qids],
                )
            )
            for metric in ("recall10", "ndcg10")
        }
    return {
        "n": len(qids),
        "conditions": conditions,
        "paired_comparisons": comparisons,
    }


def _metadata(meta: MetaMapping) -> dict[str, QuestionMeta]:
    return {qid: _coerce_meta(qid, value) for qid, value in meta.items()}


def _coerce_meta(
    qid: str, value: QuestionMeta | Mapping[str, Any] | object
) -> QuestionMeta:
    if isinstance(value, QuestionMeta):
        if value.qid != qid:
            raise ValueError(f"metadata key does not match qid: {qid}")
        return value
    repeated = getattr(value, "repeated", None)
    question = getattr(value, "question", value)
    if isinstance(question, Mapping):
        get = question.get
    else:
        get = lambda name, default=None: getattr(question, name, default)
    question_id = get("id", qid)
    question_set = get("question_set", get("set"))
    question_type = get("question_type", get("type"))
    source = get("source")
    tags = get("tags", ())
    split = get("split")
    if isinstance(value, Mapping):
        repeated = value.get("repeated", value.get("answer_repeated", repeated))
    required = {
        "qid": question_id,
        "question_set": question_set,
        "source": source,
        "question_type": question_type,
        "split": split,
    }
    missing = [name for name, field_value in required.items() if not field_value]
    if missing or not isinstance(repeated, bool):
        raise ValueError(f"incomplete metadata for {qid}: {missing or ['repeated']}")
    return QuestionMeta(
        qid=str(question_id),
        question_set=str(question_set),
        source=str(source),
        question_type=str(question_type),
        tags=tuple(str(tag) for tag in tags),
        repeated=repeated,
        split=str(split),
    )


def _meta_qids(
    metadata: Mapping[str, QuestionMeta],
    *,
    question_set: str,
    split: str,
    question_types: frozenset[str],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            qid
            for qid, item in metadata.items()
            if item.question_set == question_set
            and item.split == split
            and item.question_type in question_types
        )
    )


def _prior_pairs(prior_map: Mapping[str, Any]) -> dict[str, str]:
    versions = prior_map.get("versions")
    if isinstance(versions, Sequence) and not isinstance(versions, (str, bytes)):
        result = {}
        for value in versions:
            if not isinstance(value, Mapping):
                raise TypeError("prior version entries must be mappings")
            current = value.get("current_serial")
            prior = value.get("prior_serial")
            if not isinstance(current, str) or not isinstance(prior, str):
                raise TypeError("prior version serials must be strings")
            result[f"law-{current}"] = f"law-{prior}"
        return result
    result = {}
    for current, prior in prior_map.items():
        if not isinstance(current, str) or not isinstance(prior, str):
            raise TypeError("prior_map must map document ids to document ids")
        result[current] = prior
    return result


def _question_records(
    questions: Mapping[str, Any] | Sequence[Any],
) -> dict[str, Any]:
    if isinstance(questions, Mapping):
        return dict(questions)
    result = {}
    for value in questions:
        question = getattr(value, "question", value)
        qid = question.get("id") if isinstance(question, Mapping) else question.id
        if qid in result:
            raise ValueError(f"duplicate question id: {qid}")
        result[str(qid)] = value
    return result


def _question_value(question: Any, field: str, default: Any = None) -> Any:
    nested = getattr(question, "question", question)
    if isinstance(nested, Mapping):
        return nested.get(field, default)
    return getattr(nested, field, default)


def _question_tags(question: Any) -> tuple[str, ...]:
    return tuple(_question_value(question, "tags", ()))


def _question_split(question: Any) -> str | None:
    value = _question_value(question, "split")
    return str(value) if value is not None else None


def _positive_qrel_docs(question: Any) -> list[str]:
    result = []
    for qrel in _question_value(question, "qrels", ()):
        if isinstance(qrel, Mapping):
            doc_id, relevance = qrel.get("doc_id"), qrel.get("rel")
        else:
            doc_id, relevance = qrel.doc_id, qrel.rel
        if isinstance(doc_id, str) and isinstance(relevance, int) and relevance >= 1:
            result.append(doc_id)
    return list(dict.fromkeys(result))


def _rank(ranking: Sequence[str], doc_id: str) -> int | None:
    try:
        return ranking.index(doc_id) + 1
    except ValueError:
        return None


def _run_index(runs: Sequence[QuestionRun]) -> dict[str, QuestionRun]:
    result = {}
    for run in runs:
        if run.qid in result:
            raise ValueError(f"duplicate question run: {run.qid}")
        result[run.qid] = run
    return result


def _qid_tuple(qids: Sequence[str], *, allow_empty: bool = False) -> tuple[str, ...]:
    result = tuple(qids)
    if not result and not allow_empty:
        raise ValueError("question ids must not be empty")
    if len(result) != len(set(result)):
        raise ValueError("question ids must be unique")
    return result


def _describe_or_empty(values: Sequence[float]) -> dict[str, int | float | None]:
    if values:
        return describe(values)
    return {"n": 0, "mean": None, "sd": None, "median": None, "min": None, "max": None}


def _bootstrap_dict(result: BootstrapResult) -> dict[str, int | float]:
    return asdict(result)


def _mean(values: Sequence[float] | Any) -> float:
    numbers = tuple(float(value) for value in values)
    if not numbers:
        raise ValueError("cannot calculate a mean of an empty sequence")
    return math.fsum(numbers) / len(numbers)


def _config_sort_key(config: RunConfig) -> tuple[int, int, int]:
    method_order = {"bm25": 0, "hybrid_rrf": 1, "hybrid_weighted": 2, "knn": 3}
    chunking_order = {"fixed": 0, "structural": 1}
    analyzer_order = {"standard": 0, "nori": 1}
    return (
        method_order[config.method],
        chunking_order[config.chunking],
        analyzer_order[config.analyzer],
    )
