"""Tests for the manifest-only M2 parsing report generator."""

import json
from pathlib import Path

import pytest

from kodocs.eval.parsing_report import (
    BEGIN,
    END,
    ParsingReportError,
    render_tables,
    write_report,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _write_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values),
        encoding="utf-8",
    )


def _metrics(*, documents: int, exact: float) -> dict[str, object]:
    return {
        "documents": documents,
        "law_articles": 3,
        "parsed_articles": 3,
        "matched_articles": 3,
        "structure_exact_rate": exact,
        "key_precision_mean": 1.0,
        "key_recall_mean": 1.0,
        "key_f1_mean": 1.0,
        "order_lcs_ratio_mean": 1.0,
        "text_sim_raw_mean_micro": 0.8,
        "text_sim_norm_mean_micro": 0.9,
        "text_exact_raw_rate_micro": 0.7,
        "text_exact_norm_rate_micro": 0.85,
        "text_sim_raw_mean_macro": 0.75,
        "text_sim_norm_mean_macro": 0.88,
        "text_exact_raw_rate_macro": 0.65,
        "text_exact_norm_rate_macro": 0.8,
        "editorial_law_only": 2,
        "editorial_parsed_only": 1,
        "editorial_both": 3,
        "notation_variants": 2,
    }


def _parser_payload(parser: str) -> dict[str, object]:
    return {
        "status": "scored",
        "aggregate": {
            "overall": {
                "status_counts": {"failed": 1, "no_law_text": 1, "ok": 1},
                "all_targets": _metrics(documents=2, exact=0.5),
                "ok_only": _metrics(documents=1, exact=1.0),
                "excluded": {"no_law_text": 1},
            }
        },
        "documents": [
            {
                "parser": parser,
                "target_key": "r1:s1:a",
                "status": "ok",
                "structure_exact": True,
                "addenda_law": 1,
                "addenda_parsed": 1,
                "key_precision": 1.0,
                "key_recall": 1.0,
                "text_sim_norm_mean": 1.0,
            },
            {
                "parser": parser,
                "target_key": "r2:s2:b",
                "status": "failed",
                "structure_exact": False,
                "addenda_law": 0,
                "addenda_parsed": 0,
                "key_precision": 0.0,
                "key_recall": 0.0,
                "text_sim_norm_mean": 0.0,
            },
            {
                "parser": parser,
                "target_key": "r3:s3:c",
                "status": "no_law_text",
                "structure_exact": False,
                "addenda_law": 0,
                "addenda_parsed": 0,
                "key_precision": 0.0,
                "key_recall": 0.0,
                "text_sim_norm_mean": 0.0,
            },
        ],
    }


def _write_manifests(root: Path) -> None:
    _write_json(
        root / "data/manifest/parse_targets.json",
        {"targets": [{"role": "current"}, {"role": "history"}]},
    )
    run_base = {
        "settings_hash": "settings-1",
        "page_count": 2,
        "elapsed_s": 4.0,
        "error_type": None,
        "init_s": None,
    }
    _write_jsonl(
        root / "data/manifest/parse_runs.jsonl",
        [
            {
                **run_base,
                "parser": "pypdf",
                "parser_version": "6.0",
                "input_sha256": "a",
                "status": "ok",
            },
            {
                **run_base,
                "parser": "docling",
                "parser_version": "2.0",
                "input_sha256": "a",
                "status": "failed",
                "error_type": "RuntimeError",
                "init_s": 2.0,
            },
            {
                **run_base,
                "parser": "upstage",
                "parser_version": "document-parse-260630",
                "input_sha256": "a",
                "status": "empty",
                "api_version": "v1",
            },
        ],
    )
    _write_json(
        root / "data/manifest/parse_scores.json",
        {
            "generated_at": "2026-09-27T10:00:00+09:00",
            "parsers": {
                "pypdf": _parser_payload("pypdf"),
                "docling": _parser_payload("docling"),
                "upstage": _parser_payload("upstage"),
                "splitter-ceiling": _parser_payload("splitter-ceiling"),
            },
            "answer_differences": {
                "status": "scored",
                "parsers": ["pypdf", "docling", "upstage"],
                "total_keys": 2,
                "targets": [
                    {
                        "target_key": "r1:s1:a",
                        "law_rule_id": "r1",
                        "law_serial": "s1",
                        "role": "current",
                        "ok_parsers": ["pypdf", "docling"],
                        "law_keys": [[2, 0], [6, 4]],
                    }
                ],
                "key_recall": {
                    parser: {
                        "all_targets": {
                            "documents": 2,
                            "key_recall_mean": 0.9,
                            "key_recall_mean_adjusted": 0.95,
                        },
                        "ok_only": {
                            "documents": 1,
                            "key_recall_mean": 0.8,
                            "key_recall_mean_adjusted": 1.0,
                        },
                    }
                    for parser in ("pypdf", "docling", "upstage")
                },
            },
            "excluded_targets": [
                {
                    "law_rule_id": "r3",
                    "law_serial": "s3",
                    "role": "current",
                    "reason": "법제처 본문이 이미지 한 줄",
                }
            ],
            "law_versions_without_article_text": {
                "versions_total": 5,
                "versions_without_article_text": 1,
                "versions": [
                    {
                        "law_rule_id": "r3",
                        "law_serial": "s3",
                        "is_current": True,
                        "reason": "법제처 본문이 이미지 한 줄",
                    }
                ],
            },
        },
    )
    _write_json(
        root / "data/manifest/table_golden.json",
        {"tables": [{"id": "table-1", "golden_file_sha256": "abc"}]},
    )
    _write_json(
        root / "data/manifest/table_scores.json",
        {
            "parsers": {
                "pypdf": {"applicable": False, "measured": 0},
                "docling": {
                    "applicable": True,
                    "measured": 1,
                    "cell_exact_rate_mean": 0.75,
                    "cell_sim_mean": 0.9,
                },
                "upstage": {
                    "applicable": True,
                    "measured": 1,
                    "cell_exact_rate_mean": 1.0,
                    "cell_sim_mean": 1.0,
                },
            },
            "scores": [
                {
                    "parser": "docling",
                    "cell_exact_rate": 0.75,
                    "shape_match": False,
                },
                {
                    "parser": "upstage",
                    "cell_exact_rate": 1.0,
                    "shape_match": True,
                },
            ],
        },
    )
    _write_jsonl(
        root / "data/manifest/costs.jsonl",
        [
            {
                "service": "upstage.document-parse.standard",
                "amount_usd": 0.022,
            }
        ],
    )
    fonts = root / "infra/fontconfig/fonts.conf"
    fonts.parent.mkdir(parents=True, exist_ok=True)
    fonts.write_text("<fontconfig/>", encoding="utf-8")


def test_render_tables_uses_placeholder_when_no_manifests_exist(tmp_path: Path) -> None:
    assert render_tables(tmp_path) == "(실행 후 기록)"


def test_render_tables_covers_every_manifest_section_without_source_text(
    tmp_path: Path,
) -> None:
    _write_manifests(tmp_path)

    rendered = render_tables(tmp_path)

    for heading in (
        "측정 정보",
        "분할기 상한",
        "제외 대상",
        "조 구조 — 전체 대상 기준",
        "조 구조 — ok만 기준",
        "조문 텍스트",
        "편집 차이",
        "표 셀 일치",
        "처리 비용",
        "실패 목록",
        "법제처 본문에 조문 텍스트가 없는 버전",
        "원본 표기 변형·정답 차이",
        "정답 차이 대상",
    ):
        assert heading in rendered
    assert "| pypdf | 2 | 0.9000 | 0.9500 | 0.8000 | 1.0000 |" in rendered
    assert "| r1 | s1 | current | pypdf, docling | 제2조, 제6조의4 |" in rendered
    assert "document-parse-260630" in rendered
    assert "해당 없음(표 추출 기능 없음)" in rendered
    assert "법제처 본문이 이미지 한 줄" in rendered
    assert "RuntimeError=1" in rendered
    assert "0.0220" in rendered
    assert "| pypdf | 1/2 | 50.0% | 1.0000 | 1.0000 | 1.0000 | 50.0% |" in rendered
    assert "조문 원문" not in rendered


def test_write_report_creates_from_template_then_preserves_human_sections(
    tmp_path: Path,
) -> None:
    _write_manifests(tmp_path)
    out_path = tmp_path / "report.md"

    created = write_report(tmp_path, run_date="2026-09-27", out_path=out_path)
    first = created.read_text(encoding="utf-8")
    assert created == out_path
    assert first.count(BEGIN) == first.count(END) == 1
    assert "# M2 파싱 비교 결과" in first

    edited = first.replace(
        "(실행 후 기록 — 규정 번호·조 번호·차이 유형으로)", "사람 분석"
    )
    out_path.write_text(edited, encoding="utf-8")
    costs = tmp_path / "data/manifest/costs.jsonl"
    costs.write_text(
        costs.read_text(encoding="utf-8").replace("0.022", "0.033"),
        encoding="utf-8",
    )

    write_report(tmp_path, run_date="2026-09-27", out_path=out_path)
    updated = out_path.read_text(encoding="utf-8")

    assert "사람 분석" in updated
    assert "0.0330" in updated
    assert updated[: updated.index(BEGIN)] == edited[: edited.index(BEGIN)]
    assert (
        updated[updated.index(END) + len(END) :]
        == edited[edited.index(END) + len(END) :]
    )


@pytest.mark.parametrize(
    "contents",
    ["표지 없음", f"{BEGIN}\n짝 없음", f"{END}\n{BEGIN}"],
)
def test_write_report_refuses_malformed_markers_without_overwrite(
    tmp_path: Path, contents: str
) -> None:
    out_path = tmp_path / "report.md"
    out_path.write_text(contents, encoding="utf-8")

    with pytest.raises(ParsingReportError, match="generated"):
        write_report(tmp_path, run_date="2026-09-27", out_path=out_path)

    assert out_path.read_text(encoding="utf-8") == contents


@pytest.mark.parametrize("run_date", ["2026-9-27", "../escape", "not-a-date"])
def test_write_report_rejects_non_iso_date(tmp_path: Path, run_date: str) -> None:
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        write_report(tmp_path, run_date=run_date)


def test_answer_difference_table_explains_when_parsers_are_insufficient(
    tmp_path: Path,
) -> None:
    _write_manifests(tmp_path)
    score_path = tmp_path / "data/manifest/parse_scores.json"
    scores = json.loads(score_path.read_text(encoding="utf-8"))
    scores["answer_differences"] = {
        "status": "insufficient_parsers",
        "parsers": ["pypdf"],
    }
    _write_json(score_path, scores)

    rendered = render_tables(tmp_path)

    assert "채점된 비교 파서가 2개 미만(pypdf)" in rendered
    assert "| pypdf | 2 | 1.0000 | - | 1.0000 | - |" in rendered


def test_cost_table_excludes_model_loading_from_seconds_per_page(
    tmp_path: Path,
) -> None:
    _write_manifests(tmp_path)
    base = {"parser": "docling", "parser_version": "2.0", "error_type": None}
    _write_jsonl(
        tmp_path / "data/manifest/parse_runs.jsonl",
        [
            {
                **base,
                "run_id": "r1",
                "input_sha256": "a",
                "status": "ok",
                "elapsed_s": 6.0,
                "page_count": 2,
                "init_s": 30.0,
            },
            {
                **base,
                "run_id": "r1",
                "input_sha256": "b",
                "status": "failed",
                "elapsed_s": 100.0,
                "page_count": 5,
                "init_s": 30.0,
                "error_type": "AttributeError",
            },
            {
                **base,
                "run_id": "r2",
                "input_sha256": "a",
                "status": "skipped",
                "elapsed_s": 0.0,
                "page_count": 2,
                "init_s": None,
            },
            {
                **base,
                "run_id": "r2",
                "input_sha256": "b",
                "status": "ok",
                "elapsed_s": 3.0,
                "page_count": 4,
                "init_s": 10.0,
            },
        ],
    )

    rendered = render_tables(tmp_path)

    # 최신 ok 기록 a(6초·2쪽)·b(3초·4쪽): 9초 ÷ 6쪽, 로딩 30·10초는 따로 표시.
    assert (
        "| docling | 로컬(원장 기록 없음) | 6 | 9.000 | 1.500 | "
        "30.000(최초), 10.000 | 0.0000 | 0.0000 |"
    ) in rendered
    assert "모델 로딩(초)" in rendered


def test_table_score_table_adds_rows_excluding_nested_tables(tmp_path: Path) -> None:
    _write_manifests(tmp_path)
    score_path = tmp_path / "data/manifest/table_scores.json"
    scores = json.loads(score_path.read_text(encoding="utf-8"))
    scores["parsers_excluding_nested"] = {
        "docling": {
            "applicable": True,
            "measured": 1,
            "cell_exact_rate_mean": 0.75,
            "cell_sim_mean": 0.9,
        },
        "upstage": {"applicable": True, "measured": 0},
    }
    scores["scores"][1]["nested"] = True
    _write_json(score_path, scores)

    rendered = render_tables(tmp_path)

    assert "| upstage | 채점됨 | 1 | 1.0000 | 1.0000 | 1 |" in rendered
    assert (
        "| upstage (중첩 제외) | 채점됨 | 0 | (실행 후 기록) | (실행 후 기록) | 0 |"
        in rendered
    )
    assert "| docling (중첩 제외) | 채점됨 | 1 | 0.7500 | 0.9000 | 0 |" in rendered
