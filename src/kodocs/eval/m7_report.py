"""Offline M7 report; read measured aggregates without changing frozen decisions."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from kodocs.eval.m5_report import _git_commit, _load_optional_json, _load_optional_jsonl
from kodocs.eval.m6_inputs import GOLD_PATH, SILVER_PATH
from kodocs.eval.m7_leak import h6_verdict
from kodocs.eval.m7_rawhash import RawHashMismatch, verify_fingerprint
from kodocs.eval.m7_stats import HUMAN_FALLBACK

RESULT_PATH = Path("docs/results/2026-10-02-m7-answer-eval.md")
BEGIN, END = "<!-- BEGIN generated -->", "<!-- END generated -->"
MISSING = "미실행"
M5_LINK = "[M5 검색 사다리·제거 실험·전체 조합](2026-09-30-m5-retrieval.md)"
M6_LINK = "[M6 dev 비교·환경 표](2026-10-01-m6-agent-mcp.md)"
RAW_INPUTS = {
    "grades": ("m7_grade_summary.json", "data/interim/m7/grades.jsonl"),
    "security": ("m7_security_results.json", "data/interim/m7/security/runs.jsonl"),
    "latency": ("m7_latency.json", "data/interim/m7/latency/runs.jsonl"),
}
M6_COSTS = {
    ("test_gold", "simple"): 0.0140144,
    ("test_gold", "agentic"): 0.0329375,
    ("test_silver", "simple"): 0.0414915,
    ("test_silver", "agentic"): 0.11389,
}


class M7ReportError(ValueError):
    """A source aggregate or generated-region contract is invalid."""


def cell(value):
    if value is None:
        return MISSING
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def table(headers, rows):
    return "\n".join(
        [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
            *["| " + " | ".join(cell(x) for x in r) + " |" for r in rows],
        ]
    )


def details(value):
    """Render only explicitly selected aggregate branches, including zero/false."""
    if value is None:
        return MISSING
    rows = []

    def walk(v, path):
        if isinstance(v, dict) and v:
            for key, item in v.items():
                walk(item, f"{path}.{key}" if path else key)
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            for index, item in enumerate(v, 1):
                walk(item, f"{path}[{index}]")
        else:
            rows.append([path, "null(기록값)" if v is None else v])

    walk(value, "")
    return table(["항목", "값"], rows)


def aggregate_operations(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[f"{row['system']}|{row.get('type', MISSING)}"].append(row)
    result = {}
    for key, group in sorted(groups.items()):
        metrics = {"n": len(group)}
        for metric in (
            "input",
            "output",
            "cached_input",
            "cache_write",
            "cost_usd",
            "steps",
            "latency_s",
        ):
            values = [
                r.get(metric)
                if metric in ("cost_usd", "steps", "latency_s")
                else r.get("tokens", {}).get(metric)
                for r in group
            ]
            valid = sorted(v for v in values if v is not None)
            metrics[metric] = (
                None
                if not valid
                else {
                    "n": len(valid),
                    "missing": len(group) - len(valid),
                    "mean": statistics.mean(valid),
                    "median": statistics.median(valid),
                    "p95": valid[math.ceil(0.95 * len(valid)) - 1],
                    "total": math.fsum(valid),
                }
            )
        result[key] = metrics
    return result


def operation_table(aggregate):
    rows = []
    for label, values in aggregate.items():
        system, type_name = label.split("|")
        for metric, stats in values.items():
            if metric == "n":
                continue
            rows.append(
                [
                    system,
                    type_name,
                    metric,
                    values["n"],
                    *[
                        stats.get(k) if stats else None
                        for k in ("mean", "median", "p95", "total", "missing")
                    ],
                ]
            )
    return (
        table(
            [
                "시스템",
                "유형",
                "지표",
                "질의 n",
                "평균",
                "중앙값",
                "P95",
                "합계",
                "결측",
            ],
            rows,
        )
        if rows
        else MISSING
    )


def aggregate_costs(ledger):
    groups = {
        "운영 비용": defaultdict(list),
        "평가 비용": defaultdict(list),
        "별도 측정 비용": defaultdict(list),
    }
    for row in ledger:
        run, service = row.get("run_id", ""), row.get("service", "")
        if service.startswith("google."):
            category = "평가 비용"
        elif run.startswith(("m6-test", "m7-security")):
            category = "운영 비용"
        elif run.startswith("m7-latency"):
            category = "별도 측정 비용"
        else:
            continue
        groups[category][run].append(row["amount_usd"])
    return {
        k: {run: math.fsum(v) for run, v in sorted(g.items())}
        for k, g in groups.items()
    }


def verify_m6_costs(rows):
    for (scope, system), expected in M6_COSTS.items():
        selected = [r for r in rows if r["scope"] == scope and r["system"] == system]
        if selected and not math.isclose(
            math.fsum(r["cost_usd"] for r in selected), expected, abs_tol=5e-7
        ):
            raise M7ReportError(f"M6 비용 불일치: {scope}/{system}, 기대 ${expected}")


def operation_inputs(root):
    types = {}
    for path in (GOLD_PATH, SILVER_PATH):
        for q in _load_optional_jsonl(root / path):
            types[q["id"]] = q["type"]
    rows = []
    for scope, path in [
        ("test_gold", "data/interim/m6/test/run-01/runs.jsonl"),
        ("test_silver", "data/interim/m6/test/run-01/silver/runs.jsonl"),
    ]:
        for r in _load_optional_jsonl(root / path):
            if r.get("model") == "gpt-6-luna":
                rows.append(
                    r
                    | {
                        "scope": scope,
                        "type": types.get(r["question_id"], r.get("type", MISSING)),
                    }
                )
    verify_m6_costs(rows)
    return rows


def accuracy_table(scores, questions):
    groups = defaultdict(list)
    for answer_id, score in scores.items():
        scope, question_id, system, model = answer_id.split("|")
        if scope not in ("test_gold", "test_silver") or model != "gpt-6-luna":
            continue
        values = [v for v in score.get("correctness", []) if v is not None]
        if not values:
            continue
        q = questions.get(question_id, {})
        groups[
            (scope, q.get("source", MISSING), q.get("type", MISSING), system)
        ].append(statistics.mean(values))
    return (
        table(
            ["세트", "출처", "유형", "시스템", "n", "정답성 평균(기술 통계)"],
            [[*key, len(v), statistics.mean(v)] for key, v in sorted(groups.items())],
        )
        if groups
        else MISSING
    )


def failure_table(manifest):
    rows = []
    for scope, bucket in manifest.get("by_system_set", {}).items():
        for type_name, data in [("전체", bucket), *bucket.get("by_type", {}).items()]:
            for kind, value in data.get("classes", {}).items():
                rows.append(
                    [scope, type_name, kind, value["count"], value.get("ids", [])]
                )
            for kind in ("d1_partial", "refusal_violations"):
                if kind in data:
                    value = data[kind]
                    rows.append(
                        [scope, type_name, kind, value["count"], value.get("ids", [])]
                    )
    extra = {k: v for k, v in manifest.items() if k != "by_system_set"}
    return (
        (
            table(["시스템 × 세트", "유형", "분류", "건수", "ID"], rows)
            + "\n\n"
            + details(extra)
        )
        if rows
        else details(manifest or None)
    )


def render_tables(root):
    root = Path(root)
    errors, invalid, fingerprints = [], set(), {}
    unavailable = set()

    def load(name):
        return _load_optional_json(root / "data/manifest" / name)

    for key, (manifest, raw) in RAW_INPUTS.items():
        if not (root / raw).exists() and load(manifest).get("raw"):
            unavailable.add(key)
        if (root / raw).exists():
            try:
                fingerprints[key] = verify_fingerprint(
                    root / "data/manifest" / manifest, key, root / raw
                )
            except RawHashMismatch:
                errors.append(f"{key}: 원본 해시 불일치")
                invalid.add(key)
    analysis = load("m7_analysis.json")
    if "grades" in fingerprints:
        for name, document in [
            ("analysis", analysis),
            ("scores", load("m7_grade_scores.json")),
        ]:
            if document and document.get("raw") != fingerprints["grades"]:
                invalid.add("grades")
                errors.append(f"grades/{name}: 원본 해시 불일치")
    if "grades" in invalid | unavailable:
        analysis = {}
    security = load("m7_security_results.json")
    corrections = [
        h for h in security.get("history", []) if h.get("kind") == "judge_correction"
    ]
    if corrections:
        security = security | corrections[-1]["result"]
    latency = load("m7_latency.json")
    completed = [
        h for h in latency.get("history", []) if h.get("status") == "completed"
    ]
    if completed:
        latency = latency | completed[-1]["result"]
    h5 = dict(analysis.get("h5", {}))
    if (
        analysis.get("kappa", {}).get("passed") is False
        and h5
        and HUMAN_FALLBACK not in h5.get("verdict_text", "")
    ):
        h5["verdict_text"] = h5.get("verdict_text", "") + " " + HUMAN_FALLBACK
    h6 = analysis.get("h6")
    if "security" not in invalid | unavailable and security.get("groups"):
        from kodocs.eval.m7_security import analyze_h6

        # The existing judge authenticates registered c1 confirmation and uses
        # the latest corrected aggregate; never infer confirmation from counts.
        if (root / RAW_INPUTS["security"][1]).exists():
            h6 = analyze_h6(root)
        elif any(g.get("c1_candidates", 0) for g in security["groups"].values()):
            h6 = {"pending": True, "message": "H6 판정 대기(c1 후보 확인 필요)"}
        elif h6 is None:
            groups = {tuple(k.split("|")): v for k, v in security["groups"].items()}
            judged = h6_verdict(groups, {k: [] for k in groups}, None)
            h6 = {
                "pending": judged.pending,
                "message": judged.message,
                "by_path_system": {
                    "|".join(k): v for k, v in judged.by_path_system.items()
                },
            }
    if "security" in invalid | unavailable:
        h6 = None
    kappa = analysis.get("kappa", {})
    p95 = (
        latency.get("systems", {}).get("simple", {}).get("verdict")
        if "latency" not in invalid | unavailable
        else None
    )
    verdict = h5.get("verdict_text")
    m5_exists = (root / "docs/results/2026-09-30-m5-retrieval.md").exists()
    h6_status = (
        "미실행"
        if not h6
        else "판정 대기"
        if h6.get("pending")
        else (
            "미달"
            if any(g["leaks"] for g in h6.get("by_path_system", {}).values())
            else "달성"
        )
    )
    criteria = [
        ["검색 실험표", "달성" if m5_exists else MISSING, M5_LINK],
        [
            "사전 등록 가설·원인 분석",
            "판정 보류"
            if verdict and ("보류" in verdict or m5_exists)
            else "판정 대기",
            "H1~H7 표 참조",
        ],
        [
            "단순 대 Agentic 유형별 비교·실패 원인",
            "달성" if analysis and load("m7_failures.json") else MISSING,
            "H5·운영·실패 분석",
        ],
        [
            "보안 누설률 0%",
            h6_status,
            h6.get("message") if h6 and h6.get("pending") else "H6 경로별 판정 참조",
        ],
        [
            "κ ≥ 0.6",
            MISSING
            if not kappa
            else "판정 보류"
            if kappa.get("uninterpretable")
            else "달성"
            if kappa.get("passed")
            else "미달",
            kappa.get("quadratic_kappa"),
        ],
        [
            "단순 RAG P95 ≤ 5s",
            MISSING if p95 is None else "달성" if p95 == "P95 목표 달성" else "미달",
            p95,
        ],
    ]
    sections = []

    def section(title, value, key=None):
        content = (
            "원본 해시 불일치"
            if key in invalid
            else MISSING
            if key in unavailable
            else details(value)
        )
        sections.append(f"## {title}\n\n{content}")

    section(
        "실행 정보",
        {
            "고정 실행일(KST)": "2026-10-02",
            "생성일(KST)": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
            "생성 코드 커밋": _git_commit(root),
            "측정 코드 커밋": latency.get("code_commit"),
            **{
                f"{m} config_hash": load(f"{m}_config.json").get("config_hash")
                for m in ("m5", "m6", "m7")
            },
        },
    )
    sections.append(
        "## PRD 성공 기준 6개 체크표\n\n" + table(["기준", "상태", "근거"], criteria)
    )
    hypotheses = [
        [h, M5_LINK, "M5 확정 판정·원인 분석 참조"]
        for h in ("H1", "H2", "H3", "H4", "H7")
    ]
    hypotheses += [
        [
            "H5",
            "원본 해시 불일치" if "grades" in invalid else verdict or "판정 대기",
            "유형별·sql 제외·실패 원인 참조",
        ],
        [
            "H6",
            "원본 해시 불일치"
            if "security" in invalid
            else h6.get("message") or "경로별 사전 등록 판정은 H6 표 참조"
            if h6
            else MISSING,
            "보안 절·위협 모델 참조",
        ],
    ]
    sections.append(
        "## 가설별 판정과 원인 분석\n\n"
        + table(["가설", "판정", "원인 분석"], hypotheses)
    )
    section("채점 모델 검증(정답성)", analysis.get("kappa"), "grades")
    if kappa and "grades" not in invalid:
        sections.append(
            ("채점 검증 통과" if kappa.get("passed") else HUMAN_FALLBACK)
            + (" · κ 해석 불가" if kappa.get("uninterpretable") else "")
        )
    section("표본 구성", load("m7_human_sample.json") or None)
    section("사람 점수 분포", load("m7_human_grades.json").get("score_distribution"))
    section("probe", load("m7_probe.json") or None)
    section("파일럿", load("m7_pilot.json") or None)
    section(
        "채점 실행·형식 오류·점수 분포", load("m7_grade_summary.json") or None, "grades"
    )
    section(
        "H5: 13문항 판정·유형별·sql 제외 9문항·실버 29문항 방향·사람 기준·3회 편차",
        h5 or None,
        "grades",
    )
    section("Sol 사후 확인(판정 아님)", analysis.get("sol"), "grades")
    section("라우터 dev 판정표와 착수 여부", analysis.get("router"), "grades")
    questions = {
        q["id"]: q
        for path in (GOLD_PATH, SILVER_PATH)
        for q in _load_optional_jsonl(root / path)
    }
    sections.append(
        "## 단순 대 Agentic 정답성 유형별·실제/합성 분리(기술 통계)\n\n"
        + (
            "원본 해시 불일치"
            if "grades" in invalid
            else accuracy_table(analysis.get("scores", {}), questions)
        )
    )
    section("충실성 보조표 — 사람 검증 없음", analysis.get("faithfulness"), "grades")
    section("3회 채점 편차", analysis.get("variability"), "grades")
    section(
        "누설 판정 기준 사실 수·어휘 해시(원문 제외)",
        {k: v for k, v in load("m7_leak_facts.json").items() if k != "by_asker"}
        or None,
    )
    section("c1 사람 확인 등록", load("m7_leak_review.json") or None, "security")
    secset = load("m7_security_set.json")
    section(
        "보안 시나리오 분모·제외·보충",
        {
            k: secset[k]
            for k in (
                "counts",
                "included_counts",
                "non_control_total",
                "attack_strength_exclusions",
                "s6_exclusions",
                "supplement_count",
            )
            if k in secset
        }
        or None,
    )
    section(
        "보안 경로·시스템별 n·누설·에코·c3 제외", security.get("groups"), "security"
    )
    section("H6·c1 후보/확정·기준선", h6, "security")
    for key in (
        "c1_baseline",
        "control_false_refusal",
        "control_tool_call_errors",
        "partial_answer_rates",
        "injection_auxiliary_non_verdict",
        "parity_mismatch_count",
        "not_found_mismatch_count",
        "data_quality",
    ):
        section(f"보안 {key}", security.get(key), "security")
    if corrections:
        first, last = security["history"][0]["result"], corrections[-1]["result"]
        correction_rows = []
        for label in last["groups"]:
            correction_rows.append(
                [
                    f"{label} 누설",
                    first["groups"][label]["leaks"],
                    last["groups"][label]["leaks"],
                ]
            )
        for key in (
            "control_false_refusal",
            "control_tool_call_errors",
            "injection_auxiliary_non_verdict",
        ):
            correction_rows.append(
                [key, first.get(key, "정정 전 미분류"), last.get(key)]
            )
        sections.append(
            "## 보안 사후 정정 공개 — c2 식별자 고유성·공개본/팀본 같은 제목\n\n"
            + (
                "원본 해시 불일치"
                if "security" in invalid
                else MISSING
                if "security" in unavailable
                else table(
                    ["지표", "정정 전(첫 항목)", "정정 후(최신 judge_correction)"],
                    correction_rows,
                )
            )
        )
    section(
        "지연 — 스트리밍 모드에서 측정, 동시성 1·예열 제외",
        {
            k: latency[k]
            for k in (
                "systems",
                "concurrency",
                "warmup_excluded_count",
                "init_s",
                "limitation",
            )
            if k in latency
        }
        or None,
        "latency",
    )
    section("지연 측정 이력 전체(완료 회차만 판정)", latency.get("history"), "latency")
    if latency.get("history") and latency["history"][0].get("raw", {}).get("rows") == 0:
        sections.append(
            "1회차(seq 1): 설정 결함(`.env` 키 미로딩, FP-056)으로 API 호출 0건·`runs.jsonl` 0행 정지. RERUN_REASON과 이전 결과 해시는 위 이력 참조."
        )
    sections.append(
        "## 실패 분석 — 시스템 × 분류 × 세트(d1·d2·indeterminate·도구 부재·거절 위반)\n\n"
        + (
            "원본 해시 불일치"
            if "grades" in invalid
            else MISSING
            if "grades" in unavailable
            else failure_table(load("m7_failures.json"))
        )
    )
    rows = []
    try:
        rows = operation_inputs(root)
    except M7ReportError as exc:
        errors.append(str(exc))
    ledger = _load_optional_jsonl(root / "data/manifest/costs.jsonl")
    m6_total = math.fsum(r["cost_usd"] for r in rows)
    ledger_test = math.fsum(
        r["amount_usd"]
        for r in ledger
        if r.get("run_id") == "m6-test-01"
        and r.get("service", "").startswith("openai.gpt-6-luna.")
    )
    if rows and ledger and not math.isclose(m6_total, ledger_test, abs_tol=1e-9):
        errors.append("M6 비용 불일치: 실행 집계와 원장 m6-test-01")
    security_rows = (
        []
        if "security" in invalid | unavailable
        else _load_optional_jsonl(root / RAW_INPUTS["security"][1])
    )
    security_cost = math.fsum(
        r.get("cost_usd", 0)
        for r in security_rows
        if r.get("system") in ("simple", "agentic")
    )
    security_ledger = math.fsum(
        r["amount_usd"]
        for r in ledger
        if r.get("run_id") == security.get("run_id", "m7-security")
        and r.get("service", "").startswith("openai.gpt-6-luna.")
    )
    if (
        security_rows
        and ledger
        and not math.isclose(security_cost, security_ledger, abs_tol=1e-9)
    ):
        errors.append("보안 비용 불일치: 실행 집계와 원장")
    for r in security_rows:
        if r.get("system") in ("simple", "agentic"):
            rows.append(r | {"scope": "security", "type": r.get("kind", MISSING)})
    for scope in ("test_gold", "test_silver", "security"):
        op = aggregate_operations([r for r in rows if r["scope"] == scope])
        sections.append(
            f"## 운영 지표 {scope} — 평균·중앙값·P95(입력·출력·캐시 토큰·비용·단계·지연)\n\n"
            + (
                "원본 해시 불일치"
                if scope == "security" and "security" in invalid
                else operation_table(op)
            )
        )
    section(
        "비용 — 원장 정가 + 부가세, Gemini 평가 비용 분리",
        aggregate_costs(ledger) if ledger else None,
    )
    sections.append(
        "비용은 원장의 amount_usd(정가 + 부가세)다. 질의별 실행 기록의 비용을 재집계하고 M6 test·보안 실행의 원장 합계와 대조한다. Gemini probe·파일럿·본 채점이 같은 run_id를 쓰면 원장에는 합쳐져 있으므로 위 표는 그 run_id 그대로 보고한다. 파일럿·채점 manifest 비용은 해당 절에 별도로 병기하며 운영비에 합산하지 않는다. M6 운영 지연은 동시성 4 기술 통계이며 P95 판정은 별도 지연 절의 동시성 1 결과만 쓴다."
    )
    section(
        "예상 비용 대 실제 비용(실행별)",
        _load_optional_jsonl(root / "data/manifest/m7_dry_runs.jsonl") or None,
    )
    section(
        "지연 비캐시 보정치(재계산 없음)",
        {k: v for k, v in latency.items() if "correction" in k or "cache" in k} or None,
        "latency",
    )
    section("측정 환경(자동 수집 원문)", latency.get("environment"), "latency")
    sections.append(
        f"M6 환경·dev 모델별 비용/지연 비교는 {M6_LINK} 참조. BGE-M3 revision 5617a9f. 패키지명 정정: 임베딩은 sentence-transformers, OpenSearch는 httpx 직접 호출이다(FlagEmbedding·opensearch-py 미설치 표기는 라이브러리 선택과 다른 수집 목록 오류). 설치 실패가 아니다. 버전은 M5/M6 환경 기록 참조."
    )
    section("해시·고정 조건", load("m7_config.json") or None)
    sections.append(
        "검색은 M5 고정 구성, 답변 모델 gpt-6-luna·M6 고정 설정, 일반 답변은 M6 산출물 재사용. [사전 등록 M7 판정 규칙](hypotheses.md#m7-판정-규칙-2026-10-01-고정-m7-결과를-보기-전)·[위협 모델](../THREAT_MODEL.md) 참조."
    )
    if errors:
        sections.append("## 생성 검증 실패\n\n" + "\n".join(errors))
    return "\n\n".join(sections), errors


def initial_document():
    """Initial preserved sections; subsequent renders never touch these bytes."""
    return f"""# M7 답변 평가 결과

{BEGIN}
{END}

## 측정 환경 보충(사용자 보존 영역)

| 항목 | 기록 |
| --- | --- |
| 측정 환경 보충·패키지명 정정 | 미기록 |
| Gemini 콘솔 Usage 실행 전 | 미기록 |
| Gemini 콘솔 Usage 실행 후 | 미기록 |
| 원장 대조·차이(FP-037) | 미기록 |
| make test-integration 실행일·결과 | 미기록 |
| 사용자 메모 | 미기록 |

## 사용자 실행 절차

1. `uv add google-genai`(사용자 직접).
2. step 0~6 실행·리뷰.
3. `make m7-probe`.
4. `make m7-pilot`.
5. 루브릭 확정·커밋.
6. `make m7-cards` → 사람 채점(정답성만) → CSV 저장 → `make m7-human-register` → 커밋.
7. `make m7-freeze`.
8. `make m7-grade-dry` → `make m7-grade`(2026-12-31 전 완료).
9. step 7~9 실행 후 `make up`과 함께 **`make m6-serve`를 별도 터미널에서 실행**한 뒤 `make m7-security-set` → `make m7-security-dry` → `make m7-security`(MCP 재생은 실행 중인 서버에 연결). c1 후보 확인 CSV 저장 후 `make m7-leak-review-register`.
10. `make m7-analyze`.
11. step 10 실행 후 `make m7-latency-dry` → `make m7-latency`(동시성 1 단독, 다른 부하 없이).
12. `make m7-failures`, `make m7-report`.
13. Gemini 콘솔 Usage와 원장 대조(FP-037), `make test-integration` 결과 기록.
14. 결과 산출물·문서 커밋 전 `make verify`.

## 한계(사용자 보존 영역)

- M6 실행 당시의 `runs.jsonl` 해시가 없다. 집계에 영향을 주지 않는 답변 텍스트 변경은 `analyze_dev()` 재집계 비교로 탐지하지 못한다(M7-D1 한계).
- 코퍼스 조립에서 일부 별표의 단위·제목·표 밖 조항이 유실되어(M5 composite의 표 대체, 사람 채점 중 law-2200000149621 별표2에서 발견) 정답 근거가 부분적으로 빠진 문항이 있다.
- 합성 질문 일부는 정답이 여럿일 수 있는데 참조 답변은 하나라서, 근거에 맞는 다른 답도 정답성 0점이 된다. 두 시스템 공통이다(파일럿 예: s-syn-044, g-syn-010; 2026-10-01 파일럿 30호출 점검에서 확인).
- 블라인드 한계: SQL 결과 인용이나 답변 형식으로 채점자(Gemini·사람)가 시스템을 추정할 수 있다.
- 충실성 지표는 사람 검증이 없다(정답성만 사람 검증).
- 답변이 시스템·문항당 1회라 비결정성이 점수에 섞인다.
- 사람 채점자가 1인이라 채점자 간 일치를 잴 수 없다.
- "스트리밍 모드에서 측정"한 지연이며 외부 API 지연 변동이 있다.
- 지연 한계 1(임베딩 캐시): 지연 측정은 임베딩 캐시 적중 상태에서 이뤄져 임베딩 구간이 약 0.001초로 나왔다. "캐시 적중 상태 측정, 비캐시 보정치"를 함께 보고한다. 비캐시 보정 P95는 약 4.1초라는 관찰이 있으나 결과 manifest에 보정값이 있으면 그대로 옮기고 재계산하지 않는다. 5.0초 목표 이내이나 재측정하지 않았다.
- 지연 한계 2(스트리밍 parse_error): 스트리밍 실행에서 parse_error 4건이 있었다. 원인은 이번 마일스톤에서 확정하지 않고 M8 후보로 둔다. 조사는 오프라인 로그 확인만 한다(API 재호출 없음).
- 지연 한계 3(환경 기록): 환경 표의 패키지명이 실제 배포명과 달라(FlagEmbedding·opensearch-py가 미설치로 기록) 보고서에서 실제 사용 라이브러리인 sentence-transformers(임베딩)·httpx(OpenSearch 직접 HTTP 호출)로 정정한다. 자동 수집 원문과 배포명 정정을 구분하며, 임베딩 모델 BGE-M3는 revision 5617a9f로 적는다.
- Sol 재검토 신호: Sol 사후 비교에서 정답성 +0.17점, 비용은 약 18배(dev 합계 Luna 약 $0.095, Sol 약 $1.72)였다. 확증이 아닌 신호이며 모델 선택 고정은 바뀌지 않는다.
- 보안 sec-0072는 도구 호출 오류(tool_error)로 분류된 사례이며 누설 판정에서 거부와 구분해 적는다. S6 노출은 Agentic 4/32가 확정이고 나머지 26은 판정 불가로 남았다. 사람 채점 메모(`data/interim/m7/human_memos.md`, 비커밋)는 참고만 하고 원문을 옮기지 않는다.
- 판정기 구현 결함을 결과 확인 뒤 정정했다(c2 식별자 고유성). 정정 전·후를 모두 보고하며 임계값·분모는 바꾸지 않았다.
- 합성 코퍼스에는 같은 제목의 문서가 여럿 있다(공개본·팀본 포함).
- 단순 RAG의 S6 노출은 사전 검증과의 순위 정렬로 확정한 값이며 Agentic은 질의를 다시 써서 같은 방법을 쓸 수 없어 판정 불가 26/32가 남는다.
- 유료 등급 Gemini는 입력을 제품 개선에 쓰지 않는다([출처](https://ai.google.dev/gemini-api/docs/pricing), 확인일 2026-10-01).
- 합성 문항이 실제보다 쉽다는 점(EVAL 규칙)을 고려한다. 실제·합성 결과를 분리해서 해석하며 M5 H7의 반대 방향 결과도 함께 참조한다.
- H6 0건은 관측값이며 95% 상한 3/n이다. MCP 경로는 도구 수준이고 c1 판정은 사용자 확인에 의존한다.
- M6 한계인 문장형 정답 값으로 정답 값 포함률이 무력해진 점은 모델 선택 해상도를 제한한다. Sol 사후 확인은 재검토 신호다. Luna 부분 답변 경향은 보안의 부분 답변 비율·c1 기준선·거절 위반과 연결해 본다.

## M8 개선 후보(사용자 보존 영역)

별표 조립 시 표 밖 내용 유실: law-2200000149621 별표2(외부강의 사례금 상한액 표)의 코퍼스 텍스트에 단위 표기가 없다(문서 전체 '단위' 0회, 참조 답변은 "400(단위 천원)"). 2026-10-01 읽기 전용 확인: 원본 HWPX(`section0.xml`)·변환 PDF(18쪽)·pypdf·Docling·업스테이지(PDF·HWPX 직접) 파싱 결과 모두에 "(단위 : 천원)"이 있다. 유실은 파싱이 아니라 M5 composite 코퍼스 조립(`src/kodocs/chunking/corpus.py`의 `_composite_text_and_sections`)에서 일어난다: 업스테이지 표와 매칭된 별표는 pypdf 별표 영역 전체(별표 제목·단위·표 밖 조문 '2. 적용기준')를 표 마크다운만으로 대체한다(이 별표2는 pypdf 797자 → composite 293자). 같은 비교(composite 별표 vs pypdf 별표, 493개 섹션)에서 composite가 pypdf의 70% 미만인 별표 95개, pypdf에는 있고 composite에는 없는 '단위' 표기 14개 별표(9개 규정 이상: law-2200000148327·148341·148345·148727·148765·148769·149621·150485·150587·151395)가 있다. M8 개선 후보: 매칭된 별표는 표만 대체하지 말고 pypdf 별표 영역(제목·단위·표 밖 조항)을 유지한 채 표를 병합하거나, 표 앞뒤 머리말·단위를 청크에 보존한다. M7 수치는 이 유실이 있는 채로 측정되었다. 결과에 영향이 있는 문항은 `data_preparation_notes`로 표시한다.
"""


def write_report(root=Path(".")):
    root = Path(root).resolve()
    path = root / RESULT_PATH
    original = path.read_bytes() if path.exists() else initial_document().encode()
    begin, end = BEGIN.encode(), END.encode()
    if (
        original.count(begin) != 1
        or original.count(end) != 1
        or original.index(begin) >= original.index(end)
    ):
        raise M7ReportError("generated 표지의 짝·순서 오류")
    generated, errors = render_tables(root)
    content = (
        original[: original.index(begin) + len(begin)]
        + b"\n\n"
        + generated.encode()
        + b"\n\n"
        + original[original.index(end) :]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)
    if errors:
        raise M7ReportError("; ".join(errors))
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        print(write_report(args.root))
    except (M7ReportError, RawHashMismatch, ValueError, OSError) as exc:
        print(f"M7 보고서 실패: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
