"""Offline M7 report; read measured aggregates without changing frozen decisions."""

from __future__ import annotations

import argparse
import json
import math
import re
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
APPENDIX_PATH = Path("docs/results/2026-10-02-m7-answer-eval-appendix.md")
APPENDIX_LINK = "[부록(원본 집계)](2026-10-02-m7-answer-eval-appendix.md)"
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


def f3(value, signed=False):
    """Three-decimal text; a value rounding to zero never shows a minus sign."""
    if value is None:
        return MISSING
    text = f"{value:+.3f}" if signed else f"{value:.3f}"
    return text.replace("-0.000", "0.000").replace("+0.000", "0.000")


def ci3(low, high):
    return f"[{f3(low)}, {f3(high)}]"


def stat3(statistic):
    statistic = statistic or {}
    if statistic.get("mean_diff") is None:
        return MISSING
    return (
        f"{f3(statistic['mean_diff'], signed=True)} "
        f"{ci3(statistic.get('ci_low'), statistic.get('ci_high'))}"
    )


def fill_verdict(text, primary):
    """Fill the pre-registered '(차이, 95% CI, n=…)' slot with the measured values."""
    if not text or not primary or primary.get("mean_diff") is None:
        return text
    filled = (
        f"(차이 {f3(primary['mean_diff'], signed=True)}, "
        f"95% CI {ci3(primary.get('ci_low'), primary.get('ci_high'))}, "
        f"n={primary.get('n')})"
    )
    return re.sub(r"\(차이, 95% CI, n=\d+\)", lambda _: filled, text)


def load_m5_hypotheses(root):
    runs = sorted((Path(root) / "data/manifest/m5_test_results").glob("run-*.json"))
    if not runs:
        return {}
    latest = json.loads(runs[-1].read_text(encoding="utf-8"))
    return latest.get("analysis", {}).get("hypotheses", {})


def noncache_bound(latency):
    """Conservative non-cached P95 bound; a bound, not a verdict, and no re-measurement."""
    systems = latency.get("systems", {})
    simple = systems.get("simple", {}).get("p95_s")
    embed = (
        systems.get("agentic", {}).get("segments", {}).get("embed_s", {}).get("max_s")
    )
    if simple is None or embed is None:
        return None
    return {
        "simple_p95_s": simple,
        "agentic_embed_max_s": embed,
        "bound_s": simple + embed,
        "target_s": systems.get("simple", {}).get("target_s"),
    }


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


def hypothesis_rows(m5, h5, h6, verdict):
    """H1~H7 verdict rows; M5 values are copied from the M5 manifest, not recomputed."""
    rows = []
    for key in ("H1", "H2", "H3", "H4"):
        value = m5.get(key, {})
        rows.append(
            [
                key,
                value.get("verdict"),
                stat3(value.get("statistic")),
                value.get("n"),
                f"{value.get('comparison', MISSING)} ({value.get('metric', MISSING)})",
            ]
        )
    primary = h5.get("primary", {})
    types = h5.get("by_type", {})
    rows.append(
        [
            "H5",
            verdict or "판정 대기",
            stat3(primary),
            primary.get("n"),
            "유형별 차이 "
            + ", ".join(f"{k} {f3(v.get('mean_diff'), True)}" for k, v in types.items())
            + (
                f"; sql 제외 {stat3(h5['sql_excluded'])}"
                if h5.get("sql_excluded")
                else ""
            ),
        ]
    )
    groups = (h6 or {}).get("by_path_system", {})
    rows.append(
        [
            "H6",
            MISSING
            if not h6
            else h6.get("message")
            or ("판정 대기" if h6.get("pending") else _h6_verdict_text(groups)),
            "누설 "
            + ", ".join(sorted({f"{g['leaks']}/{g['n']}" for g in groups.values()}))
            if groups
            else MISSING,
            "경로·시스템당 " + "/".join(sorted({str(g["n"]) for g in groups.values()}))
            if groups
            else MISSING,
            "경로·시스템별 판정은 H6 표, 근거는 부록의 보안 절",
        ]
    )
    h7 = m5.get("H7", {})
    rows.append(
        [
            "H7",
            h7.get("verdict"),
            stat3(h7.get("statistic")),
            f"합성 {h7.get('n_synthetic')}·실제 {h7.get('n_real')}" if h7 else MISSING,
            f"{h7.get('comparison', MISSING)} ({h7.get('metric', MISSING)})",
        ]
    )
    return rows


def _h6_verdict_text(groups):
    leaks = sum(g["leaks"] for g in groups.values())
    return (
        f"관측 누설 {leaks}건, 4경로·시스템 모두 H6 채택(관측값)"
        if not leaks and len(groups) == 4
        else f"관측 누설 {leaks}건, 경로별 판정은 H6 표 참조"
    )


def h6_rows(h6, security):
    groups = (h6 or {}).get("by_path_system", {})
    rows = []
    for label, g in groups.items():
        control = g.get("control_false_refusal") or {}
        rows.append(
            [
                label,
                f"{g['leaks']}/{g['n']}",
                f"{f3(g.get('clopper_pearson_upper'))}",
                f"{g.get('c1_candidates')}/{g.get('c1_confirmed')}",
                f"{control.get('numerator')}/{control.get('denominator')}"
                if control
                else MISSING,
            ]
        )
    return rows


def correction_note(security):
    """Pre/post disclosure for the post-hoc c2 judge correction (leaks and tool-path controls)."""
    history = security.get("history", [])
    corrections = [h for h in history if h.get("kind") == "judge_correction"]
    if not corrections or not history:
        return None
    first, last = history[0]["result"], corrections[-1]["result"]
    before = sum(g["leaks"] for g in first.get("groups", {}).values())
    after = sum(g["leaks"] for g in last.get("groups", {}).values())
    pre_refusal = {
        label: v
        for label, v in first.get("control_false_refusal", {}).items()
        if isinstance(v, dict) and v.get("numerator")
    }
    tool_errors = {
        label: v
        for label, v in last.get("control_tool_call_errors", {}).items()
        if isinstance(v, dict) and v.get("numerator")
    }
    text = (
        f"사후 정정 전 누설 판정 {before}건(c2 식별자 고유성 결함 — 공개본·팀본 같은 제목)은 "
        f"정정 후 {after}건이다. 정정 전·후를 모두 부록에 보존한다."
    )
    if pre_refusal:
        text += (
            " 도구 경로 대조군 오거절은 정정 전 "
            + ", ".join(
                f"{k} {v['numerator']}/{v['denominator']}"
                for k, v in pre_refusal.items()
            )
            + "에서 정정 후 "
            + (
                "도구 호출 오류 "
                + ", ".join(
                    f"{k} {v['numerator']}/{v['denominator']}"
                    for k, v in tool_errors.items()
                )
                + "로 재분류돼 오거절 0건이다."
            )
        )
    return text


def latency_rows(latency):
    rows = []
    for key, label in (("simple", "단순 RAG"), ("agentic", "Agentic RAG")):
        system = latency.get("systems", {}).get(key)
        if not system:
            continue
        segments, total = system.get("segments", {}), system.get("total", {})
        rows.append(
            [
                label,
                f3(total.get("p50_s")),
                (
                    f"{f3(system.get('p95_s'))} "
                    f"{ci3(*(system.get('p95_ci95_s') or (None, None)))}"
                ),
                (
                    f"{f3(segments.get('ttft_s', {}).get('p50_s'))} / "
                    f"{f3(segments.get('ttft_s', {}).get('p95_s'))}"
                ),
                (
                    f"{f3(segments.get('search_s', {}).get('p50_s'))} / "
                    f"{f3(segments.get('search_s', {}).get('p95_s'))}"
                ),
                f"{system.get('execution_count')} / {system.get('failure_count')}",
            ]
        )
    return rows


def dev_cost_ratio(m6_dev):
    totals = {}
    for model, systems in m6_dev.get("operations", {}).items():
        totals[model] = math.fsum(v["cost_usd"]["total"] for v in systems.values())
    luna, sol = totals.get("gpt-6-luna"), totals.get("gpt-6.1-sol")
    if not luna or not sol:
        return None
    return {"luna": luna, "sol": sol, "ratio": sol / luna}


def summary_section(ctx):
    """Human-readable summary; every number comes from a manifest, rounded to 3 decimals."""
    analysis, h5, h6 = ctx["analysis"], ctx["h5"], ctx["h6"]
    latency, security = ctx["latency"], ctx["security"]
    lines = ["## 핵심 결과 요약", ""]
    lines += [
        "### PRD 성공 기준 6개 체크표",
        "",
        table(["기준", "상태", "핵심 수치"], ctx["criteria"]),
    ]
    lines += [
        "",
        "### H1~H7 판정",
        "",
        table(
            ["가설", "판정", "차이 [95% CI]", "n", "비교·보조"],
            hypothesis_rows(ctx["m5"], h5, h6, ctx["verdict"]),
        ),
        "",
        f"H1~H4·H7의 값과 원인 분석은 {M5_LINK}에서 옮겼다.",
    ]
    if h5.get("primary"):
        silver = h5.get("silver_direction", {})
        rows = [
            [
                f"H5 {name}",
                v.get("n"),
                f3(v.get("agentic_mean")),
                f3(v.get("simple_mean")),
                f3(v.get("mean_diff"), True),
            ]
            for name, v in h5.get("by_type", {}).items()
        ]
        for label, statistic in (
            ("H5 전체", h5["primary"]),
            ("sql 제외", h5.get("sql_excluded")),
            ("실버 방향 확인(판정 아님)", silver.get("comparison")),
            ("사람 기준", h5.get("human")),
        ):
            if statistic:
                rows.append(
                    [
                        label,
                        statistic.get("n"),
                        "해당 없음",
                        "해당 없음",
                        stat3(statistic),
                    ]
                )
        lines += [
            "",
            "### H5 유형별·보조 비교(정답성, 0~3점)",
            "",
            table(["비교", "n", "Agentic", "단순", "차이 [95% CI]"], rows),
        ]
    h6_table = h6_rows(h6, security)
    if h6_table:
        lines += [
            "",
            "### H6 경로·시스템별(비밀 정보 누설)",
            "",
            table(
                [
                    "경로·시스템",
                    "누설",
                    "Clopper-Pearson 95% 상한",
                    "c1 후보/확정",
                    "대조군 오거절",
                ],
                h6_table,
            ),
        ]
        note = correction_note(security)
        if note:
            lines += ["", note]
    rows = latency_rows(latency)
    if rows:
        lines += [
            "",
            "### 지연(스트리밍 모드, 동시성 1, 예열 제외, 단위 초)",
            "",
            table(
                [
                    "시스템",
                    "P50",
                    "P95 [95% CI]",
                    "TTFT P50 / P95",
                    "검색 P50 / P95",
                    "실행 / 실패",
                ],
                rows,
            ),
        ]
        bound = ctx["bound"]
        if bound:
            lines += [
                "",
                (
                    f"비캐시 보정 상한 = {f3(bound['simple_p95_s'])} + "
                    f"{f3(bound['agentic_embed_max_s'])} = {f3(bound['bound_s'])}s"
                    "(반올림 전 값의 합, 판정 아님, 보수적 상한)."
                ),
            ]
    sol, router = analysis.get("sol", {}), analysis.get("router", {})
    extra = []
    overall, cost = sol.get("overall"), ctx["dev_cost"]
    if overall:
        text = f"Sol 사후 신호(판정 아님): Sol − Luna {stat3(overall)}, n={overall.get('n')}"
        if cost:
            text += f", dev 비용 약 {cost['ratio']:.0f}배(Luna ${cost['luna']:.3f}, Sol ${cost['sol']:.3f})"
        extra.append(text + (" — 재검토 신호." if sol.get("recheck_signal") else "."))
    if router:
        extra.append(
            router["note"]
            if router.get("note")
            else "라우터 " + ("착수" if router.get("launch") else "미착수")
        )
    if extra:
        lines += ["", *[f"- {e}" for e in extra]]
    lines += ["", "세부 표와 원본 집계는 " + APPENDIX_LINK + "에 있다."]
    return "\n".join(lines)


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
    primary = h5.get("primary", {})
    verdict = fill_verdict(h5.get("verdict_text"), primary)
    m5_exists = (root / "docs/results/2026-09-30-m5-retrieval.md").exists()
    m5 = load_m5_hypotheses(root)
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
    held = [
        h
        for h, v in [
            *[
                (k, m5.get(k, {}).get("verdict") or "")
                for k in ("H1", "H2", "H3", "H4", "H7")
            ],
            ("H5", verdict or ""),
        ]
        if "보류" in v
    ]
    hypotheses_done = bool(verdict and m5 and h6 and not h6.get("pending"))
    hypotheses_status = (
        "판정 대기"
        if not hypotheses_done
        else "부분 달성(7개 모두 사전 등록 규칙대로 판정·원인 분석 완료, "
        + "·".join(sorted(held))
        + "는 판정 보류)"
        if held
        else "달성"
    )
    groups6 = (h6 or {}).get("by_path_system", {})
    bound = noncache_bound(latency) if "latency" not in invalid | unavailable else None
    simple_latency = latency.get("systems", {}).get("simple", {})
    criteria = [
        ["검색 실험표", "달성" if m5_exists else MISSING, M5_LINK],
        ["사전 등록 가설·원인 분석", hypotheses_status, "아래 H1~H7 판정 표"],
        [
            "단순 대 Agentic 유형별 비교·실패 원인",
            "달성" if analysis and load("m7_failures.json") else MISSING,
            f"H5 {stat3(primary)}, n={primary.get('n')}" if primary else MISSING,
        ],
        [
            "보안 누설률 0%",
            h6_status,
            h6.get("message")
            if h6 and h6.get("pending")
            else (
                f"{len(groups6)}경로·시스템 누설 "
                + "/".join(sorted({f"{g['leaks']}/{g['n']}" for g in groups6.values()}))
                + f", Clopper-Pearson 상한 {f3(max(g['clopper_pearson_upper'] for g in groups6.values()))}"
            )
            if groups6 and all("clopper_pearson_upper" in g for g in groups6.values())
            else "H6 경로별 판정 참조",
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
            f"{f3(kappa.get('quadratic_kappa'))} {ci3(kappa.get('ci_low'), kappa.get('ci_high'))}"
            if kappa.get("quadratic_kappa") is not None
            else MISSING,
        ],
        [
            "단순 RAG P95 ≤ 5s",
            MISSING if p95 is None else "달성" if p95 == "P95 목표 달성" else "미달",
            f"P95 {f3(simple_latency.get('p95_s'))}s "
            f"{ci3(*(simple_latency.get('p95_ci95_s') or (None, None)))}, 목표 {simple_latency.get('target_s')}s"
            if simple_latency.get("p95_s") is not None
            else p95,
        ],
    ]
    body, sections = [], []  # sections is the appendix

    def section(title, value, key=None, target=None):
        content = (
            "원본 해시 불일치"
            if key in invalid
            else MISSING
            if key in unavailable
            else details(value)
        )
        (sections if target is None else target).append(f"## {title}\n\n{content}")

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
        target=body,
    )
    body.insert(
        0,
        summary_section(
            {
                "analysis": analysis,
                "h5": h5,
                "h6": h6,
                "kappa": kappa,
                "latency": latency if "latency" not in invalid | unavailable else {},
                "security": security if "security" not in invalid | unavailable else {},
                "criteria": criteria,
                "m5": m5,
                "verdict": verdict,
                "bound": bound,
                "dev_cost": dev_cost_ratio(load("m6_dev.json")),
            }
        ),
    )
    body.extend(f"원본 해시 불일치: {key}" for key in sorted(invalid))
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
    body.append(
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
        target=body,
    )
    body.append(
        "비용은 원장의 amount_usd(정가 + 부가세)다. 질의별 실행 기록의 비용을 재집계하고 M6 test·보안 실행의 원장 합계와 대조한다. Gemini probe·파일럿·본 채점이 같은 run_id를 쓰면 원장에는 합쳐져 있으므로 위 표는 그 run_id 그대로 보고한다. 파일럿·채점 manifest 비용은 해당 절에 별도로 병기하며 운영비에 합산하지 않는다. M6 운영 지연은 동시성 4 기술 통계이며 P95 판정은 별도 지연 절의 동시성 1 결과만 쓴다."
    )
    section(
        "예상 비용 대 실제 비용(실행별)",
        _load_optional_jsonl(root / "data/manifest/m7_dry_runs.jsonl") or None,
    )
    body.append(
        "## 지연 비캐시 보정치(판정 아님, 보수적 상한)\n\n"
        + (
            "원본 해시 불일치"
            if "latency" in invalid
            else MISSING
            if "latency" in unavailable or not bound
            else "지연 측정은 임베딩 캐시 적중 상태에서 이뤄졌다(캐시 적중 상태 측정). "
            "보정 상한 = 단순 RAG P95 "
            f"{f3(bound['simple_p95_s'])} + Agentic 비캐시 임베딩 최대 {f3(bound['agentic_embed_max_s'])} "
            f"= {f3(bound['bound_s'])}s(반올림 전 값의 합)다. 목표 {bound['target_s']}s 이내이며, "
            "판정이 아니라 보수적 상한이다. 재측정하지 않았다."
        )
    )
    section("측정 환경(자동 수집 원문)", latency.get("environment"), "latency")
    body.append(
        f"M6 환경·dev 모델별 비용/지연 비교는 {M6_LINK} 참조. BGE-M3 revision 5617a9f. 패키지명 정정: 임베딩은 sentence-transformers, OpenSearch는 httpx 직접 호출이다(FlagEmbedding·opensearch-py 미설치 표기는 라이브러리 선택과 다른 수집 목록 오류). 설치 실패가 아니다. 버전은 M5/M6 환경 기록 참조."
    )
    section("해시·고정 조건", load("m7_config.json") or None)
    body.append(
        "검색은 M5 고정 구성, 답변 모델 gpt-6-luna·M6 고정 설정, 일반 답변은 M6 산출물 재사용. [사전 등록 M7 판정 규칙](hypotheses.md#m7-판정-규칙-2026-10-01-고정-m7-결과를-보기-전)·[위협 모델](../THREAT_MODEL.md) 참조."
    )
    if errors:
        body.append("## 생성 검증 실패\n\n" + "\n".join(errors))
    return "\n\n".join(body), "\n\n".join(sections), errors


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
- 지연 한계 1(임베딩 캐시): 지연 측정은 임베딩 캐시 적중 상태에서 이뤄져 임베딩 구간이 약 0.001초로 나왔다. 따라서 이 지연은 캐시 적중 상태 측정이며, 본문의 "지연 비캐시 보정치" 절에 단순 RAG P95와 Agentic 비캐시 임베딩 최대값을 더한 보수적 상한(약 4.11초, 판정 아님)을 계산식과 함께 적었다. 5.0초 목표 이내이나 재측정하지 않았다.
- 지연 한계 2(스트리밍 parse_error): 스트리밍 실행에서 parse_error 4건이 있었다. 원인은 이번 마일스톤에서 확정하지 않고 M8 후보로 둔다. 후속 조사는 오프라인 로그 확인으로 제한한다(API 재호출 없음).
- 지연 한계 3(환경 기록): 환경 표의 패키지명이 실제 배포명과 달라(FlagEmbedding·opensearch-py가 미설치로 기록) 실제 사용 라이브러리는 sentence-transformers(임베딩)와 httpx(OpenSearch 직접 HTTP 호출)다. 부록의 자동 수집 원문은 그대로 두고 정정은 본문에 따로 적었으며, 임베딩 모델 BGE-M3의 revision은 5617a9f다.
- Sol 재검토 신호: Sol 사후 비교에서 정답성 +0.17점, 비용은 약 18배(dev 합계 Luna 약 $0.095, Sol 약 $1.72)였다. 확증이 아닌 신호이며 모델 선택 고정은 바뀌지 않는다.
- 보안 sec-0072는 도구 호출 오류(tool_error)로 분류된 사례이며 누설 판정에서 거부와 구분했다. S6 노출은 Agentic 4/32가 확정이고 나머지 26은 판정 불가로 남았다. 사람 채점 메모(`data/interim/m7/human_memos.md`, 비커밋)는 참고 자료로만 썼고 이 문서에 원문을 싣지 않았다.
- 판정기 구현 결함을 결과 확인 뒤 정정했다(c2 식별자 고유성). 정정 전·후를 모두 보고하며 임계값·분모는 바꾸지 않았다.
- 합성 코퍼스에는 같은 제목의 문서가 여럿 있다(공개본·팀본 포함).
- 단순 RAG의 S6 노출은 사전 검증과의 순위 정렬로 확정한 값이며 Agentic은 질의를 다시 써서 같은 방법을 쓸 수 없어 판정 불가 26/32가 남는다.
- 유료 등급 Gemini는 입력을 제품 개선에 쓰지 않는다([출처](https://ai.google.dev/gemini-api/docs/pricing), 확인일 2026-10-01).
- M5 H7에서 합성 문항이 실제 문항보다 쉽다는 가설은 반대 방향으로 기각됐다(합성 0.469, 실제 0.917). 이 문서도 실제·합성 결과를 분리해서 해석했다.
- H6 0건은 관측값이며 95% 상한 3/n이다. MCP 경로는 도구 수준이고 c1 판정은 사용자 확인에 의존한다.
- M6 한계인 문장형 정답 값으로 정답 값 포함률이 무력해진 점은 모델 선택 해상도를 제한한다. Sol 사후 확인은 재검토 신호다. Luna 부분 답변 경향은 보안의 부분 답변 비율·c1 기준선·거절 위반과 연결해 본다.

## M8 개선 후보(사용자 보존 영역)

별표 조립 시 표 밖 내용 유실: law-2200000149621 별표2(외부강의 사례금 상한액 표)의 코퍼스 텍스트에 단위 표기가 없다(문서 전체 '단위' 0회, 참조 답변은 "400(단위 천원)"). 2026-10-01 읽기 전용 확인: 원본 HWPX(`section0.xml`)·변환 PDF(18쪽)·pypdf·Docling·업스테이지(PDF·HWPX 직접) 파싱 결과 모두에 "(단위 : 천원)"이 있다. 유실은 파싱이 아니라 M5 composite 코퍼스 조립(`src/kodocs/chunking/corpus.py`의 `_composite_text_and_sections`)에서 일어난다: 업스테이지 표와 매칭된 별표는 pypdf 별표 영역 전체(별표 제목·단위·표 밖 조문 '2. 적용기준')를 표 마크다운만으로 대체한다(이 별표2는 pypdf 797자 → composite 293자). 같은 비교(composite 별표 vs pypdf 별표, 493개 섹션)에서 composite가 pypdf의 70% 미만인 별표 95개, pypdf에는 있고 composite에는 없는 '단위' 표기 14개 별표(10개 규정: law-2200000148327·148341·148345·148727·148765·148769·149621·150485·150587·151395)가 있다. M8 개선 후보: 매칭된 별표는 표만 대체하지 말고 pypdf 별표 영역(제목·단위·표 밖 조항)을 유지한 채 표를 병합하거나, 표 앞뒤 머리말·단위를 청크에 보존한다. M7 수치는 이 유실이 있는 채로 측정되었다. 결과에 영향이 있는 문항은 `data_preparation_notes`로 표시한다.
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
    generated, appendix, errors = render_tables(root)
    appendix_path = root / APPENDIX_PATH
    appendix_text = (
        "# M7 답변 평가 결과 부록(원본 집계)\n\n"
        "`make m7-report`가 만드는 생성 문서다. 본문은 "
        "[M7 답변 평가 결과](2026-10-02-m7-answer-eval.md)이며, 이 파일은 손으로 고치지 않는다.\n\n"
        + appendix
        + "\n"
    )
    temporary_appendix = appendix_path.with_suffix(".tmp")
    appendix_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_appendix.write_text(appendix_text, encoding="utf-8")
    temporary_appendix.replace(appendix_path)
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
