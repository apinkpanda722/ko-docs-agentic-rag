"""Offline report contracts: missing data, preserved bytes and authenticated input."""

import json

import pytest

from kodocs.eval import m7_report as report
from kodocs.eval.m7_rawhash import record_fingerprint
from kodocs.eval.m7_stats import H5_ACCEPT


def put(root, name, value):
    path = root / "data/manifest" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def test_m7_report_missing(tmp_path):
    text, errors = report.render_tables(tmp_path)
    assert not errors
    assert "미실행" in text
    assert "PRD 성공 기준" in text
    assert "판정 대기" in text
    assert "사람 검증 없음" in text


def test_m7_report_data_presence_and_fallback(tmp_path):
    put(
        tmp_path,
        "m7_analysis.json",
        {
            "kappa": {"passed": False, "quadratic_kappa": 0.4},
            "h5": {"verdict_text": "H5 판정 보류", "input_source": "human"},
            "sol": {"overall": {"mean_diff": 0}},
            "router": {"launch": False},
            "faithfulness": {"rows": [{"n": 1, "mean": 3}]},
            "variability": {"n": 1},
        },
    )
    text, _ = report.render_tables(tmp_path)
    assert report.HUMAN_FALLBACK in text
    assert "quadratic_kappa | 0.4" in text
    assert "launch | false" in text


def test_m7_report_c1_pending(tmp_path):
    put(
        tmp_path,
        "m7_security_results.json",
        {
            "status": "completed",
            "groups": {
                "in-process|simple": {
                    "n": 2,
                    "leaks": 0,
                    "c1_candidates": 1,
                    "c1_candidate_ids": ["sec-1"],
                }
            },
        },
    )
    assert "판정 대기(c1 후보 확인 필요)" in report.render_tables(tmp_path)[0]


def test_m7_report_hash_failure_writes_error(tmp_path):
    raw = tmp_path / "data/interim/m7/grades.jsonl"
    raw.parent.mkdir(parents=True)
    raw.write_text("{}\n")
    manifest = put(tmp_path, "m7_grade_summary.json", {"calls": 1})
    record_fingerprint(manifest, "grades", raw, "completed")
    raw.write_text("{}\n{}\n")
    assert report.main(["--root", str(tmp_path)]) == 1
    text = (tmp_path / report.RESULT_PATH).read_text()
    assert "원본 해시 불일치" in text
    assert "calls | 1" not in text


def test_m7_report_preserves_bytes(tmp_path):
    path = tmp_path / report.RESULT_PATH
    path.parent.mkdir(parents=True)
    prefix = b"# custom\r\n\r\n" + report.BEGIN.encode()
    suffix = report.END.encode() + "\r\n메모 보존\r\n".encode()
    path.write_bytes(prefix + b"old" + suffix)
    report.write_report(tmp_path)
    result = path.read_bytes()
    assert result.startswith(prefix) and result.endswith(suffix)


@pytest.mark.parametrize(
    "phrase",
    [
        "analyze_dev()",
        "참조 답변은 하나",
        "별표의 단위·제목·표 밖 조항",
        "블라인드 한계",
        "충실성 지표는 사람 검증이 없다",
        "시스템·문항당 1회",
        "사람 채점자가 1인",
        "스트리밍 모드에서 측정",
        "유료 등급 Gemini",
        "판정기 구현 결함을 결과 확인 뒤 정정했다",
        "parse_error 4건",
        "pypdf 797자 → composite 293자",
        "make m6-serve",
        "미기록",
    ],
)
def test_m7_report_required_template(phrase):
    assert phrase in report.initial_document()


def test_m7_report_operations_and_evaluation_costs():
    rows = [
        {
            "system": "simple",
            "type": "sql",
            "cost_usd": 0.0140144,
            "tokens": {"input": 3, "output": 4, "cached_input": 8, "cache_write": 2},
            "steps": 1,
        }
    ]
    op = report.aggregate_operations(rows)
    assert op["simple|sql"]["cost_usd"]["total"] == 0.0140144
    assert op["simple|sql"]["cached_input"]["p95"] == 8
    costs = report.aggregate_costs(
        [
            {
                "run_id": "m6-test-01",
                "service": "openai.gpt-6-luna.input",
                "amount_usd": 1,
            },
            {"run_id": "m7-grade", "service": "google.gemini.input", "amount_usd": 9},
        ]
    )
    assert costs["운영 비용"]["m6-test-01"] == 1
    assert costs["평가 비용"]["m7-grade"] == 9


def test_m7_report_m6_cost_regression():
    rows = []
    for scope, system, amount in [
        ("test_gold", "simple", 0.0140144),
        ("test_gold", "agentic", 0.0329375),
        ("test_silver", "simple", 0.0414915),
        ("test_silver", "agentic", 0.11389),
    ]:
        rows.append({"scope": scope, "system": system, "cost_usd": amount})
    report.verify_m6_costs(rows)
    rows[0]["cost_usd"] += 0.01
    with pytest.raises(report.M7ReportError, match="M6 비용 불일치"):
        report.verify_m6_costs(rows)


def test_m7_report_recorded_null_is_not_missing():
    assert "미실행" not in report.details(
        {"temperature": None, "count": 0, "passed": False}
    )


def test_m7_report_gemini_real_run_id():
    result = report.aggregate_costs(
        [
            {
                "run_id": "gemini-grade",
                "service": "google.gemini-3.8-flash.input",
                "amount_usd": 1.79,
            }
        ]
    )
    assert result["평가 비용"]["gemini-grade"] == 1.79
    assert not result["운영 비용"]


def test_m7_report_all_aggregate_sections_filled(tmp_path):
    analysis = {
        "kappa": {"passed": True, "n": 50},
        "h5": {"verdict_text": H5_ACCEPT},
        "sol": {"n": 1},
        "router": {"launch": False},
        "faithfulness": {"n": 1},
        "variability": {"n": 1},
    }
    put(tmp_path, "m7_analysis.json", analysis)
    for name in (
        "human_sample",
        "human_grades",
        "probe",
        "pilot",
        "grade_summary",
        "failures",
        "config",
    ):
        put(tmp_path, f"m7_{name}.json", {"n": 1, "score_distribution": {"3": 50}})
    text, _ = report.render_tables(tmp_path)
    for title in (
        "채점 모델 검증(정답성)",
        "표본 구성",
        "사람 점수 분포",
        "probe",
        "파일럿",
        "H5: 13문항",
        "Sol 사후",
        "라우터 dev",
        "충실성 보조표",
        "3회 채점 편차",
        "실패 분석",
        "해시·고정 조건",
    ):
        section = next(s for s in text.split("## ") if s.startswith(title))
        assert "미실행" not in section


def test_m7_report_security_denominators_and_correction(tmp_path):
    groups = {"in-process|simple": {"n": 5, "leaks": 0, "c1_candidates": 0}}
    before = {
        "groups": {"in-process|simple": {"n": 5, "leaks": 1}},
        "control_false_refusal": {"numerator": 1, "denominator": 2},
    }
    after = {
        "groups": groups,
        "control_false_refusal": {"numerator": 0, "denominator": 2},
        "control_tool_call_errors": {"numerator": 1, "denominator": 2},
        "injection_auxiliary_non_verdict": {"simple": {"n": 3}},
    }
    put(
        tmp_path,
        "m7_security_results.json",
        after
        | {
            "history": [
                {"result": before},
                {"kind": "judge_correction", "result": after},
            ]
        },
    )
    put(
        tmp_path,
        "m7_security_set.json",
        {
            "included_counts": {"S6": 3, "control": 2},
            "non_control_total": 5,
            "supplement_count": 1,
        },
    )
    text, _ = report.render_tables(tmp_path)
    assert "included_counts.S6 | 3" in text
    assert "non_control_total | 5" in text
    assert "정정 전(첫 항목)" in text
    assert "정정 후(최신 judge_correction)" in text


def test_m7_report_latest_history_and_latency_failure(tmp_path):
    put(
        tmp_path,
        "m7_latency.json",
        {
            "systems": {"simple": {"p95_s": 99}},
            "history": [
                {"seq": 1, "status": "stopped", "raw": {"rows": 0}, "result": {}},
                {
                    "seq": 2,
                    "status": "completed",
                    "rerun_reason": "FP-056",
                    "previous_result_sha256": "first-hash",
                    "result": {
                        "systems": {"simple": {"p95_s": 3, "verdict": "P95 목표 달성"}}
                    },
                },
            ],
        },
    )
    text, _ = report.render_tables(tmp_path)
    assert "systems.simple.p95_s | 3" in text
    assert "FP-056" in text and "first-hash" in text and "API 호출 0건" in text


def test_m7_report_accuracy_split_and_failures():
    scores = {
        "test_gold|q1|simple|gpt-6-luna": {"correctness": [0, 0, 0]},
        "test_gold|q1|agentic|gpt-6-luna": {"correctness": [3, 3, 3]},
    }
    result = report.accuracy_table(
        scores, {"q1": {"type": "sql", "source": "synthetic"}}
    )
    assert "synthetic" in result and "| 3 |" in result and "simple" in result
    result = report.failure_table(
        {
            "by_system_set": {
                "simple|test_gold": {
                    "classes": {"d1": {"count": 2, "ids": ["q1", "q2"]}},
                    "d1_partial": {"count": 1},
                    "refusal_violations": {"count": 1},
                }
            }
        }
    )
    assert "d1" in result and "refusal_violations" in result


@pytest.mark.parametrize("key", ["security", "latency"])
def test_m7_report_other_raw_hash_failure(tmp_path, key):
    name, raw_name = report.RAW_INPUTS[key]
    raw = tmp_path / raw_name
    raw.parent.mkdir(parents=True)
    raw.write_text("{}\n")
    manifest = put(tmp_path, name, {"status": "completed"})
    record_fingerprint(manifest, key, raw, "completed")
    raw.write_text("{}\n{}\n")
    text, errors = report.render_tables(tmp_path)
    assert errors and "원본 해시 불일치" in text


def test_m7_report_stale_analysis_fingerprint(tmp_path):
    raw = tmp_path / report.RAW_INPUTS["grades"][1]
    raw.parent.mkdir(parents=True)
    raw.write_text("{}\n")
    manifest = put(tmp_path, "m7_grade_summary.json", {"status": "completed"})
    record_fingerprint(manifest, "grades", raw, "completed")
    put(
        tmp_path,
        "m7_analysis.json",
        {"raw": {"sha256": "old", "rows": 1}, "kappa": {"passed": True}},
    )
    text, errors = report.render_tables(tmp_path)
    assert errors and "원본 해시 불일치" in text
    assert "passed | true" not in text


def test_m7_report_operation_table_contains_latency():
    aggregate = report.aggregate_operations(
        [
            {
                "system": "simple",
                "type": "sql",
                "latency_s": 2,
                "steps": 1,
                "cost_usd": 0.01,
                "tokens": {
                    "input": 1,
                    "output": 2,
                    "cached_input": 3,
                    "cache_write": 4,
                },
            }
        ]
    )
    text = report.operation_table(aggregate)
    assert "latency_s" in text and "sql" in text and "P95" in text
    assert "미실행" not in text


def test_m7_report_complete_fixture_has_no_false_missing(tmp_path, monkeypatch):
    from kodocs.eval import m7_security
    from kodocs.eval.m7_rawhash import fingerprint

    monkeypatch.setattr(report, "_git_commit", lambda root: "fixture-commit")
    h6 = {
        "pending": False,
        "message": None,
        "by_path_system": {
            "in-process|simple": {
                "n": 5,
                "leaks": 0,
                "c1_candidates": 0,
                "c1_confirmed": 0,
            }
        },
    }
    monkeypatch.setattr(m7_security, "analyze_h6", lambda root: h6)
    rows = []
    for (scope, system), amount in report.M6_COSTS.items():
        rows.append(
            {
                "scope": scope,
                "system": system,
                "type": "sql",
                "cost_usd": amount,
                "steps": 1,
                "latency_s": 2,
                "tokens": {
                    "input": 1,
                    "output": 2,
                    "cached_input": 3,
                    "cache_write": 4,
                },
            }
        )
    monkeypatch.setattr(report, "operation_inputs", lambda root: rows.copy())
    raw_grade = tmp_path / report.RAW_INPUTS["grades"][1]
    raw_grade.parent.mkdir(parents=True)
    raw_grade.write_text("{}\n")
    raw_security = tmp_path / report.RAW_INPUTS["security"][1]
    raw_security.parent.mkdir(parents=True)
    raw_security.write_text(json.dumps(rows[0] | {"kind": "S1"}) + "\n")
    raw_latency = tmp_path / report.RAW_INPUTS["latency"][1]
    raw_latency.parent.mkdir(parents=True)
    raw_latency.write_text("{}\n")
    for key, (name, raw_name) in report.RAW_INPUTS.items():
        values = {"status": "completed"}
        if key == "security":
            values |= {
                "groups": {"in-process|simple": {"n": 5, "leaks": 0}},
                "c1_baseline": {"n": 5},
                "control_false_refusal": {"n": 5},
                "control_tool_call_errors": {"n": 5},
                "partial_answer_rates": {"n": 5},
                "injection_auxiliary_non_verdict": {"n": 5},
                "parity_mismatch_count": 0,
                "not_found_mismatch_count": 0,
                "data_quality": {"duplicate_titles": {"n": 1}},
            }
        if key == "latency":
            values |= {
                "systems": {"simple": {"verdict": "P95 목표 달성"}},
                "history": [{"seq": 1, "status": "completed", "result": {}}],
                "environment": {"python": "fixture"},
                "code_commit": "measured",
                "noncache_correction": {"p95_s": 4.1},
            }
        manifest = put(tmp_path, name, values)
        record_fingerprint(manifest, key, tmp_path / raw_name, "completed")
    put(
        tmp_path,
        "m7_analysis.json",
        {
            "raw": fingerprint(raw_grade),
            "h6": h6,
            "kappa": {"passed": True, "quadratic_kappa": 0.8},
            "h5": {"verdict_text": H5_ACCEPT},
            "sol": {"n": 1},
            "router": {"launch": False},
            "faithfulness": {"n": 1},
            "variability": {"n": 1},
            "scores": {"test_gold|q1|simple|gpt-6-luna": {"correctness": [3, 3, 3]}},
        },
    )
    put(tmp_path, "m7_grade_scores.json", {"raw": fingerprint(raw_grade)})
    for name in ("m5_config", "m6_config", "m7_config"):
        put(tmp_path, name + ".json", {"config_hash": "fixture"})
    for name in (
        "human_sample",
        "human_grades",
        "probe",
        "pilot",
        "failures",
        "leak_facts",
        "leak_review",
    ):
        put(tmp_path, "m7_" + name + ".json", {"n": 1, "score_distribution": {"3": 50}})
    put(
        tmp_path,
        "m7_security_set.json",
        {
            "counts": {"S1": 5},
            "included_counts": {"S1": 5},
            "non_control_total": 5,
            "supplement_count": 0,
        },
    )
    m5 = tmp_path / "docs/results/2026-09-30-m5-retrieval.md"
    m5.parent.mkdir(parents=True)
    m5.write_text("M5 결과 참조")
    questions = tmp_path / report.GOLD_PATH
    questions.parent.mkdir(parents=True)
    questions.write_text(
        json.dumps({"id": "q1", "type": "sql", "source": "synthetic"}) + "\n"
    )
    ledger = tmp_path / "data/manifest/costs.jsonl"
    ledger.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {
                    "run_id": "m6-test-01",
                    "service": "openai.gpt-6-luna.input",
                    "amount_usd": sum(report.M6_COSTS.values()),
                },
                {
                    "run_id": "m7-security",
                    "service": "openai.gpt-6-luna.input",
                    "amount_usd": rows[0]["cost_usd"],
                },
                {
                    "run_id": "gemini-grade",
                    "service": "google.gemini.input",
                    "amount_usd": 0.2,
                },
            ]
        )
    )
    dry = tmp_path / "data/manifest/m7_dry_runs.jsonl"
    dry.write_text("{}\n")
    text, errors = report.render_tables(tmp_path)
    assert not errors
    assert "미실행" not in text


def test_m7_report_registered_raw_missing_is_unexecuted(tmp_path):
    put(
        tmp_path,
        "m7_grade_summary.json",
        {"calls": 3, "raw": [{"key": "grades", "sha256": "registered", "rows": 3}]},
    )
    put(
        tmp_path,
        "m7_analysis.json",
        {"kappa": {"passed": True}, "h5": {"verdict_text": H5_ACCEPT}},
    )
    text, errors = report.render_tables(tmp_path)
    assert not errors
    section = next(s for s in text.split("## ") if s.startswith("채점 실행·"))
    assert "미실행" in section and "calls | 3" not in section
    assert H5_ACCEPT not in text
