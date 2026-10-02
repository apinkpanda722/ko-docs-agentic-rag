"""Offline report contracts: missing data, preserved bytes and authenticated input."""

import json
from pathlib import Path

import pytest

from kodocs.eval import m7_report as report
from kodocs.eval.m7_rawhash import record_fingerprint
from kodocs.eval.m7_stats import H5_ACCEPT


def put(root, name, value):
    path = root / "data/manifest" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def render(root):
    """Body and appendix together, for tests that inspect every generated section."""
    body, appendix, errors = report.render_tables(root)
    return body + "\n\n" + appendix, errors


def test_m7_report_missing(tmp_path):
    text, errors = render(tmp_path)
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
    text, _ = render(tmp_path)
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
    assert "판정 대기(c1 후보 확인 필요)" in render(tmp_path)[0]


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
    text, _ = render(tmp_path)
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
    text, _ = render(tmp_path)
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
    text, _ = render(tmp_path)
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
    text, errors = render(tmp_path)
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
    text, errors = render(tmp_path)
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
                "clopper_pearson_upper": 0.4,
                "control_false_refusal": {"numerator": 0, "denominator": 5},
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
                "systems": {
                    "simple": {
                        "verdict": "P95 목표 달성",
                        "p95_s": 3.7,
                        "p95_ci95_s": [3.1, 6.6],
                        "target_s": 5.0,
                        "total": {"p50_s": 1.9},
                        "execution_count": 113,
                        "failure_count": 2,
                        "segments": {
                            "ttft_s": {"p50_s": 0.7, "p95_s": 1.3},
                            "search_s": {"p50_s": 0.02, "p95_s": 0.03},
                            "embed_s": {"max_s": 0.378},
                        },
                    },
                    "agentic": {
                        "p95_s": 13.4,
                        "p95_ci95_s": [10.8, 15.5],
                        "total": {"p50_s": 6.7},
                        "execution_count": 113,
                        "failure_count": 5,
                        "segments": {
                            "ttft_s": {"p50_s": 0.7, "p95_s": 1.3},
                            "search_s": {"p50_s": 0.02, "p95_s": 0.03},
                            "embed_s": {"max_s": 0.378},
                        },
                    },
                },
                "history": [{"seq": 1, "status": "completed", "result": {}}],
                "environment": {"python": "fixture"},
                "code_commit": "measured",
            }
        manifest = put(tmp_path, name, values)
        record_fingerprint(manifest, key, tmp_path / raw_name, "completed")
    put(
        tmp_path,
        "m7_analysis.json",
        {
            "raw": fingerprint(raw_grade),
            "h6": h6,
            "kappa": {
                "passed": True,
                "quadratic_kappa": 0.8,
                "ci_low": 0.7,
                "ci_high": 0.9,
            },
            "h5": {
                "verdict_text": H5_ACCEPT,
                "primary": {"n": 13, "mean_diff": 1.5, "ci_low": 0.9, "ci_high": 2.0},
            },
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
    put(tmp_path, "m6_dev.json", {"operations": {}})
    stat = {"mean_diff": 0.1, "ci_low": 0.0, "ci_high": 0.2, "n": 21}
    m5_run = tmp_path / "data/manifest/m5_test_results/run-01.json"
    m5_run.parent.mkdir(parents=True)
    m5_run.write_text(
        json.dumps(
            {
                "analysis": {
                    "hypotheses": {
                        **{
                            h: {
                                "verdict": "채택",
                                "statistic": stat,
                                "n": 21,
                                "comparison": "a - b",
                                "metric": "recall10",
                            }
                            for h in ("H1", "H2", "H3", "H4")
                        },
                        "H7": {
                            "verdict": "기각(반대 방향)",
                            "statistic": stat,
                            "n_synthetic": 8,
                            "n_real": 6,
                            "comparison": "합성 - 실제",
                            "metric": "recall10",
                        },
                    }
                }
            }
        )
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
    text, errors = render(tmp_path)
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
    text, errors = render(tmp_path)
    assert not errors
    section = next(s for s in text.split("## ") if s.startswith("채점 실행·"))
    assert "미실행" in section and "calls | 3" not in section
    assert H5_ACCEPT not in text


def test_m7_report_formatting_and_placeholder_fill():
    assert report.f3(1.6153846) == "1.615"
    assert report.f3(-0.0004) == "0.000"
    assert report.f3(0.0, signed=True) == "0.000"
    assert report.stat3(
        {"mean_diff": 1.6153846, "ci_low": 0.923, "ci_high": 2.3077}
    ) == ("+1.615 [0.923, 2.308]")
    primary = {"n": 13, "mean_diff": 1.6153846, "ci_low": 0.923, "ci_high": 2.3077}
    filled = report.fill_verdict(H5_ACCEPT, primary)
    assert "차이 +1.615, 95% CI [0.923, 2.308], n=13" in filled
    assert "(차이, 95% CI" not in filled


def test_m7_report_noncache_bound_from_committed_latency_manifest():
    """Regression: the bound must come from the real manifest shape, not an invented key."""
    latency = json.loads(Path("data/manifest/m7_latency.json").read_text())
    completed = [h for h in latency["history"] if h["status"] == "completed"]
    latency = latency | completed[-1]["result"]
    bound = report.noncache_bound(latency)
    assert bound is not None
    assert bound["bound_s"] == pytest.approx(
        bound["simple_p95_s"] + bound["agentic_embed_max_s"]
    )
    assert report.f3(bound["bound_s"]) == "4.109"
    assert report.latency_rows(latency)


def test_m7_report_bound_section_never_missing_when_data_exists(tmp_path):
    latency = json.loads(Path("data/manifest/m7_latency.json").read_text())
    raw = tmp_path / report.RAW_INPUTS["latency"][1]
    raw.parent.mkdir(parents=True)
    raw.write_text("{}\n")
    manifest = put(tmp_path, "m7_latency.json", latency)
    record_fingerprint(manifest, "latency", raw, "completed")
    text, _, _ = report.render_tables(tmp_path)
    assert "= 4.109s(반올림 전 값의 합)" in text and "판정 아님, 보수적 상한" in text
    section = next(s for s in text.split("## ") if s.startswith("지연 비캐시 보정치"))
    assert "미실행" not in section


def test_m7_report_summary_first_and_appendix_split(tmp_path):
    body, appendix, _ = report.render_tables(tmp_path)
    assert body.startswith("## 핵심 결과 요약")
    for title in (
        "probe",
        "운영 지표 test_gold",
        "지연 측정 이력 전체",
        "해시·고정 조건",
    ):
        assert f"## {title}" in appendix and f"## {title}" not in body
    assert "부록" in body


@pytest.mark.parametrize(
    "phrase",
    ["보정값이 있으면", "원문을 옮기지 않는다", "그대로 옮기고", "9개 규정 이상"],
)
def test_m7_report_preserved_text_reads_as_statements(phrase):
    assert phrase not in report.initial_document()


def test_m7_report_partial_achievement_status(tmp_path):
    h6 = {
        "pending": False,
        "message": None,
        "by_path_system": {"a|b": {"n": 5, "leaks": 0, "clopper_pearson_upper": 0.4}},
    }
    put(
        tmp_path,
        "m7_analysis.json",
        {
            "kappa": {"passed": True},
            "h5": {
                "verdict_text": H5_ACCEPT,
                "primary": {"n": 13, "mean_diff": 1.0, "ci_low": 0.5, "ci_high": 1.5},
            },
            "h6": h6,
        },
    )
    run = tmp_path / "data/manifest/m5_test_results/run-01.json"
    run.parent.mkdir(parents=True)
    hold = "판정 보류(차이 입증 못 함)"
    run.write_text(
        json.dumps(
            {
                "analysis": {
                    "hypotheses": {
                        "H1": {"verdict": "채택"},
                        "H2": {"verdict": hold},
                        "H3": {"verdict": hold},
                        "H4": {"verdict": hold},
                        "H7": {"verdict": "기각(반대 방향)"},
                    }
                }
            }
        )
    )
    body, _, _ = report.render_tables(tmp_path)
    assert (
        "부분 달성(7개 모두 사전 등록 규칙대로 판정·원인 분석 완료, "
        "H2·H3·H4는 판정 보류)"
    ) in body
