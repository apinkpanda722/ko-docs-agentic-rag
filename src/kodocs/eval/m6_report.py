"""Render the fixed M6 report using only offline aggregate artifacts."""

from __future__ import annotations

import argparse
import math
import re
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from kodocs.eval.m5_report import (
    _git_commit,
    _load_optional_json,
    _load_optional_jsonl,
    _write_atomic,
)

TEMPLATE_PATH = Path(__file__).parent / "templates/m6_agent.md"
REPORT_PATH = Path("docs/results/2026-10-01-m6-agent-mcp.md")
BEGIN, END = "<!-- BEGIN generated -->", "<!-- END generated -->"
MANIFEST = Path("data/manifest")
MODELS = ("gpt-6-luna", "gpt-6.1-sol")
SYSTEMS = ("simple", "agentic")
TYPES = ("identifier", "multi_doc", "semantic", "temporal", "sql", "table")
MISSING = "미실행"


class M6ReportError(ValueError):
    """Invalid aggregate input or generated-region markers."""


def _map(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _at(value: object, *keys: str) -> object:
    for key in keys:
        value = _map(value).get(key)
    return value


def _num(value: object) -> int | float | None:
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    ):
        return value
    return None


def _cell(value: object) -> str:
    if value is None:
        return MISSING
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, float):
        return f"{value:.6g}" if math.isfinite(value) else MISSING
    if not isinstance(value, (str, int)):
        return MISSING
    text = re.sub(r"(?i)\bbearer\s+\S+|\bsk-[\w-]+", "[REDACTED]", str(value))
    return (
        text.replace("|", "\\|")
        .replace("\n", " ")
        .replace("\r", " ")
        .replace("<", "&lt;")
    )


def _mapping_rows(value: object) -> list[Mapping]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _hash(value: object) -> str | None:
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{7,64}", value)
        else None
    )


def _ids(value: object) -> str:
    if not isinstance(value, list):
        return MISSING
    ids = [
        item
        for item in value
        if isinstance(item, str) and re.fullmatch(r"[gs]-(?:real|syn)-\d{3}", item)
    ]
    return ", ".join(ids) or "없음"


def _rate(value: object) -> str:
    numerator, denominator, rate = (
        _num(_at(value, key)) for key in ("numerator", "denominator", "rate")
    )
    if numerator is None or denominator is None:
        return MISSING
    return f"{_cell(numerator)}/{_cell(denominator)} ({_cell(rate)})"


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    if not rows:
        return MISSING
    return "\n".join(
        [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
            *("| " + " | ".join(_cell(cell) for cell in row) + " |" for row in rows),
        ]
    )


def _section(title: str, content: str) -> str:
    return f"## {title}\n\n{content}"


def _inputs(data: Mapping) -> str:
    if not data:
        return MISSING
    rows = []
    for label, key in (
        ("dev", "dev_set"),
        ("H5 풀", "h5_pool"),
        ("H5 풀(sql 제외 보조)", "h5_pool_ex_sql"),
        ("실버 test", "silver_test"),
        ("H5 실버 방향 확인", "h5_silver_direction"),
        ("정답 값 분모", "answer_match_denominator"),
        ("게이트", "gate_permission_dev"),
        ("답 없음", "unanswerable_dev"),
    ):
        section = _map(data.get(key))
        by_set = _map(section.get("by_set"))
        ids = section.get("ids")
        if "by_set" not in section and isinstance(ids, list):
            by_set = {
                name: sum(
                    isinstance(qid, str) and qid.startswith(prefix) for qid in ids
                )
                for name, prefix in (("gold", "g-"), ("silver", "s-"))
            }
        rows.append(
            [
                label,
                _num(_at(data, key, "count")),
                _num(by_set.get("gold")),
                _num(by_set.get("silver")),
            ]
        )
    source = _table(
        ("출처", "정답 값 분모"),
        [
            [name, _num(_at(data, "answer_match_denominator", "by_source", name))]
            for name in ("real", "synthetic")
        ],
    )
    router = []
    for group, label in (("eligible", "8 이상"), ("below_threshold", "8 미만")):
        for kind in TYPES:
            value = _at(data, "router_types", group, kind, "count")
            if value is not None:
                router.append([kind, _num(value), label])
    excluded = [
        _map(row).get("id")
        for row in (_at(data, "answer_match_denominator", "excluded") or [])
    ]
    return "\n\n".join(
        [
            _table(("집합", "문항 수", "골드", "실버"), rows),
            source,
            _table(("라우터 유형", "문항 수", "최소 8문항 기준"), router),
            "정답 값 분모 제외 문항 ID: " + _ids(excluded),
        ]
    )


def _temporal(data: Mapping) -> str:
    note = "사후 기술 통계이며 M5 판정을 바꾸지 않는다. M5 개정 전 우선은 2/5였다."
    if not data:
        return MISSING + "\n\n" + note
    off = _at(data, "temporal", "prefer_current_off", "rows") or []
    on = {
        _map(row).get("qid"): _map(row)
        for row in (_at(data, "temporal", "prefer_current_on", "rows") or [])
    }
    rows = []
    for row in off:
        qid = _map(row).get("qid")
        if _ids([qid]) == "없음":
            continue
        after = on.get(qid, {})
        rows.append(
            [
                qid,
                row.get("split") if row.get("split") in {"dev", "test"} else None,
                _num(row.get("current_rank")),
                _num(row.get("prior_rank")),
                _num(after.get("current_rank")),
                _num(after.get("prior_rank")),
            ]
        )
    counts = []
    for label, values in (("끔", off), ("켬", list(on.values()))):
        measured = [
            row.get("prior_above_current")
            for row in values
            if isinstance(row.get("prior_above_current"), bool)
        ]
        counts.append(
            [
                label,
                sum(measured) if measured else None,
                len(measured) if measured else None,
            ]
        )
    changes = _map(data.get("top10_changes"))
    non_temporal = _num(changes.get("non_temporal_change_count"))
    change_rows = [
        [
            _ids([row.get("question_id")]),
            (
                "구성 변경"
                if row.get("membership_changed") is True
                else "순서만"
                if row.get("order_only") is True
                else MISSING
            ),
            _cell(
                row.get("relevant_docs_all_current")
                if isinstance(row.get("relevant_docs_all_current"), bool)
                else None
            ),
        ]
        for row in _mapping_rows(changes.get("non_temporal_changes"))
    ]
    groups = _map(changes.get("by_group"))
    group_rows = [
        [
            group,
            _num(_at(groups, group, "question_count")),
            _num(_at(groups, group, "changed_count")),
        ]
        for group in ("gold_dev", "gold_test", "silver_dev", "silver_test")
        if group in groups
    ]
    return "\n\n".join(
        [
            note,
            _table(
                (
                    "문항 ID",
                    "split",
                    "끔 현행 순위",
                    "끔 개정 전 순위",
                    "켬 현행 순위",
                    "켬 개정 전 순위",
                ),
                rows,
            ),
            _table(("옵션", "개정 전 우선 수", "분모"), counts),
            _table(("집합", "검사 문항", "상위 10 변경 문항"), group_rows),
            (
                "비 temporal 문항 중 상위 10 순서가 바뀐 문항 수: "
                f"{_cell(non_temporal)} (구성 변경 "
                f"{_cell(_num(changes.get('non_temporal_membership_changed_count')))}, "
                "순서만 "
                f"{_cell(_num(changes.get('non_temporal_order_only_count')))}); "
                "바뀐 문항의 정답 문서가 모두 현행판인 문항 수: "
                f"{_cell(_num(changes.get('non_temporal_relevant_all_current_count')))}. "
                "같은 rule_id의 여러 판이 상위에 함께 오면 temporal이 아니어도 교환이 일어난다 "
                "(non_temporal_changes)."
            ),
            (
                _table(("문항 ID", "변경 유형", "정답 문서 모두 현행판"), change_rows)
                if change_rows or non_temporal is None
                else "변경된 비 temporal 문항 없음"
            ),
        ]
    )


def _operations(data: object, concurrency: object) -> str:
    rows = []
    for model in MODELS:
        for system in SYSTEMS:
            op = _map(_at(data, model, system))
            if not op:
                continue
            guards = _map(op.get("guardrail_terminations"))
            guard_values = [
                _num(guards.get(key))
                for key in (
                    "guardrail_max_steps",
                    "guardrail_tokens",
                    "guardrail_timeout",
                )
            ]
            guard_count = (
                sum(guard_values) if all(v is not None for v in guard_values) else None
            )
            rows.append(
                [
                    model,
                    system,
                    _num(op.get("n")),
                    _num(_at(op, "cost_usd", "total")),
                    _num(_at(op, "cost_usd", "mean")),
                    _num(_at(op, "latency_s", "p50")),
                    _num(concurrency),
                    _num(_at(op, "steps", "mean")),
                    _num(_at(op, "steps", "max")),
                    guard_count,
                    _num(_at(op, "statuses", "billing_uncertain")),
                    _num(op.get("init_s")),
                ]
            )
    return _table(
        (
            "모델 ID",
            "시스템",
            "실행 수",
            "비용 합 USD",
            "평균 비용 USD",
            "지연 P50 초",
            "동시성",
            "평균 단계",
            "최대 단계",
            "가드레일",
            "billing_uncertain",
            "초기화 초",
        ),
        rows,
    )


def _dev(data: Mapping) -> str:
    if not data:
        return MISSING
    rows, gates = [], []
    for model in MODELS:
        for system in SYSTEMS:
            metric = _at(data, "answer_match", model, system)
            if metric is not None:
                rows.append(
                    [
                        model,
                        system,
                        _rate(metric),
                        _rate(_at(metric, "by_source", "real")),
                        _rate(_at(metric, "by_source", "synthetic")),
                        _rate(_at(data, "unanswerable_refusal", model, system)),
                    ]
                )
        gate = _map(_at(data, "gate", model))
        if gate:
            gates.append(
                [
                    model,
                    _num(gate.get("execution_count")),
                    _num(gate.get("a_count")),
                    _num(gate.get("b_count")),
                    gate.get("passed")
                    if isinstance(gate.get("passed"), bool)
                    else None,
                ]
            )
    comparison = _map(data.get("paired_bootstrap"))
    paired = _table(
        (
            "왼쪽 모델",
            "오른쪽 모델",
            "짝 비교 차이(왼쪽−오른쪽)",
            "95% CI 하한",
            "95% CI 상한",
        ),
        [
            [
                comparison.get("left_model")
                if comparison.get("left_model") in MODELS
                else None,
                comparison.get("right_model")
                if comparison.get("right_model") in MODELS
                else None,
                *[
                    _num(comparison.get(key))
                    for key in ("mean_diff", "ci_low", "ci_high")
                ],
            ]
        ]
        if comparison
        else [],
    )
    reasons = {
        "gate_failed",
        "ci_excludes_zero",
        "cheaper_on_tie",
        "latency_unavailable_concurrency_mismatch",
        "complete_tie",
        "latency_on_cost_tie",
    }
    return "\n\n".join(
        [
            "정답 값 포함률: 값이 있는 같은 56문항 한정, 출처별 보고. 두 시스템 평균의 모델 간 짝 비교이며 H5 판정이 아니다.",
            _table(
                (
                    "모델 ID",
                    "시스템",
                    "전체 포함률",
                    "real",
                    "synthetic",
                    "unanswerable 거절률",
                ),
                rows,
            ),
            _table(
                ("모델 ID", "게이트 실행 수", "(a) 누설 수", "(b) 누설 수", "통과"),
                gates,
            ),
            _operations(data.get("operations"), data.get("concurrency")),
            paired,
            f"선택: {_cell(data.get('selection') if data.get('selection') in MODELS else None)}; 사유: {_cell(data.get('selection_reason') if data.get('selection_reason') in reasons else None)}",
        ]
    )


def _config(data: Mapping) -> str:
    if not data:
        return MISSING
    answer = _map(data.get("answer"))
    rows = [
        ["모델 ID", answer.get("model") if answer.get("model") in MODELS else None],
        ["모델 설정 해시", _hash(answer.get("settings_hash"))],
    ]
    rows.extend(
        [
            [key, _hash(data.get(key))]
            for key in ("config_hash", "m5_config_hash", "m6_dev_sha256", "code_commit")
        ]
    )
    rows.extend(
        [
            [f"프롬프트 {name} 해시", _hash(_at(data, "prompt_hashes", name))]
            for name in ("common", "agentic")
        ]
    )
    rows += [
        ["가드레일 해시", _hash(_at(data, "guardrails", "settings_hash"))],
        ["고정 시각", _timestamp(data.get("frozen_at"))],
    ]
    return _table(("고정 항목", "값"), rows)


def _timestamp(value: object) -> str | None:
    if isinstance(value, str):
        try:
            datetime.fromisoformat(value)
            return value
        except ValueError:
            pass
    return None


def _reason(value: object) -> str | None:
    """Keep operator notes but redact credentials, including bare MCP tokens."""
    if not isinstance(value, str):
        return None
    return re.sub(
        r"(?i)(?:api[_-]?key|token|secret)\s*[:=]\s*\S+|[A-Za-z0-9_-]{32,}",
        "[REDACTED]",
        _cell(value),
    )


def _metrics_table(result: Mapping) -> str:
    rows = []
    for system in SYSTEMS:
        metric = _map(_at(result, "deterministic_metrics", system))
        if metric:
            citation = _map(metric.get("citation_precision"))
            rows.append(
                [
                    system,
                    _rate(metric.get("answer_match")),
                    _num(citation.get("mean")),
                    _num(citation.get("denominator")),
                    _num(citation.get("excluded")),
                    _rate(metric.get("unanswerable_refusal")),
                    _rate(metric.get("permission_refusal")),
                ]
            )
    return _table(
        (
            "시스템",
            "정답 값 포함률(값 있는 문항 한정)",
            "인용 정밀도",
            "인용 분모",
            "인용 제외",
            "답 없음 거절률",
            "권한 거절률",
        ),
        rows,
    )


def _test(results: Sequence[Mapping], runs: Sequence[Mapping]) -> str:
    history = [
        [
            _num(row.get("seq")),
            _timestamp(row.get("ran_at")),
            row.get("status") if row.get("status") in {"completed", "failed"} else None,
            _reason(row.get("rerun_reason")) or "해당 없음",
            _hash(row.get("previous_result_sha256")),
            _hash(row.get("result_sha256")),
        ]
        for row in runs
    ]
    parts = [
        _table(
            ("시도", "시각", "상태", "사유", "이전 결과 해시", "이번 결과 해시"),
            history,
        )
    ]
    for result in sorted(results, key=lambda row: _num(row.get("seq")) or 0):
        parts.append(f"### test 시도 {_cell(_num(result.get('seq')))}")
        set_counts = _map(result.get("set_counts"))
        if set_counts:
            parts.append(
                _table(
                    ("세트", "문항 수", "실행 수"),
                    [
                        [
                            name,
                            _num(_at(set_counts, name, "question_count")),
                            _num(_at(set_counts, name, "execution_count")),
                        ]
                        for name in ("gold_test", "silver_test")
                        if name in set_counts
                    ],
                )
            )
        parts += [
            _metrics_table(result),
            _operations(result.get("operations"), result.get("concurrency")),
        ]
        silver = _map(result.get("silver_test"))
        if silver:
            parts += [
                "#### 실버 test 방향 확인(판정 아님)",
                _metrics_table(silver),
                _operations(silver.get("operations"), silver.get("concurrency")),
            ]
    parts += [
        "### H5\n\n"
        + _table(
            ("범위", "문항 수", "상태"),
            [
                ["13문항 판정", 13, "채점 대기(M7)"],
                ["9문항 보조(sql 제외, 판정 아님)", 9, MISSING],
                ["실버 test 방향 확인(29문항, M7 채점 후 채움)", 29, MISSING],
            ],
        )
    ]
    return "\n\n".join(parts)


def _dev_history(entries: Sequence[Mapping]) -> str:
    rows = [
        [
            _num(entry.get("attempt")),
            entry.get("kind") if entry.get("kind") in {"dev", "pilot"} else None,
            entry.get("status") if entry.get("status") in {"invalid"} else None,
            _timestamp(entry.get("invalidated_on")),
            _num(entry.get("runs_rows")),
            _reason(entry.get("reason")),
            _hash(entry.get("runs_sha256")),
            _hash(entry.get("result_sha256")),
        ]
        for entry in entries
    ]
    return _table(
        (
            "시도",
            "종류",
            "상태",
            "무효 처리일",
            "행 수",
            "사유",
            "runs SHA-256",
            "결과 SHA-256",
        ),
        rows,
    ) + (
        "\n\n무효 처리한 실행의 결과(예: 1차 모델 선택)는 선택에 쓰지 않음. "
        "규칙 14에 따라 dev 비교를 처음부터 재실행한다."
        if rows
        else ""
    )


def _costs(dry: Sequence[Mapping], ledger: Sequence[Mapping]) -> str:
    rows = [
        [
            _timestamp(row.get("ran_at")),
            _num(row.get("concurrency")),
            *[
                _num(_at(row, "totals", key))
                for key in (
                    "estimated_cost_usd",
                    "guarded_estimated_cost_usd",
                    "upper_cost_usd",
                    "guarded_upper_cost_usd",
                )
            ],
        ]
        for row in dry
    ]
    groups = defaultdict(list)
    for row in ledger:
        run = row.get("run_id")
        service = row.get("service")
        amount = _num(row.get("amount_usd"))
        if not isinstance(run, str) or not re.fullmatch(
            r"m6-(?:dev|pilot|test-\d{2,})", run
        ):
            continue
        if (
            service
            not in {
                f"openai.{model}.{suffix}"
                for model in MODELS
                for suffix in ("input", "cached_input", "cache_write", "output")
            }
            or amount is None
        ):
            continue
        groups[(run, service)].append(amount)
    actual = [
        [run, service, math.fsum(amounts)]
        for (run, service), amounts in sorted(groups.items())
    ]
    return "\n\n".join(
        [
            _table(
                (
                    "dry-run 시각",
                    "동시성",
                    "예상 USD",
                    "예상+여유 USD",
                    "상한 USD",
                    "상한+여유 USD",
                ),
                rows,
            ),
            "실제 비용(원장, 실행별·과금 항목별 누적; 재시도·재실행 포함):",
            _table(("실행", "과금 항목", "실제 USD"), actual),
            "OpenAI 콘솔 Usage와 첫 실행 뒤 대조 전까지 원장은 미검증(FP-037). 실행 전후 Usage·차이·대조 결과는 보존 영역에 기록한다.",
        ]
    )


def render_tables(root: Path, *, run_date: date | None = None) -> str:
    """Project aggregate/count/hash fields; never read local question/answer rows."""
    root = Path(root).resolve()

    def load(name):
        return _load_optional_json(root / MANIFEST / name)

    inputs, temporal, dev, config = (
        load(name)
        for name in (
            "m6_eval_inputs.json",
            "m6_temporal.json",
            "m6_dev.json",
            "m6_config.json",
        )
    )
    runs = _load_optional_jsonl(root / MANIFEST / "m6_test_runs.jsonl")
    results = [
        _load_optional_json(path)
        for path in sorted((root / MANIFEST / "m6_test_results").glob("run-*.json"))
    ]
    dev_history = _load_optional_jsonl(root / MANIFEST / "m6_dev_history.jsonl")
    dry = _load_optional_jsonl(root / MANIFEST / "m6_dry_runs.jsonl")
    ledger = _load_optional_jsonl(root / MANIFEST / "costs.jsonl")
    snapshots = _load_optional_json(root / "tests/contract/snapshots/index.json")
    contract = []
    for name, value in sorted(_map(snapshots.get("files")).items()):
        match = re.fullmatch(
            r"(search_docs|lookup_id|run_sql|get_document)\.(v\d+)\.json", name
        )
        if match:
            contract.append([match[1], match[2], _hash(value)])
    execution = _table(
        ("항목", "값"),
        [
            ["문서 고정 날짜", "2026-10-01"],
            ["보고서 생성일", run_date.isoformat() if run_date else None],
            ["생성 코드 커밋 해시", _hash(_git_commit(root))],
            ["dev 실행 커밋 해시", _hash(dev.get("code_commit"))],
        ],
    )
    return "\n\n".join(
        [
            _section("실행 정보", execution),
            _section("입력 요약", _inputs(inputs)),
            _section("is_current 효과", _temporal(temporal)),
            _section("dev 모델 비교", _dev(dev)),
            _section("dev 실행 이력", _dev_history(dev_history)),
            _section("고정 설정", _config(config)),
            _section("test 실행", _test(results, runs)),
            _section("예상 비용 대 실제 비용", _costs(dry, ledger)),
            _section(
                "MCP 계약",
                _table(("도구", "버전", "스냅샷 SHA-256"), contract)
                + "\n\n통합 테스트 결과는 자동 수집하지 않는다. 사용자가 `make up` 뒤 `make test-integration`을 실행하고 결과를 보존 영역에 기록한다.",
            ),
        ]
    )


def write_report(root: Path, *, run_date: date) -> Path:
    """Replace only the generated region, atomically, at the fixed report path."""
    if not isinstance(run_date, date):
        raise TypeError("run_date는 date여야 합니다.")
    root = Path(root).resolve()
    destination = root / REPORT_PATH
    original = (destination if destination.exists() else TEMPLATE_PATH).read_text(
        encoding="utf-8"
    )
    if (
        original.count(BEGIN) != 1
        or original.count(END) != 1
        or original.index(BEGIN) >= original.index(END)
    ):
        raise M6ReportError("generated 표지가 없거나 짝·순서가 맞지 않습니다.")
    generated = render_tables(root, run_date=run_date)
    _write_atomic(
        destination,
        f"{original[: original.index(BEGIN) + len(BEGIN)]}\n\n{generated}\n\n{original[original.index(END) :]}",
    )
    return destination


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--date", default=datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    )
    args = parser.parse_args(argv)
    try:
        path = write_report(args.root, run_date=date.fromisoformat(args.date))
    except (OSError, ValueError, TypeError):
        # Do not echo malformed source content or exception messages with secrets.
        print(
            "M6 결과 문서 생성 실패: 입력 형식 또는 생성 영역 표지를 확인하세요.",
            file=sys.stderr,
        )
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
