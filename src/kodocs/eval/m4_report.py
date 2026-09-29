"""Render the fixed M4 golden-set report from committed metadata."""

from __future__ import annotations

import argparse
import json
import math
import platform
import statistics
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from kodocs.eval.golden import QUESTION_TYPES, Question, load_questions
from kodocs.eval.golden_review import GOLD_PATH, SILVER_PATH, SUMMARY_PATH
from kodocs.eval.lexical import CONTAINMENT_CAP, LCS_CAP, MISMATCH_MAX

TEMPLATE_PATH = Path(__file__).parent / "templates" / "m4_golden.md"
BEGIN, END = "<!-- BEGIN generated -->", "<!-- END generated -->"
REPORT_PATH = Path("docs/results/2026-09-28-m4-golden-set.md")

_PLACEHOLDER = "(실행 후 기록)"
_QUERY_VALIDATION_PATH = Path("data/manifest/golden_query_validation.json")
_DRY_RUNS_PATH = Path("data/manifest/golden_dry_runs.jsonl")
_BATCHES_PATH = Path("data/manifest/anthropic_batches_golden.jsonl")
_COSTS_PATH = Path("data/manifest/costs.jsonl")
_KST = ZoneInfo("Asia/Seoul")


class M4ReportError(RuntimeError):
    """Report inputs or generated-region markers are invalid."""


def render_tables(root: Path) -> str:
    """Render the generated region, using placeholders for missing inputs."""
    return _render_tables(Path(root).resolve(), run_date=None)


def write_report(root: Path, *, run_date: date) -> Path:
    """Create the one fixed M4 report or replace only its generated region."""
    if not isinstance(run_date, date):
        raise TypeError("run_date는 date여야 합니다.")
    root = Path(root).resolve()
    destination = root / REPORT_PATH
    source = destination if destination.exists() else TEMPLATE_PATH
    try:
        original = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise M4ReportError(f"결과 문서 틀을 읽을 수 없습니다: {source}") from exc
    start, finish = _marker_indexes(original)
    generated = _render_tables(root, run_date=run_date)
    updated = f"{original[: start + len(BEGIN)]}\n\n{generated}\n\n{original[finish:]}"
    _write_atomic(destination, updated)
    return destination


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the offline, fixed-path M4 report generator."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--date",
        default=datetime.now(_KST).date().isoformat(),
        help="결과 문서에 기록할 실행일(YYYY-MM-DD, 기본값: Asia/Seoul 오늘)",
    )
    args = parser.parse_args(argv)
    try:
        run_date = _parse_date(args.date)
        path = write_report(args.root, run_date=run_date)
    except (
        M4ReportError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"M4 골드셋 결과표 생성 실패: {exc}", file=sys.stderr)
        return 1
    print(path)
    return 0


def _render_tables(root: Path, *, run_date: date | None) -> str:
    gold = _load_optional_questions(root / GOLD_PATH)
    silver = _load_optional_questions(root / SILVER_PATH)
    summary = _load_optional_json(root / SUMMARY_PATH)
    lexical = _load_optional_json(root / _QUERY_VALIDATION_PATH)
    dry_runs = _load_optional_jsonl(root / _DRY_RUNS_PATH)
    batches = _load_optional_jsonl(root / _BATCHES_PATH)
    costs = _load_optional_jsonl(root / _COSTS_PATH)
    questions = [*gold, *silver]
    return "\n\n".join(
        (
            _execution_section(root, run_date),
            _distribution_section(questions),
            _temporal_section(questions),
            _qrels_section(questions),
            _lexical_section(lexical),
            _review_section(summary),
            _split_section(summary),
            _cost_section(dry_runs, batches, costs),
        )
    )


def _execution_section(root: Path, run_date: date | None) -> str:
    return "\n".join(
        (
            "## 실행 정보",
            "",
            _table(
                ("항목", "값"),
                (
                    ("실행일(Asia/Seoul)", run_date.isoformat() if run_date else None),
                    ("커밋", _git_commit(root)),
                    ("측정 환경", _measurement_environment()),
                    ("고정 조건", "ADR-025 유형·출처·split 및 ADR-026 사전 등록"),
                ),
            ),
        )
    )


def _distribution_section(questions: Sequence[Question]) -> str:
    counts = Counter(
        (question.type, question.source, question.split) for question in questions
    )
    rows: list[tuple[object, ...]] = []
    for question_type in QUESTION_TYPES:
        for source in ("real", "synthetic"):
            dev = counts[(question_type, source, "dev")]
            test = counts[(question_type, source, "test")]
            if dev or test:
                rows.append((question_type, source, dev, test, dev + test))
    if not rows:
        rows = [(None, None, None, None, None)]
    return "\n".join(
        (
            "## 유형 × 출처 × split",
            "",
            _table(("유형", "출처", "dev", "test", "계"), rows),
        )
    )


def _temporal_section(questions: Sequence[Question]) -> str:
    rows = []
    for tag in ("temporal:revision", "temporal:as_of_event"):
        dev = sum(
            tag in question.tags and question.split == "dev" for question in questions
        )
        test = sum(
            tag in question.tags and question.split == "test" for question in questions
        )
        rows.append((tag, dev, test, dev + test))
    if not questions:
        rows = [
            (tag, None, None, None)
            for tag in ("temporal:revision", "temporal:as_of_event")
        ]
    return "\n".join(
        (
            "## 시점 태그",
            "",
            _table(("태그", "dev", "test", "계"), rows),
        )
    )


def _qrels_section(questions: Sequence[Question]) -> str:
    rel_two = [
        len({qrel.doc_id for qrel in question.qrels if qrel.rel == 2})
        for question in questions
    ]
    unmarked = [len(question.unmarked_candidates) for question in questions]
    rows = (
        ("문항당 관련도 2 문서 수", *_stats(rel_two)),
        ("문항당 미표시 후보 수", *_stats(unmarked)),
    )
    return "\n".join(
        (
            "## qrels 통계",
            "",
            _table(("지표", "n", "최소", "평균", "최대"), rows),
        )
    )


def _lexical_section(lexical: Mapping[str, object]) -> str:
    overlap = _mapping(lexical.get("overlap"))
    all_values = _mapping(overlap.get("all"))
    containment = _mapping(all_values.get("containment"))
    lcs = _mapping(all_values.get("lcs"))
    caps = _mapping(lexical.get("caps"))
    return "\n".join(
        (
            "## 어휘 겹침 분포",
            "",
            _table(
                ("지표", "n", "최소", "p25", "p50", "p75", "p90", "최대", "확정 상한"),
                (
                    (
                        "글자 바이그램 포함률",
                        containment.get("n"),
                        containment.get("min"),
                        containment.get("p25"),
                        containment.get("p50"),
                        containment.get("p75"),
                        containment.get("p90"),
                        containment.get("max"),
                        caps.get("containment", CONTAINMENT_CAP),
                    ),
                    (
                        "최장 공통 부분 문자열",
                        lcs.get("n"),
                        lcs.get("min"),
                        lcs.get("p25"),
                        lcs.get("p50"),
                        lcs.get("p75"),
                        lcs.get("p90"),
                        lcs.get("max"),
                        caps.get("lcs", LCS_CAP),
                    ),
                    (
                        "lexical_mismatch 기준",
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        caps.get("mismatch_max", MISMATCH_MAX),
                    ),
                ),
            ),
        )
    )


def _review_section(summary: Mapping[str, object]) -> str:
    gold = _mapping(summary.get("gold"))
    silver = _mapping(summary.get("silver_sample"))
    attention = _mapping(summary.get("attention_checks"))
    disagree = _mapping(gold.get("disagree_by_item"))
    wilson = silver.get("wilson95")
    wilson_text = (
        f"[{wilson[0]}, {wilson[1]}]"
        if isinstance(wilson, list) and len(wilson) == 2
        else None
    )
    return "\n".join(
        (
            "## 검수 결과",
            "",
            _table(
                ("항목", "값"),
                (
                    (
                        "골드 검수/확정",
                        _pair(gold.get("reviewed"), gold.get("confirmed")),
                    ),
                    ("골드 수락률", _percent(gold.get("acceptance_rate"))),
                    ("불일치 q1_answerable", disagree.get("q1_answerable")),
                    ("불일치 q2_qrels_complete", disagree.get("q2_qrels_complete")),
                    ("불일치 q3_labels_correct", disagree.get("q3_labels_correct")),
                    (
                        "실버 표본 n/부정",
                        _pair(silver.get("n"), silver.get("negative")),
                    ),
                    ("실버 표본 오류율", _percent(silver.get("error_rate"))),
                    ("실버 Wilson 95%", wilson_text),
                    (
                        "점검용 더미 n/적발",
                        _pair(attention.get("n"), attention.get("detected")),
                    ),
                ),
            ),
        )
    )


def _split_section(summary: Mapping[str, object]) -> str:
    split = _mapping(summary.get("split"))
    rows = []
    for question_set in ("gold", "silver"):
        report = _mapping(split.get(question_set))
        groups = _mapping(report.get("groups"))
        cross = _mapping(report.get("cross_split_docs"))
        rows.append(
            (
                question_set,
                groups.get("n"),
                groups.get("max_size"),
                _quota_diff_text(report.get("quota_diff")),
                cross.get("n"),
                report.get("retrieval_test_n"),
                report.get("h5_bundle_test_n"),
                _h7_text(report.get("h7")),
            )
        )
    return "\n".join(
        (
            "## split 보고",
            "",
            _table(
                (
                    "세트",
                    "그룹",
                    "최대 그룹",
                    "할당 차이",
                    "누수 문서",
                    "retrieval_test_n",
                    "h5_bundle_test_n",
                    "h7",
                ),
                rows,
            ),
        )
    )


def _cost_section(
    dry_runs: Sequence[Mapping[str, object]],
    batches: Sequence[Mapping[str, object]],
    costs: Sequence[Mapping[str, object]],
) -> str:
    dry = dry_runs[-1] if dry_runs else {}
    batch_ids = {
        str(row["batch_id"]) for row in batches if isinstance(row.get("batch_id"), str)
    }
    actual_rows = [row for row in costs if row.get("run_id") in batch_ids]
    actual = sum(
        float(row["amount_usd"])
        for row in actual_rows
        if _is_number(row.get("amount_usd"))
    )
    expected = dry.get("expected_cost_usd")
    delta = actual - float(expected) if actual_rows and _is_number(expected) else None
    return "\n".join(
        (
            "## 질의 초안 Batch 비용",
            "",
            _table(
                ("항목", "값"),
                (
                    ("dry-run 요청 수", dry.get("n_requests")),
                    ("dry-run 추정 입력 토큰", dry.get("estimated_input_tokens")),
                    ("dry-run 예상 비용 USD", _decimal(expected)),
                    ("원장 실제 비용 USD", _decimal(actual) if actual_rows else None),
                    ("실제 - 예상 USD", _decimal(delta)),
                    ("콘솔 대조", None),
                ),
            ),
        )
    )


def _stats(values: Sequence[int]) -> tuple[object, object, object, object]:
    if not values:
        return (None, None, None, None)
    return (len(values), min(values), round(statistics.fmean(values), 4), max(values))


def _quota_diff_text(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    parts = []
    for split_name in ("test", "dev"):
        by_type = value.get(split_name)
        if not isinstance(by_type, Mapping):
            continue
        for question_type, difference in sorted(by_type.items()):
            if isinstance(difference, int) and difference:
                parts.append(f"{split_name}.{question_type}={difference:+d}")
    return ", ".join(parts) if parts else "0"


def _h7_text(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    synthetic = value.get("synthetic_n")
    real = value.get("real_n")
    if synthetic is None and real is None:
        return None
    return f"synthetic={synthetic}, real={real}"


def _pair(left: object, right: object) -> str | None:
    return None if left is None and right is None else f"{left}/{right}"


def _measurement_environment() -> str:
    processor = platform.processor() or "unknown"
    return (
        f"{platform.platform()} / machine={platform.machine()} / processor={processor}"
    )


def _git_commit(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    rendered = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    rendered.extend(
        "| " + " | ".join(_markdown(_display(value)) for value in row) + " |"
        for row in rows
    )
    return "\n".join(rendered)


def _display(value: object) -> str:
    return _PLACEHOLDER if value is None or value == "" else str(value)


def _markdown(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _decimal(value: object) -> str | None:
    return f"{float(value):.4f}" if _is_number(value) else None


def _percent(value: object) -> str | None:
    return f"{float(value) * 100:.1f}%" if _is_number(value) else None


def _load_optional_questions(path: Path) -> list[Question]:
    return load_questions(path) if path.is_file() else []


def _load_optional_json(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON 객체가 아닙니다: {path}")
    return value


def _load_optional_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    rows: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise TypeError(f"JSONL 객체가 아닙니다: {path}:{line_number}")
            rows.append(value)
    return rows


def _parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("실행일은 YYYY-MM-DD 형식이어야 합니다.") from exc
    if parsed.isoformat() != value:
        raise ValueError("실행일은 YYYY-MM-DD 형식이어야 합니다.")
    return parsed


def _marker_indexes(text: str) -> tuple[int, int]:
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise M4ReportError("generated 표지가 없거나 짝이 맞지 않습니다.")
    start, finish = text.index(BEGIN), text.index(END)
    if start >= finish:
        raise M4ReportError("generated 표지 순서가 잘못되었습니다.")
    return start, finish


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as destination:
            destination.write(text)
            temporary = Path(destination.name)
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":  # pragma: no cover - CLI acceptance check
    raise SystemExit(main())
