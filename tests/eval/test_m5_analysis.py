"""Tests for pure M5 retrieval analysis and preregistered decisions."""

from __future__ import annotations

import json

import pytest

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
from kodocs.eval.m5_metrics import Span
from kodocs.eval.m5_runner import (
    HybridSetting,
    QuestionRun,
    RunConfig,
    all_combo_configs,
)


def _config(
    chunking: str,
    method: str,
    analyzer: str,
    *,
    condition: str = "composite",
) -> RunConfig:
    return RunConfig(
        condition=condition,
        chunking=chunking,
        method=method,
        analyzer=analyzer,
        candidate_chunks=50,
        bm25_weight=0.4 if method == "hybrid_weighted" else None,
    )


STANDARD_FIXED = _config("fixed", "bm25", "standard")
NORI_FIXED = _config("fixed", "bm25", "nori")
HYBRID_FIXED = _config("fixed", "hybrid_rrf", "nori")
HYBRID_STRUCTURAL = _config("structural", "hybrid_rrf", "nori")
BM25_STRUCTURAL = _config("structural", "bm25", "nori")
STANDARD_HYBRID_STRUCTURAL = _config("structural", "hybrid_rrf", "standard")


def _run(
    qid: str,
    value: float,
    *,
    span_hit: bool | None = True,
    top1: bool | None = None,
    ranked_docs: tuple[str, ...] = ("current", "prior"),
) -> QuestionRun:
    return QuestionRun(
        qid=qid,
        ranked_docs=ranked_docs,
        top10_chunks=(Span(ranked_docs[0], 0, 10),) if ranked_docs else (),
        recall10=value,
        ndcg10=value,
        span_hit=span_hit,
        top1=value > 0 if top1 is None else top1,
    )


def _key(
    config: RunConfig,
    *,
    question_set: str = "gold",
    split: str = "test",
) -> RunKey:
    return RunKey(
        question_set=question_set,
        split=split,
        config=config,
        selected_hybrid=config.method == "hybrid_rrf",
    )


def _runs(
    qids: list[str],
    values: dict[RunConfig, float] | None = None,
    *,
    question_set: str = "gold",
    spans: dict[str, bool | None] | None = None,
) -> dict[RunKey, list[QuestionRun]]:
    values = values or {
        STANDARD_FIXED: 0.1,
        NORI_FIXED: 0.3,
        HYBRID_FIXED: 0.6,
        HYBRID_STRUCTURAL: 0.9,
        BM25_STRUCTURAL: 0.4,
        STANDARD_HYBRID_STRUCTURAL: 0.5,
    }
    return {
        _key(config, question_set=question_set): [
            _run(qid, value, span_hit=(spans or {}).get(qid, True)) for qid in qids
        ]
        for config, value in values.items()
    }


def _meta(
    qid: str,
    *,
    question_set: str = "gold",
    source: str = "synthetic",
    question_type: str = "semantic",
    tags: tuple[str, ...] = (),
    repeated: bool = False,
) -> QuestionMeta:
    return QuestionMeta(
        qid=qid,
        question_set=question_set,
        source=source,
        question_type=question_type,
        tags=tags,
        repeated=repeated,
        split="test",
    )


def test_ladder_and_ablation_use_preregistered_adjacent_pairs() -> None:
    qids = ["q1", "q2"]
    runs = _runs(qids)

    result = ladder(runs, qids)

    assert [row["id"] for row in result["stages"]] == [
        "standard_bm25_fixed",
        "nori_bm25_fixed",
        "nori_hybrid_fixed",
        "nori_hybrid_structural",
    ]
    assert result["stages"][1]["delta_from_previous"]["recall10"][
        "mean_diff"
    ] == pytest.approx(0.2)
    assert result["stages"][3]["delta_from_previous"]["ndcg10"][
        "mean_diff"
    ] == pytest.approx(0.3)

    removed = ablation(runs, qids)
    assert [row["element"] for row in removed["removals"]] == [
        "structural",
        "hybrid",
        "nori",
    ]
    assert removed["removals"][0]["delta_final_minus_removed"]["recall10"][
        "mean_diff"
    ] == pytest.approx(0.3)
    assert removed["removals"][1]["configuration"] == "nori_bm25_structural"


def test_full_grid_reports_all_14_combinations_and_json_serializes() -> None:
    configs = all_combo_configs(HybridSetting(50, 0.4))
    qids = ["q1", "q2"]
    runs = {
        _key(config): [_run("q1", 0.0, span_hit=None), _run("q2", 1.0)]
        for config in configs
    }

    result = full_grid(runs, qids)

    assert len(result["rows"]) == 14
    assert all(row["n"] == 2 for row in result["rows"])
    assert all(row["mean_recall10"] == 0.5 for row in result["rows"])
    assert all(row["span_n"] == 1 for row in result["rows"])
    json.dumps(result, ensure_ascii=False)


def _hypothesis_fixture() -> tuple[
    dict[RunKey, list[QuestionRun]], dict[str, QuestionMeta], dict[str, bool]
]:
    gold_qids = [f"g{i}" for i in range(14)]
    silver_qids = ["s1", "s2"]
    spans = {"g13": None}
    runs = _runs(gold_qids, spans=spans)
    runs.update(_runs(silver_qids, question_set="silver"))
    meta = {
        qid: _meta(
            qid,
            source="synthetic" if index < 8 else "real",
            question_type="identifier" if index < 4 else "semantic",
            tags=("lexical_mismatch",) if index == 0 else (),
            repeated=index % 2 == 0,
        )
        for index, qid in enumerate(gold_qids)
    }
    meta.update(
        {
            qid: _meta(
                qid,
                question_set="silver",
                question_type="identifier",
                repeated=False,
            )
            for qid in silver_qids
        }
    )
    lookup = {qid: True for qid in [*gold_qids[:4], *silver_qids]}
    return runs, meta, lookup


def test_hypotheses_apply_fixed_conditions_and_keep_missing_spans_visible() -> None:
    runs, meta, lookup = _hypothesis_fixture()

    result = hypotheses(runs, meta, lookup)

    assert result["H1"]["comparison"] == "nori BM25 고정 - standard BM25 고정"
    assert result["H2"]["comparison"] == "nori hybrid 고정 - nori BM25 고정"
    assert result["H3"]["comparison"] == "nori hybrid 구조 - nori hybrid 고정"
    assert result["H3"]["excluded_no_span"] == {"n": 1, "qids": ["g13"]}
    assert result["H3"]["n"] == 13
    assert result["H4"]["note"].startswith("표본 4문항")
    assert result["H4"]["silver_test_direction"]["n"] == 2
    assert result["H7"]["n_synthetic"] == 8
    assert result["H7"]["n_real"] == 6
    assert result["H7"]["comparison"] == "합성 - 실제"
    assert result["H1"]["verdict"] == "채택"
    assert result["H2"]["verdict"] == "채택"
    assert result["H3"]["verdict"] == "판정 보류(차이 입증 못 함)"


def test_hypotheses_reject_h7_counts_other_than_eight_and_six() -> None:
    runs, meta, lookup = _hypothesis_fixture()
    meta["g7"] = _meta("g7", source="real")

    with pytest.raises(ValueError, match="H7.*8.*6"):
        hypotheses(runs, meta, lookup)


def test_h2_and_lexical_breakdowns_are_descriptive_only() -> None:
    runs, meta, _lookup = _hypothesis_fixture()

    result = h2_breakdown(runs, meta)
    lexical = lexical_mismatch_subset(runs, meta)

    assert result["gold_test"]["n"] == 14
    assert (
        result["gold_test"]["by_source_and_answer_repeated"]["synthetic"]["repeated"][
            "n"
        ]
        == 4
    )
    assert (
        result["silver_test"]["by_source_and_answer_repeated"]["synthetic"][
            "not_repeated"
        ]["n"]
        == 2
    )
    assert lexical["gold_test"]["n"] == 1
    assert "verdict" not in json.dumps(result, ensure_ascii=False)
    assert "verdict" not in json.dumps(lexical, ensure_ascii=False)


def test_h3_breakdown_splits_sources_and_excludes_missing_spans() -> None:
    runs, meta, _lookup = _hypothesis_fixture()

    result = h3_breakdown(runs, meta)

    gold = result["gold_test"]
    assert gold["synthetic"]["n"] == 8
    assert gold["real"]["n"] == 6
    assert gold["synthetic"]["metric_n"] == 8
    assert gold["real"]["metric_n"] == 5
    assert gold["real"]["excluded_no_span"] == {"n": 1, "qids": ["g13"]}
    assert result["silver_test"]["synthetic"]["n"] == 2
    encoded = json.dumps(result, ensure_ascii=False)
    assert "verdict" not in encoded
    assert "채택" not in encoded
    assert "기각" not in encoded


def _corpus_fixture(
    not_repeated_values: list[float], repeated_values: list[float]
) -> tuple[dict[RunKey, list[QuestionRun]], dict[str, QuestionMeta]]:
    qids = [
        *(f"n{i}" for i in range(len(not_repeated_values))),
        *(f"r{i}" for i in range(len(repeated_values))),
    ]
    bm25 = [
        *(_run(f"n{i}", 0.0) for i in range(len(not_repeated_values))),
        *(_run(f"r{i}", 0.0) for i in range(len(repeated_values))),
    ]
    hybrid = [
        *(_run(f"n{i}", value) for i, value in enumerate(not_repeated_values)),
        *(_run(f"r{i}", value) for i, value in enumerate(repeated_values)),
    ]
    runs = {
        _key(NORI_FIXED, question_set="silver"): bm25,
        _key(HYBRID_FIXED, question_set="silver"): hybrid,
    }
    meta = {
        qid: _meta(
            qid,
            question_set="silver",
            repeated=qid.startswith("r"),
        )
        for qid in qids
    }
    return runs, meta


def test_corpus_v2_rule_has_sample_shortage_branch() -> None:
    runs, meta = _corpus_fixture([0.4] * 9, [0.0] * 10)

    result = corpus_v2_rule(runs, meta)

    assert result["group_sizes"] == {"not_repeated": 9, "repeated": 10}
    assert result["decision"] == (
        "판정 불가(표본 부족) — 코퍼스 v2 미착수, 한계로 기록"
    )
    assert result["ci_low"] == pytest.approx(0.4)


def test_corpus_v2_rule_proposes_only_when_lower_bound_reaches_point_one() -> None:
    proposed_runs, proposed_meta = _corpus_fixture([0.2] * 10, [0.0] * 10)
    held_runs, held_meta = _corpus_fixture([0.05] * 10, [0.0] * 10)

    proposed = corpus_v2_rule(proposed_runs, proposed_meta)
    held = corpus_v2_rule(held_runs, held_meta)

    assert proposed["group_sizes"] == {"not_repeated": 10, "repeated": 10}
    assert proposed["ci_low"] == pytest.approx(0.2)
    assert proposed["decision"] == "코퍼스 v2 별도 phase 제안"
    assert held["ci_low"] == pytest.approx(0.05)
    assert held["decision"] == "코퍼스 v2 미착수, 한계로만 기록"


def test_temporal_prior_vs_current_handles_absent_ranks() -> None:
    runs = {
        _key(HYBRID_STRUCTURAL): [
            _run("q-test", 1.0, ranked_docs=("law-prior",)),
            _run("q-dev", 1.0, ranked_docs=("other",)),
        ]
    }
    prior_map = {"law-current": "law-prior"}
    questions = {
        "q-test": {
            "tags": ["temporal:revision"],
            "split": "test",
            "qrels": [{"doc_id": "law-current", "rel": 2}],
        },
        "q-dev": {
            "tags": ["temporal:revision"],
            "split": "dev",
            "qrels": [{"doc_id": "law-current", "rel": 2}],
        },
    }

    result = temporal_prior_vs_current(runs, prior_map, questions)

    rows = {row["qid"]: row for row in result["rows"]}
    assert rows["q-test"]["current_rank"] is None
    assert rows["q-test"]["prior_rank"] == 1
    assert rows["q-test"]["prior_above_current"] is True
    assert rows["q-dev"]["current_rank"] is None
    assert rows["q-dev"]["prior_rank"] is None
    assert rows["q-dev"]["prior_above_current"] is False
    assert result["by_split"]["test"]["prior_above_current_count"] == 1


def test_parser_compare_is_labeled_exploratory_and_checks_fixed_counts() -> None:
    qids19 = [f"q{i}" for i in range(19)]
    qids13 = qids19[:13]
    runs = {
        "pypdf": [_run(qid, 0.2) for qid in qids19],
        "upstage": [_run(qid, 0.4) for qid in qids19],
        "composite": [_run(qid, 0.8) for qid in qids19],
    }

    result = parser_compare(runs, qids19, qids13)

    assert result["label"] == "탐색적 비교(사전 등록 가설 아님)"
    assert result["subsets"]["real_all_19"]["conditions"]["pypdf"]["recall10"][
        "mean"
    ] == pytest.approx(0.2)
    assert result["subsets"]["real_test_13"]["paired_comparisons"][
        "composite_minus_pypdf"
    ]["ndcg10"]["mean_diff"] == pytest.approx(0.6)
    assert "verdict" not in json.dumps(result, ensure_ascii=False)

    with pytest.raises(ValueError, match="19"):
        parser_compare(runs, qids19[:-1], qids13)
    with pytest.raises(ValueError, match="13"):
        parser_compare(runs, qids19, qids13[:-1])


def test_silver_direction_reports_all_and_temporal_tag_groups() -> None:
    all_qids = ["s1", "s2"]
    runs = _runs(all_qids, question_set="silver", spans={"s2": None})

    result = silver_direction(
        runs,
        {
            "all": all_qids,
            "temporal:revision": [],
            "temporal:as_of_event": ["s1"],
        },
    )

    assert result["label"] == "방향 확인용"
    assert result["all"]["n"] == 2
    assert result["all"]["H3"]["excluded_no_span"] == {
        "n": 1,
        "qids": ["s2"],
    }
    assert result["temporal_tags"]["temporal:revision"]["n"] == 0
    assert result["temporal_tags"]["temporal:as_of_event"]["n"] == 1
    assert "verdict" not in json.dumps(result, ensure_ascii=False)
