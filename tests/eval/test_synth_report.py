"""Manifest-only M3 synthetic-corpus report generation."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from kodocs.eval import synth_report
from kodocs.eval.synth_report import BEGIN, END, render_tables, write_report


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values),
        encoding="utf-8",
    )


def _write_inputs(root: Path) -> None:
    plans = [
        {
            "doc_id": "doc-a",
            "doc_type": "guide",
            "team": "platform",
            "access": {"level": "public"},
        },
        {
            "doc_id": "doc-b",
            "doc_type": "meeting",
            "team": "payments",
            "access": {"level": "team"},
        },
    ]
    _write_jsonl(root / "data/synthetic/plan.jsonl", plans)
    _write_json(root / "data/synthetic/canaries.json", {"count": 30, "canaries": []})
    _write_json(
        root / "data/synthetic/injections.json", {"count": 12, "injections": []}
    )
    _write_json(
        root / "data/manifest/synth_validation.json",
        {
            "planned": 2,
            "generated": 2,
            "passed": 2,
            "failed": 0,
            "not_generated": 0,
            "exhausted": 0,
            "by_code": {},
            "canary": {"expected": 30, "present": 2, "leaks": 0},
            "injections": {"expected": 12, "inserted": 1},
        },
    )
    _write_json(
        root / "data/manifest/synth_db.json",
        {
            "content_hash": "db-content-hash",
            "row_counts": {"tickets": 500, "approvals": 150},
            "access_counts": {
                "tickets": {"public": 300, "team": 175, "restricted": 25},
                "approvals": {"public": 30, "team": 90, "restricted": 30},
            },
            "seed_files_sha256": {"data/seed/company.yaml": "seed-hash"},
        },
    )
    _write_json(
        root / "data/manifest/synth_metrics.json",
        {
            "schema_version": 1,
            "scope": {"passed_documents": 2},
            "duplicate": {
                "shingle_n": 5,
                "threshold": 0.5,
                "pair_count": 1,
                "overall": {"max": 0.25, "p95": 0.25, "mean": 0.25},
                "within_type": {"max": None, "p95": None, "mean": None},
                "between_type": {"max": 0.25, "p95": 0.25, "mean": 0.25},
                "near_duplicates": [],
            },
            "repeated_sentences": {
                "sentences_in_3_plus_docs": 1,
                "top": [{"sentence_prefix": "반복 문장 앞 40자", "document_count": 3}],
            },
            "length": {
                "by_type": {
                    "guide": {
                        "documents": 1,
                        "mean_chars": 100.0,
                        "stddev_chars": 0.0,
                        "coefficient_of_variation": 0.0,
                        "min_chars": 100,
                        "max_chars": 100,
                        "target_mean_chars": 100.0,
                        "target_ratio": 1.0,
                    }
                }
            },
            "lexical_diversity": {
                "by_type": {
                    "guide": {
                        "distinct_2": 0.75,
                        "unique_bigrams": 75,
                        "total_bigrams": 100,
                    }
                }
            },
            "style": {
                "overall": {
                    "sentences": 2,
                    "counts": {"formal": 1, "plain": 1, "nominal": 0, "other": 0},
                    "ratios": {
                        "formal": 0.5,
                        "plain": 0.5,
                        "nominal": 0.0,
                        "other": 0.0,
                    },
                },
                "by_type": {},
            },
            "distribution": {
                "by_type": {"guide": {"planned": 1, "passed": 1}},
                "by_access": {"public": {"planned": 1, "passed": 1}},
                "by_team": {"platform": {"planned": 1, "passed": 1}},
                "canary_documents": 2,
                "injection_documents": 1,
            },
            "fact_coverage": {
                "normal_facts_total": 4,
                "covered_facts": 3,
                "coverage_rate": 0.75,
                "documents_per_fact_distribution": {"0": 1, "1": 2, "2": 1},
                "facts_in_2_plus_documents": 1,
            },
            "validation": {"pass_rate": 1.0, "failure_codes": {}},
            "provenance": {
                "models": ["claude-sonnet-5-20260901"],
                "template_hashes": ["template-guide"],
                "seed_files_sha256": {"data/seed/company.yaml": "seed-hash"},
                "regulation_index_sha256": ["reg-hash"],
                "validator_versions": [1],
            },
        },
    )
    generation = {
        "model": "claude-sonnet-5",
        "max_tokens": 6000,
        "thinking": "disabled",
        "effort": None,
        "temperature": None,
        "sampling": "unsupported_by_model",
    }
    _write_jsonl(
        root / "data/manifest/anthropic_batches.jsonl",
        [
            {
                "submission_id": "sub-1",
                "batch_id": "batch-1",
                "state": "collected",
                "n_requests": 2,
                "custom_ids": ["doc-a-a1", "doc-b-a1"],
                "request_set_hash": "requests-hash",
                "settings_hash": "settings-hash",
                "generation": generation,
                "result_counts": {
                    "succeeded": 2,
                    "errored": 0,
                    "canceled": 0,
                    "expired": 0,
                    "missing": 0,
                },
                "usage_totals": {
                    "input_tokens": 100,
                    "output_tokens": 200,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
                "estimated_input_tokens": 80,
                "ts": "2026-09-27T00:00:00+00:00",
            }
        ],
    )
    _write_jsonl(
        root / "data/manifest/costs.jsonl",
        [
            {"service": "anthropic.claude-sonnet-5.batch.input", "amount_usd": 0.11},
            {"service": "anthropic.claude-sonnet-5.batch.output", "amount_usd": 0.22},
            {"service": "upstage.document-parse.standard", "amount_usd": 99.0},
        ],
    )


def test_render_tables_uses_placeholders_when_inputs_do_not_exist(
    tmp_path: Path,
) -> None:
    rendered = render_tables(tmp_path)

    assert rendered.count("(실행 후 기록)") >= 8
    for heading in (
        "실행 정보",
        "고정 조건",
        "생성·비용",
        "검증",
        "품질 지표",
        "티켓·결재 DB",
    ):
        assert heading in rendered


def test_render_tables_keeps_generation_results_placeholder_before_generation(
    tmp_path: Path,
) -> None:
    _write_json(
        tmp_path / "data/manifest/synth_validation.json",
        {
            "planned": 200,
            "generated": 0,
            "passed": 0,
            "failed": 0,
            "not_generated": 200,
            "exhausted": 0,
            "by_code": {},
            "canary": {"expected": 30, "present": 0, "leaks": 0},
            "injections": {"expected": 12, "inserted": 0},
        },
    )

    rendered = render_tables(tmp_path)

    assert "| 계획 | 200 |" in rendered
    assert "| 통과 | (실행 후 기록) |" in rendered
    assert "| 미생성 | (실행 후 기록) |" in rendered
    assert "| 카나리아 | 30 | (실행 후 기록) | (실행 후 기록) |" in rendered


def test_render_tables_includes_metrics_provenance_and_anthropic_cost_only(
    tmp_path: Path,
) -> None:
    _write_inputs(tmp_path)

    rendered = render_tables(tmp_path)

    assert "claude-sonnet-5-20260901" in rendered
    assert "temperature" in rendered
    assert "null (모델 미지원: unsupported_by_model)" in rendered
    assert "thinking=disabled" in rendered
    assert "template-guide" in rendered
    assert "seed-hash" in rendered
    assert "reg-hash" in rendered
    assert "db-content-hash" in rendered
    assert "0.3300" in rendered
    assert "99.0000" not in rendered
    assert "125.0%" in rendered
    assert "반복 문장 앞 40자" in rendered
    assert "계획 2건" in rendered
    assert "최대 시도 3회" in rendered
    assert "Batch 사용" in rendered


def test_write_report_preserves_outside_generated_region_and_omits_doc_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_inputs(tmp_path)
    unique_body = "결과 문서에 들어가면 안 되는 합성 본문 전문 SECRET-BODY"
    _write_json(
        tmp_path / "data/synthetic/docs/doc-a.json",
        {"doc_id": "doc-a", "body": unique_body},
    )
    monkeypatch.setattr(synth_report, "_git_commit", lambda _root: "abc1234")
    out = tmp_path / "result.md"

    created = write_report(tmp_path, run_date=date(2026, 9, 27), out_path=out)
    first = created.read_text(encoding="utf-8")
    assert first.count(BEGIN) == first.count(END) == 1
    assert "2026-09-27" in first
    assert "abc1234" in first
    assert unique_body not in first
    assert "(검수 후 기록)" in first
    assert "① 제출 전 Anthropic 콘솔 Usage/Cost를 기록한다" in first
    assert "billing_margin`(현재 1.05)" in first
    assert "합성 문서는 실제보다 쉽다" in first
    assert "문서 형식 다양성 없음" in first
    assert "동일 요청 재현 불가" in first

    edited = first.replace("(사용자 기록)", "사용자가 기록한 콘솔 값", 1)
    out.write_text(edited, encoding="utf-8")
    write_report(tmp_path, run_date=date(2026, 9, 27), out_path=out)
    updated = out.read_text(encoding="utf-8")

    assert "사용자가 기록한 콘솔 값" in updated
    assert updated[: updated.index(BEGIN)] == edited[: edited.index(BEGIN)]
    assert (
        updated[updated.index(END) + len(END) :]
        == edited[edited.index(END) + len(END) :]
    )


@pytest.mark.parametrize(
    "contents", ["표지 없음", f"{BEGIN}\n짝 없음", f"{END}\n{BEGIN}"]
)
def test_write_report_refuses_malformed_markers(tmp_path: Path, contents: str) -> None:
    out = tmp_path / "result.md"
    out.write_text(contents, encoding="utf-8")

    with pytest.raises(synth_report.SynthReportError, match="generated"):
        write_report(tmp_path, run_date=date(2026, 9, 27), out_path=out)

    assert out.read_text(encoding="utf-8") == contents
