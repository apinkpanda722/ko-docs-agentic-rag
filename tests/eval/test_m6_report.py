"""Offline M6 report projection, preservation, and privacy contracts."""

import json
from datetime import date

import pytest

from kodocs.eval import m6_report as report


def put(root, name, value):
    path = root / "data/manifest" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")


def render(root):
    return report.write_report(root, run_date=date(2030, 1, 2)).read_text()


def test_m6_report_empty_and_fixed_path(tmp_path):
    text = render(tmp_path)
    assert (tmp_path / "docs/results/2026-10-01-m6-agent-mcp.md").is_file()
    for heading in (
        "입력 요약",
        "is_current 효과",
        "dev 모델 비교",
        "고정 설정",
        "test 실행",
        "예상 비용 대 실제 비용",
        "MCP 계약",
    ):
        section = text.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]
        assert "미실행" in section
    for field in (
        "2030-01-02",
        "커밋 해시",
        "모델 ID",
        "설정 해시",
        "측정 환경",
        "OpenSearch 힙",
        "임베딩 CPU/GPU",
        "도구 버전",
        "FP-044",
    ):
        assert field in text
    assert "채점 대기(M7)" in text
    assert "answer_values" not in text


def test_m6_report_partial_inputs_and_preservation(tmp_path):
    put(
        tmp_path,
        "m6_eval_inputs.json",
        {
            "dev_set": {"count": 75, "by_set": {"gold": 20, "silver": 55}},
            "h5_pool": {"count": 13},
            "answer_match_denominator": {
                "count": 56,
                "by_source": {"real": 3, "synthetic": 53},
            },
            "gate_permission_dev": {"count": 8},
            "router_types": {
                "eligible": {"identifier": {"count": 13}},
                "below_threshold": {"sql": {"count": 2}},
            },
        },
    )
    text = render(tmp_path)
    assert "| dev | 75 | 20 | 55 |" in text
    assert "| real | 3 |" in text
    assert "| identifier | 13 | 8 이상 |" in text
    assert "| sql | 2 | 8 미만 |" in text
    path = tmp_path / report.REPORT_PATH
    edited = text.replace("## 목적과 범위", "사용자 메모: 보존 확인\n\n## 목적과 범위")
    path.write_text(edited)
    put(
        tmp_path,
        "m6_config.json",
        {
            "answer": {"model": "gpt-6-luna", "settings_hash": "a" * 64},
            "config_hash": "b" * 64,
            "frozen_at": "2026-10-01T00:00:00Z",
        },
    )
    second = render(tmp_path)
    assert "사용자 메모: 보존 확인" in second
    assert "b" * 64 in second
    assert text.split(report.END)[1] in second
    assert render(tmp_path) == second


def rate(n=2, d=3):
    return {"numerator": n, "denominator": d, "rate": n / d}


def operation():
    return {
        "n": 75,
        "cost_usd": {"total": 1.25, "mean": 0.02},
        "latency_s": {"p50": 2.5},
        "steps": {"mean": 3, "max": 5},
        "statuses": {"billing_uncertain": 2},
        "guardrail_terminations": {
            "guardrail_max_steps": 1,
            "guardrail_tokens": 2,
            "guardrail_timeout": 3,
        },
        "tokens": {
            "input": 100,
            "cached_input": 10,
            "cache_write": 0,
            "output": 20,
            "reasoning": 5,
        },
    }


def test_m6_report_measurements_and_no_raw_content(tmp_path):
    secret = "PRIVATE-QUESTION-ANSWER-ARTICLE-TOKEN"
    metric = {**rate(40, 56), "by_source": {"real": rate(), "synthetic": rate(38, 53)}}
    put(
        tmp_path,
        "m6_dev.json",
        {
            "answer_match": {"gpt-6-luna": {"simple": metric, "agentic": metric}},
            "gate": {
                "gpt-6-luna": {
                    "a_count": 0,
                    "b_count": 0,
                    "passed": True,
                    "execution_count": 16,
                }
            },
            "unanswerable_refusal": {"gpt-6-luna": {"simple": rate(7, 8)}},
            "operations": {"gpt-6-luna": {"simple": operation()}},
            "paired_bootstrap": {
                "left_model": "gpt-6-luna",
                "right_model": "gpt-6.1-sol",
                "mean_diff": -0.1,
                "ci_low": -0.2,
                "ci_high": 0.01,
            },
            "selection": "gpt-6-luna",
            "selection_reason": "cheaper_on_tie",
            "concurrency": 4,
            "answer_values": [secret],
            "question": secret,
            "answer": secret,
        },
    )
    put(
        tmp_path,
        "m6_temporal.json",
        {
            "temporal": {
                option: {
                    "rows": [
                        {
                            "qid": "g-real-003",
                            "split": "test",
                            "current_rank": current,
                            "prior_rank": prior,
                            "prior_above_current": prior < current,
                        }
                    ]
                }
                for option, current, prior in (
                    ("prefer_current_off", 2, 1),
                    ("prefer_current_on", 1, 2),
                )
            },
            "top10_changes": {
                "unexpected_change_count": 0,
                "unexpected_changes": [],
                "by_group": {"gold_test": {"checked": 30}},
            },
        },
    )
    put(
        tmp_path,
        "m6_test_results/run-01.json",
        {
            "seq": 1,
            "concurrency": 4,
            "deterministic_metrics": {
                "simple": {
                    "answer_match": rate(),
                    "citation_precision": {
                        "mean": 0.75,
                        "denominator": 20,
                        "excluded": 10,
                    },
                    "unanswerable_refusal": rate(2, 2),
                    "permission_refusal": rate(3, 3),
                }
            },
            "operations": {"gpt-6-luna": {"simple": operation()}},
            "h5": {"correctness": 3},
            "answer": secret,
        },
    )
    put(
        tmp_path,
        "m6_test_runs.jsonl",
        {
            "seq": 1,
            "ran_at": "2026-10-01T00:00:00Z",
            "status": "completed",
            "result_sha256": "c" * 64,
        },
    )
    put(
        tmp_path,
        "m6_dry_runs.jsonl",
        {
            "ran_at": "2026-10-01T00:00:00Z",
            "totals": {"estimated_cost_usd": 4, "upper_cost_usd": 10},
            "concurrency": 4,
        },
    )
    put(
        tmp_path,
        "costs.jsonl",
        {
            "service": "openai.gpt-6-luna.input",
            "run_id": "m6-dev",
            "amount_usd": 1.25,
            "quantity": 100,
            "ref": secret,
            "generation": {"api_key": secret},
        },
    )
    index = tmp_path / "tests/contract/snapshots/index.json"
    index.parent.mkdir(parents=True)
    index.write_text(
        json.dumps({"files": {"search_docs.v1.json": "d" * 64, "token": secret}})
    )
    text = render(tmp_path)
    for value in (
        "40/56",
        "38/53",
        "7/8",
        "-0.2",
        "cheaper_on_tie",
        "1.25",
        "2.5",
        "c" * 64,
        "d" * 64,
        "unexpected_changes",
    ):
        assert value in text
    assert "| g-real-003 | test | 2 | 1 | 1 | 2 |" in text
    for value in (secret, "answer_values", "api_key", "correctness", "P95 |"):
        assert value not in text
    h5 = text.split("### H5\n")[1].split("\n## ")[0]
    assert "| 13문항 판정 | 13 | 채점 대기(M7) |" in h5
    assert "| 9문항 보조(sql 제외, 판정 아님) | 9 | 미실행 |" in h5
    assert "answer_values" not in h5


@pytest.mark.parametrize(
    "markers", ["", report.END + report.BEGIN, report.BEGIN + report.BEGIN + report.END]
)
def test_m6_report_invalid_markers_leave_document_untouched(tmp_path, markers):
    path = tmp_path / report.REPORT_PATH
    path.parent.mkdir(parents=True)
    path.write_text(markers)
    with pytest.raises(report.M6ReportError):
        render(tmp_path)
    assert path.read_text() == markers


def test_m6_report_cli_and_malformed_input(tmp_path, capsys):
    assert report.main(["--root", str(tmp_path), "--date", "2026-10-01"]) == 0
    put(tmp_path, "m6_dev.json", [])
    assert report.main(["--root", str(tmp_path)]) == 1
    assert "실패" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("name", "value", "heading", "expected"),
    [
        ("m6_config.json", {"config_hash": "e" * 64}, "고정 설정", "e" * 64),
        (
            "m6_dry_runs.jsonl",
            {"totals": {"estimated_cost_usd": 12.34}},
            "예상 비용 대 실제 비용",
            "12.34",
        ),
        (
            "m6_dev.json",
            {"selection": "gpt-6.1-sol", "selection_reason": "ci_excludes_zero"},
            "dev 모델 비교",
            "ci_excludes_zero",
        ),
        (
            "m6_temporal.json",
            {
                "top10_changes": {
                    "unexpected_change_count": 1,
                    "unexpected_changes": ["s-syn-001"],
                }
            },
            "is_current 효과",
            "s-syn-001",
        ),
        ("m6_test_results/run-02.json", {"seq": 2}, "test 실행", "test 시도 2"),
    ],
)
def test_m6_report_each_optional_artifact_is_independent(
    tmp_path, name, value, heading, expected
):
    put(tmp_path, name, value)
    text = render(tmp_path)
    section = text.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]
    assert expected in section
    if heading != "dev 모델 비교":
        assert "## dev 모델 비교\n\n미실행" in text
    assert render(tmp_path) == text


def test_m6_report_rerun_history_redacts_secrets_and_preserves_attempts(tmp_path):
    rows = [
        {
            "seq": 1,
            "ran_at": "2026-10-01T00:00:00Z",
            "status": "completed",
            "result_sha256": "a" * 64,
        },
        {
            "seq": 2,
            "ran_at": "2026-10-02T00:00:00Z",
            "status": "failed",
            "previous_result_sha256": "a" * 64,
            "rerun_reason": "인프라 재시도 Bearer mcp-hidden-value sk-private-key token=hidden-token "
            + "Z" * 43,
            "error": "PRIVATE-EXCEPTION",
        },
    ]
    path = tmp_path / "data/manifest/m6_test_runs.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(json.dumps(row) for row in rows))
    text = render(tmp_path)
    assert "| 1 | 2026-10-01T00:00:00Z | completed |" in text
    assert "| 2 | 2026-10-02T00:00:00Z | failed | 인프라 재시도" in text
    assert "[REDACTED]" in text
    for value in (
        "mcp-hidden-value",
        "sk-private-key",
        "hidden-token",
        "Z" * 43,
        "PRIVATE-EXCEPTION",
    ):
        assert value not in text


def test_m6_report_cost_ledger_only_counts_m6_openai_entries(tmp_path):
    rows = [
        {"run_id": "m6-dev", "service": "openai.gpt-6-luna.input", "amount_usd": 0.1},
        {"run_id": "m6-dev", "service": "openai.gpt-6-luna.input", "amount_usd": 0.2},
        {
            "run_id": "m6-test-01",
            "service": "openai.gpt-6-luna.output",
            "amount_usd": 0.4,
        },
        {"run_id": "m5-test", "service": "openai.gpt-6-luna.output", "amount_usd": 999},
        {"run_id": "m6-dev", "service": "anthropic.input", "amount_usd": 998},
    ]
    path = tmp_path / "data/manifest/costs.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(json.dumps(row) for row in rows))
    text = render(tmp_path)
    assert "| m6-dev | openai.gpt-6-luna.input | 0.3 |" in text
    assert "| m6-test-01 | openai.gpt-6-luna.output | 0.4 |" in text
    assert "999" not in text
    assert "998" not in text


def test_m6_report_lists_invalidated_dev_runs(tmp_path):
    entry = {
        "attempt": 1,
        "kind": "dev",
        "status": "invalid",
        "invalidated_on": "2026-10-01",
        "reason": "인용 검증 버그",
        "runs_rows": 300,
        "runs_sha256": "a" * 64,
        "result_sha256": "b" * 64,
        "selection_used": False,
    }
    path = tmp_path / "data/manifest/m6_dev_history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")

    text = render(tmp_path)

    section = text.split("## dev 실행 이력\n", 1)[1].split("\n## ", 1)[0]
    assert "| 1 | dev | invalid | 2026-10-01 | 300 |" in section
    assert "인용 검증 버그" in section
    assert "a" * 64 in section and "b" * 64 in section
    assert "선택에 쓰지 않음" in section


def test_m6_report_dev_history_is_unrun_without_the_file(tmp_path):
    text = render(tmp_path)

    section = text.split("## dev 실행 이력\n", 1)[1].split("\n## ", 1)[0]
    assert "미실행" in section
