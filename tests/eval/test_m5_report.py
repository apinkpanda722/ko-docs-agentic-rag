"""Tests for the fixed-path, generated-region M5 retrieval report."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from kodocs.eval.m5_report import BEGIN, END, TEMPLATE_PATH, render_tables, write_report


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _index_fixture(root: Path) -> None:
    structural = {
        "status": "ok",
        "document_counts": {
            "synthetic": 200,
            "real_current": 95,
            "real_prior": 5,
            "total": 300,
        },
        "chunk_count": 4811,
        "failure_count": 0,
        "embedder": {
            "model_id": "BAAI/bge-m3",
            "revision": "revision-abc",
            "settings_hash": "settings-123",
            "device": "mps",
            "init_s": 1.25,
        },
        "max_seq_length": 8192,
        "over_limit_chunk_count": 3,
        "gold_answer_chunks_over_limit": 1,
        "silver_answer_chunks_over_limit": 2,
        "chunk_length_chars_by_source": {
            "real": {"count": 4057, "median": 170},
            "synthetic": {"count": 754, "median": 420},
        },
        "chunk_length_chars_by_doc_type": {
            "regulation": {"count": 4057, "median": 162},
            "meeting": {"count": 58, "median": 930},
            "incident": {"count": 95, "median": 510},
            "weekly": {"count": 159, "median": 410},
            "runbook": {"count": 172, "median": 380},
            "guide": {"count": 178, "median": 360},
            "notice": {"count": 92, "median": 330},
        },
    }
    indexes = {
        "kodocs-m5-composite-fixed": {
            **structural,
            "chunk_count": 9967,
            "over_limit_chunk_count": 0,
            "gold_answer_chunks_over_limit": 0,
            "silver_answer_chunks_over_limit": 0,
        },
        "kodocs-m5-composite-structural": structural,
        "kodocs-m5-pypdf-structural": {**structural, "chunk_count": 4700},
        "kodocs-m5-upstage-structural": {**structural, "chunk_count": 4500},
    }
    _write_json(
        root / "data/manifest/m5_index.json",
        {
            "chunking": {
                "fixed_size": 200,
                "fixed_overlap": 40,
                "structural_chunk_count": 4811,
                "fixed_chunk_count": 9967,
                "structural_body_length_chars": {
                    "median": 193,
                    "p90": 626,
                    "max": 26409,
                },
            },
            "indexes": indexes,
        },
    )
    _write_json(
        root / "data/manifest/m5_annex_mapping.json",
        {"counts": {"fallback_annex_count": 62, "unmapped_table_count": 193}},
    )


def _analysis_fixture(value: float) -> dict[str, object]:
    statistic = {
        "n": 21,
        "mean_diff": value,
        "ci_low": value - 0.01,
        "ci_high": value + 0.01,
        "n_boot": 10000,
        "seed": 20260930,
    }
    description = {
        "n": 8,
        "mean": value,
        "sd": 0.1,
        "median": value,
        "min": 0.0,
        "max": 1.0,
    }
    stages = [
        {
            "id": "standard_bm25_fixed",
            "mean_recall10": 0.5,
            "mean_ndcg10": 0.4,
            "delta_from_previous": None,
        },
        {
            "id": "nori_bm25_fixed",
            "mean_recall10": 0.6,
            "mean_ndcg10": 0.5,
            "delta_from_previous": {
                "recall10": statistic,
                "ndcg10": statistic,
            },
        },
        {
            "id": "nori_hybrid_fixed",
            "mean_recall10": 0.7,
            "mean_ndcg10": 0.6,
            "delta_from_previous": {
                "recall10": statistic,
                "ndcg10": statistic,
            },
        },
        {
            "id": "nori_hybrid_structural",
            "mean_recall10": 0.8,
            "mean_ndcg10": 0.7,
            "delta_from_previous": {
                "recall10": statistic,
                "ndcg10": statistic,
            },
        },
    ]
    hypothesis = {
        "comparison": "left - right",
        "metric": "recall10",
        "n": 21,
        "statistic": statistic,
        "verdict": "채택",
    }
    h3_sources = {
        source: {
            "n": n,
            "metric_n": n,
            "structural": description,
            "fixed": description,
            "difference": description,
            "excluded_no_span": {"n": 0, "qids": []},
        }
        for source, n in (("real", 13), ("synthetic", 8))
    }
    parser_subset = {
        "n": 19,
        "conditions": {
            name: {"recall10": description, "ndcg10": description}
            for name in ("pypdf", "upstage", "composite")
        },
        "paired_comparisons": {
            "composite_minus_pypdf": {
                "recall10": statistic,
                "ndcg10": statistic,
            }
        },
    }
    return {
        "ladder": {"n": 21, "stages": stages},
        "ablation": {
            "n": 21,
            "final": {
                "configuration": "nori_hybrid_structural",
                "mean_recall10": 0.8,
                "mean_ndcg10": 0.7,
            },
            "removals": [
                {
                    "element": "hybrid",
                    "configuration": "nori_bm25_structural",
                    "mean_recall10": 0.6,
                    "mean_ndcg10": 0.5,
                    "delta_final_minus_removed": {
                        "recall10": statistic,
                        "ndcg10": statistic,
                    },
                }
            ],
        },
        "full_grid": {
            "n": 21,
            "rows": [
                {
                    "condition": "composite",
                    "chunking": "fixed",
                    "method": "bm25",
                    "analyzer": "standard",
                    "candidate_chunks": 50,
                    "bm25_weight": None,
                    "selected_hybrid": False,
                    "mean_recall10": 0.5,
                    "mean_ndcg10": 0.4,
                    "span_hit_rate": 0.3,
                    "span_n": 21,
                    "top1_hit_rate": 0.2,
                }
            ],
        },
        "hypotheses": {
            "H1": hypothesis,
            "H2": {**hypothesis, "metric": "ndcg10"},
            "H3": {**hypothesis, "metric": "span_hit"},
            "H4": {
                **hypothesis,
                "n": 4,
                "metric": "top1_hit",
                "note": "표본 4문항이라 CI가 넓어 사실상 판정 불가",
            },
            "H7": {
                **hypothesis,
                "n_synthetic": 8,
                "n_real": 6,
                "synthetic": description,
                "real": {**description, "n": 6},
                "all_types_descriptive": {
                    "real": {**description, "n": 13},
                    "synthetic": description,
                },
            },
        },
        "h3_breakdown": {
            "label": "기술 통계",
            "gold_test": h3_sources,
            "silver_test": {
                "real": {**h3_sources["real"], "n": 0, "metric_n": 0},
                "synthetic": {**h3_sources["synthetic"], "n": 65, "metric_n": 65},
            },
        },
        "h2_breakdown": {
            "label": "기술 통계",
            "gold_test": {
                "n": 21,
                "by_source_and_answer_repeated": {
                    source: {
                        "repeated": description,
                        "not_repeated": description,
                    }
                    for source in ("real", "synthetic")
                },
            },
            "silver_test": {
                "n": 65,
                "by_source_and_answer_repeated": {
                    "synthetic": {
                        "repeated": description,
                        "not_repeated": description,
                    }
                },
            },
        },
        "lexical_mismatch_subset": {
            "label": "기술 통계",
            "gold_test": {
                "n": 3,
                "bm25": description,
                "hybrid": description,
                "difference": description,
            },
        },
        "corpus_v2_rule": {
            "group_sizes": {"not_repeated": 6, "repeated": 59},
            "statistic": statistic,
            "decision": "판정 불가(표본 부족) — 코퍼스 v2 미착수, 한계로 기록",
        },
        "temporal_prior_vs_current": {
            "rows": [
                {
                    "qid": "g-real-001",
                    "split": "test",
                    "current_doc_id": "law-current",
                    "prior_doc_id": "law-prior",
                    "current_rank": 1,
                    "prior_rank": 2,
                    "prior_above_current": False,
                }
            ],
            "by_split": {"test": {"n": 3, "prior_above_current_count": 0}},
        },
        "parser_comparison": {
            "label": "탐색적 비교(사전 등록 가설 아님)",
            "subsets": {
                "real_all_19": parser_subset,
                "real_test_13": {**parser_subset, "n": 13},
            },
        },
        "silver_direction": {
            "label": "방향 확인용",
            "all": {"n": 65, "ladder": {"n": 65, "stages": stages}},
        },
    }


def _result_fixture(seq: int, value: float) -> dict[str, object]:
    return {
        "seq": seq,
        "run_id": f"m5-test-{seq:02d}",
        "ran_at": f"2026-09-30T1{seq}:00:00+09:00",
        "config_hash": "config-abc",
        "rerun_reason": None if seq == 1 else "재기동 후 재측정",
        "previous_result_sha256": None if seq == 1 else "previous-sha",
        "fixed_configuration": {
            "hybrid_method": "hybrid_rrf",
            "candidate_chunks": 50,
            "bm25_weight": None,
            "rank_constant": 60,
            "combination_count": 14,
        },
        "question_counts": {"gold_test": 21, "silver_test": 65},
        "counting_units": {"retrieval": "unique question_id"},
        "environment": {
            "platform": "test-platform",
            "code_commit": "abcdef0",
            "embedder": {
                "model_id": "BAAI/bge-m3",
                "revision": "revision-abc",
                "settings_hash": "settings-123",
                "device": "mps",
                "init_s": 1.25,
            },
            "opensearch": {"version": "3.8.0", "heap": "-Xms2g -Xmx2g"},
            "bootstrap": {"seed": 20260930, "resamples": 10000},
        },
        "analysis": _analysis_fixture(value),
    }


def test_chunk_length_and_embedding_truncation_tables_use_index_manifest(
    tmp_path: Path,
) -> None:
    _index_fixture(tmp_path)

    rendered = render_tables(tmp_path)

    assert "구조 청크 길이(문자)" in rendered
    assert "| 출처 | real | 4057 | 170 |" in rendered
    assert "| 문서 유형 | meeting | 58 | 930 |" in rendered
    assert "고정 크기 200자·겹침 40자" in rendered
    assert "전체 구조 청크 중앙값 193자·p90 626자·최대 26409자" in rendered
    assert "임베딩 잘림" in rendered
    assert "| kodocs-m5-composite-structural | 8192 | 3 | 1 | 2 |" in rendered


def test_missing_run_results_create_a_placeholder_draft_with_static_sections(
    tmp_path: Path,
) -> None:
    _index_fixture(tmp_path)

    destination = write_report(tmp_path, run_date=date(2026, 9, 30))
    report = destination.read_text(encoding="utf-8")

    assert "(실행 후 기록)" in report
    assert "중앙값이 규정 조 청크(84%)에 의해 정해져" in report
    assert "n-gram 분석기 제외" in report
    assert "## 실행 순서(사용자 실행)" in report
    assert "## 첫 실행과 재실행" in report


def test_template_does_not_claim_synthetic_questions_are_easier() -> None:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    assert "합성 문항은 실제 문항보다 쉽다" not in template


def test_result_fixture_renders_ladder_hypotheses_and_exploratory_tables(
    tmp_path: Path,
) -> None:
    _index_fixture(tmp_path)
    result = _result_fixture(1, 0.1)
    _write_json(tmp_path / "data/manifest/m5_test_results/run-01.json", result)

    rendered = render_tables(tmp_path)

    assert "주 표 — 사다리" in rendered
    assert "standard_bm25_fixed" in rendered
    assert "| H1 | 채택 |" in rendered
    assert "| H5 | M6·M7 대상 |" in rendered
    assert "| H6 | M6·M7 대상 |" in rendered
    assert "탐색적 비교(사전 등록 가설 아님)" in rendered
    h3_table = rendered.split("### H3 출처별 기술 통계", 1)[1].split("## 보조 분석", 1)[
        0
    ]
    assert "채택" not in h3_table
    assert "기각" not in h3_table


def test_generated_region_update_preserves_human_limits_and_change_log(
    tmp_path: Path,
) -> None:
    _index_fixture(tmp_path)
    destination = write_report(tmp_path, run_date=date(2026, 9, 30))
    original = destination.read_text(encoding="utf-8")
    human_text = "사람이 추가한 한계 해석"
    changed = original.replace("## 변경 기록", f"{human_text}\n\n## 변경 기록")
    destination.write_text(changed, encoding="utf-8")

    write_report(tmp_path, run_date=date(2026, 10, 1))
    updated = destination.read_text(encoding="utf-8")

    assert updated.count(BEGIN) == 1
    assert updated.count(END) == 1
    assert human_text in updated
    assert "## 변경 기록" in updated


def test_first_and_rerun_records_and_core_results_are_all_reported(
    tmp_path: Path,
) -> None:
    _index_fixture(tmp_path)
    for seq, value in ((1, 0.1), (2, 0.2)):
        _write_json(
            tmp_path / f"data/manifest/m5_test_results/run-{seq:02d}.json",
            _result_fixture(seq, value),
        )
    runs = tmp_path / "data/manifest/m5_test_runs.jsonl"
    runs.write_text(
        "\n".join(
            json.dumps(
                {
                    "seq": seq,
                    "ran_at": f"2026-09-30T1{seq}:00:00+09:00",
                    "status": "completed",
                    "rerun_reason": None if seq == 1 else "재기동 후 재측정",
                    "previous_result_sha256": None if seq == 1 else "previous-sha",
                    "result_sha256": f"result-sha-{seq}",
                    "result_path": f"data/manifest/m5_test_results/run-{seq:02d}.json",
                },
                ensure_ascii=False,
            )
            for seq in (1, 2)
        )
        + "\n",
        encoding="utf-8",
    )

    rendered = render_tables(tmp_path)

    assert "| 1 | 1 | 2026-09-30T11:00:00+09:00 | completed |" in rendered
    assert "| 2 | 2 | 2026-09-30T12:00:00+09:00 | completed |" in rendered
    assert "재기동 후 재측정" in rendered
    assert "| 1 | H1 | 채택 | 0.1 |" in rendered
    assert "| 2 | H1 | 채택 | 0.2 |" in rendered


def test_numbers_are_rounded_to_three_decimals_including_intervals(
    tmp_path: Path,
) -> None:
    _index_fixture(tmp_path)
    _write_json(
        tmp_path / "data/manifest/m5_test_results/run-01.json",
        _result_fixture(1, 0.123456),
    )

    rendered = render_tables(tmp_path)

    assert "0.123 [0.113, 0.133]" in rendered
    assert "0.123456" not in rendered


def test_not_applicable_cells_say_so_and_known_values_are_filled(
    tmp_path: Path,
) -> None:
    _index_fixture(tmp_path)
    result = _result_fixture(1, 0.1)
    analysis = result["analysis"]
    empty = {"n": 0, "mean": None, "sd": None, "median": None, "min": None, "max": None}
    analysis["h3_breakdown"]["silver_test"]["real"] = {
        "n": 0,
        "metric_n": 0,
        "structural": empty,
        "fixed": empty,
        "difference": empty,
        "excluded_no_span": {"n": 0, "qids": []},
    }
    _write_json(tmp_path / "data/manifest/m5_test_results/run-01.json", result)
    _write_json(
        tmp_path / "data/manifest/m5_dev.json",
        {
            "grid": [
                {
                    "method": "hybrid_weighted",
                    "candidate_chunks": 50,
                    "bm25_weight": 0.7,
                    "gold_dev_mean_ndcg10": 0.6,
                    "silver_dev_mean_ndcg10": 0.4,
                    "selection_score": 0.523545,
                },
                {
                    "method": "hybrid_rrf",
                    "candidate_chunks": 50,
                    "bm25_weight": None,
                    "gold_dev_mean_ndcg10": 0.6,
                    "silver_dev_mean_ndcg10": 0.3,
                    "selection_score": 0.45,
                },
            ],
            "selected": {
                "method": "hybrid_weighted",
                "candidate_chunks": 50,
                "bm25_weight": 0.7,
            },
        },
    )

    rendered = render_tables(tmp_path)

    # 사다리 첫 행은 이전 단계가 없어 해당 없음
    assert "| standard_bm25_fixed | 0.5 | 0.4 | 해당 없음 | 해당 없음 |" in rendered
    # BM25 가중치가 적용되지 않는 방식
    assert "| 50 | 해당 없음 | False |" in rendered
    assert "| hybrid_rrf | 50 | 해당 없음 |" in rendered
    # 빈 그룹의 통계
    assert (
        "| silver_test | real | 0 | 0 | 해당 없음 | 해당 없음 | 해당 없음 |" in rendered
    )
    # 값이 있는데 비어 있던 칸: H7 n, dev 선택 점수
    assert "n=합성=8, 실제=6" in rendered
    assert "| 선택 점수 | 0.524 |" in rendered


def test_run_history_separates_attempts_and_annotates_failed_attempt(
    tmp_path: Path,
) -> None:
    _index_fixture(tmp_path)
    _write_json(
        tmp_path / "data/manifest/m5_test_results/run-01.json",
        _result_fixture(1, 0.1),
    )
    failed_at = "2026-09-30T18:35:04+09:00"
    completed_at = "2026-09-30T18:38:02+09:00"
    records = [
        {
            "seq": 1,
            "ran_at": failed_at,
            "status": "failed",
            "error": "QuestionRunError: index_not_found_exception",
            "rerun_reason": None,
            "previous_result_sha256": None,
            "result_sha256": None,
        },
        {
            "seq": 1,
            "ran_at": completed_at,
            "status": "completed",
            "rerun_reason": None,
            "previous_result_sha256": None,
            "result_sha256": "result-sha-1",
        },
    ]
    (tmp_path / "data/manifest/m5_test_runs.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
    )
    note = "통합 테스트가 실측 인덱스를 삭제해 결과 없이 실패, 재색인 후 재실행"
    _write_json(tmp_path / "data/manifest/m5_test_run_notes.json", {failed_at: note})

    rendered = render_tables(tmp_path)

    assert "| 시도 | seq | 시각 | 상태 | 사유 |" in rendered
    assert (
        f"| 1 | 1 | {failed_at} | failed | {note} | 해당 없음 | 해당 없음 |" in rendered
    )
    assert (
        f"| 2 | 1 | {completed_at} | completed | 해당 없음 | 해당 없음 | result-sha-1 |"
        in rendered
    )
    assert "실패한 시도는 결과 파일을 만들지 않고 잠금을 소모하지 않는다" in rendered
