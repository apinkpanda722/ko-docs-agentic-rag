"""Render the M3 synthetic-corpus report from committed metadata only."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import platform
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TEMPLATE_PATH = Path(__file__).parent / "templates" / "m3_synthetic.md"
BEGIN, END = "<!-- BEGIN generated -->", "<!-- END generated -->"

_PLACEHOLDER = "(실행 후 기록)"
_METRICS_PATH = Path("data/manifest/synth_metrics.json")
_VALIDATION_PATH = Path("data/manifest/synth_validation.json")
_DB_PATH = Path("data/manifest/synth_db.json")
_BATCHES_PATH = Path("data/manifest/anthropic_batches.jsonl")
_COSTS_PATH = Path("data/manifest/costs.jsonl")
_PLAN_PATH = Path("data/synthetic/plan.jsonl")
_CANARIES_PATH = Path("data/synthetic/canaries.json")
_INJECTIONS_PATH = Path("data/synthetic/injections.json")
_REGULATION_INDEX_PATH = Path("data/manifest/regulation_refs.json")
_STYLE_LABELS = {
    "formal": "합니다·했습니다류",
    "plain": "한다·했다류",
    "nominal": "함·음·명사형",
    "other": "기타",
}


class SynthReportError(RuntimeError):
    """The report inputs or generated-region markers are invalid."""


def render_tables(root: Path) -> str:
    """Render the generated region without reading synthetic document bodies."""
    return _render_tables(Path(root).resolve(), run_date=None)


def write_report(
    root: Path,
    *,
    run_date: date,
    out_path: Path | None = None,
) -> Path:
    """Create a dated report or replace only its generated region."""
    if not isinstance(run_date, date):
        raise TypeError("run_date는 date여야 합니다.")
    root = Path(root).resolve()
    destination = (
        out_path or root / "docs/results" / f"{run_date.isoformat()}-m3-synthetic.md"
    )
    source_path = destination if destination.exists() else TEMPLATE_PATH
    try:
        original = source_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SynthReportError(
            f"결과 문서 틀을 읽을 수 없습니다: {source_path}"
        ) from exc
    start, finish = _marker_indexes(original)
    generated = _render_tables(root, run_date=run_date)
    updated = f"{original[: start + len(BEGIN)]}\n\n{generated}\n\n{original[finish:]}"
    _write_atomic(destination, updated)
    return destination


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the offline M3 report generator."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--date",
        default=datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat(),
        help="결과 파일 실행일(YYYY-MM-DD, 기본값: Asia/Seoul 오늘)",
    )
    parser.add_argument("--out", type=Path, help="기본 결과 경로 대신 쓸 파일")
    args = parser.parse_args(argv)
    try:
        parsed_date = _parse_date(args.date)
        path = write_report(args.root, run_date=parsed_date, out_path=args.out)
    except (
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        SynthReportError,
    ) as exc:
        print(f"합성 코퍼스 결과표 생성 실패: {exc}", file=sys.stderr)
        return 1
    print(path)
    return 0


def _render_tables(root: Path, *, run_date: date | None) -> str:
    metrics = _load_optional_json(root / _METRICS_PATH)
    validation = _load_optional_json(root / _VALIDATION_PATH)
    synth_db = _load_optional_json(root / _DB_PATH)
    batches = _load_optional_jsonl(root / _BATCHES_PATH)
    costs = _load_optional_jsonl(root / _COSTS_PATH)
    plans = _load_optional_jsonl(root / _PLAN_PATH)
    canaries = _load_optional_json(root / _CANARIES_PATH)
    injections = _load_optional_json(root / _INJECTIONS_PATH)
    latest_batches = _latest_batches(batches)
    sections = [
        _execution_section(root, run_date, metrics, synth_db, latest_batches),
        _fixed_conditions_section(metrics, plans),
        _generation_cost_section(latest_batches, costs),
        _validation_section(validation, canaries, injections),
        _quality_section(metrics),
        _database_section(synth_db),
    ]
    return "\n\n".join(sections)


def _execution_section(
    root: Path,
    run_date: date | None,
    metrics: Mapping[str, object],
    synth_db: Mapping[str, object],
    batches: Sequence[Mapping[str, object]],
) -> str:
    provenance = _mapping(metrics.get("provenance"))
    models = _string_list(provenance.get("models"))
    for batch in batches:
        generation = _mapping(batch.get("generation"))
        model = generation.get("model")
        if isinstance(model, str) and model not in models:
            models.append(model)
    generation_records = _generation_records(provenance, batches)
    template_hashes = _string_list(provenance.get("template_hashes"))
    seed_hashes = _mapping(provenance.get("seed_files_sha256"))
    if not seed_hashes:
        seed_hashes = _mapping(synth_db.get("seed_files_sha256"))
    regulation_hashes = _string_list(provenance.get("regulation_index_sha256"))
    if not regulation_hashes:
        regulation_hash = _file_hash(root / _REGULATION_INDEX_PATH)
        if regulation_hash is not None:
            regulation_hashes = [regulation_hash]
    rows: list[list[object]] = [
        ["실행일", run_date.isoformat() if run_date is not None else None],
        [
            "커밋 해시 (`git rev-parse --short HEAD`)",
            _git_commit(root) or "(알 수 없음)",
        ],
        ["생성 모델·버전", ", ".join(sorted(models)) or None],
        ["generation 기록", "<br>".join(generation_records) or None],
        ["프롬프트 템플릿 해시", "<br>".join(sorted(template_hashes)) or None],
        ["시드 파일 해시", _hash_mapping(seed_hashes)],
        ["규정 색인 해시", "<br>".join(sorted(regulation_hashes)) or None],
        ["SQLite `content_hash`", synth_db.get("content_hash")],
        ["Python 버전", platform.python_version()],
        ["anthropic SDK 버전", _package_version("anthropic")],
        ["측정 환경", _measurement_environment()],
    ]
    return "\n".join(
        [
            "## 1. 실행 정보",
            "",
            _table(["항목", "값"], rows),
        ]
    )


def _fixed_conditions_section(
    metrics: Mapping[str, object], plans: Sequence[Mapping[str, object]]
) -> str:
    by_type = Counter(
        str(plan["doc_type"]) for plan in plans if isinstance(plan.get("doc_type"), str)
    )
    by_access = Counter(
        str(access["level"])
        for plan in plans
        if isinstance((access := plan.get("access")), Mapping)
        and isinstance(access.get("level"), str)
    )
    provenance = _mapping(metrics.get("provenance"))
    versions = provenance.get("validator_versions")
    validator_versions = (
        ", ".join(str(value) for value in versions)
        if isinstance(versions, list) and versions
        else None
    )
    plan_summary = None
    if plans:
        plan_summary = (
            f"계획 {len(plans)}건 / 유형 {_counter_text(by_type)} / "
            f"권한 {_counter_text(by_access)}"
        )
    rows = [
        ["문서 계획", plan_summary],
        ["인라인 사실 표시", "`⟦F:fact_id⟧…⟦/F⟧` 후 검증기가 span으로 변환"],
        ["검증 규칙 버전", validator_versions],
        ["재생성 상한", "최대 시도 3회"],
        ["생성 방식", "Anthropic Message Batch 사용"],
        ["품질 지표 대상", "검증 통과 문서만 사용(분자·분모 동일 집합)"],
    ]
    return "\n".join(["## 2. 고정 조건", "", _table(["조건", "고정값"], rows)])


def _generation_cost_section(
    batches: Sequence[Mapping[str, object]], costs: Sequence[Mapping[str, object]]
) -> str:
    batch_rows: list[list[object]] = []
    estimated_inputs: list[int] = []
    actual_input = 0
    for batch in batches:
        result_counts = _mapping(batch.get("result_counts"))
        usage = _mapping(batch.get("usage_totals"))
        estimate = _estimated_input_tokens(batch)
        if estimate is not None:
            estimated_inputs.append(estimate)
        actual_input += _integer(usage.get("input_tokens")) or 0
        batch_rows.append(
            [
                batch.get("batch_id"),
                batch.get("state"),
                batch.get("n_requests"),
                _mapping_text(result_counts),
                _usage_text(usage),
            ]
        )
    if not batch_rows:
        batch_rows.append([None, None, None, None, None])
    anthropic_amounts = [
        float(amount)
        for record in costs
        if isinstance(record.get("service"), str)
        and str(record["service"]).startswith("anthropic.")
        and _is_number(amount := record.get("amount_usd"))
    ]
    ledger_total = math.fsum(anthropic_amounts) if anthropic_amounts else None
    estimated_total = sum(estimated_inputs) if estimated_inputs else None
    ratio = (
        actual_input / estimated_total
        if estimated_total is not None and estimated_total > 0
        else None
    )
    cost_rows = [
        ["Anthropic 원장 합계(VAT 포함, USD)", _usd(ledger_total)],
        [
            "콘솔 대조용 원장 합계(VAT 제외 환산, USD)",
            _usd(ledger_total / 1.1) if ledger_total is not None else None,
        ],
        ["dry-run 추정 입력 토큰", estimated_total],
        ["실제 입력 토큰", actual_input if batches else None],
        ["dry-run 추정 대비 실제 입력 토큰 비율", _percent(ratio)],
        [
            "파일럿 보정 근거",
            (
                "파일럿 actual/dry-run 비율로 `chars_per_token_estimate` 보정 여부를 판단"
                if ratio is not None
                else None
            ),
        ],
    ]
    return "\n".join(
        [
            "## 3. 생성·비용",
            "",
            "### 배치별 제출·결과·usage",
            "",
            _table(
                ["batch_id", "상태", "제출 수", "결과 유형 수", "usage 합"],
                batch_rows,
            ),
            "",
            "### 원장·dry-run 대조",
            "",
            _table(["항목", "값"], cost_rows),
        ]
    )


def _validation_section(
    validation: Mapping[str, object],
    canaries: Mapping[str, object],
    injections: Mapping[str, object],
) -> str:
    generated = _integer(validation.get("generated"))
    has_generated_documents = generated is not None and generated > 0
    rows = [
        ["계획", validation.get("planned")],
        ["통과", validation.get("passed") if has_generated_documents else None],
        ["실패", validation.get("failed") if has_generated_documents else None],
        [
            "미생성",
            validation.get("not_generated") if has_generated_documents else None,
        ],
        ["소진", validation.get("exhausted") if has_generated_documents else None],
        [
            "통과율(통과/생성)",
            _validation_pass_rate(validation) if has_generated_documents else None,
        ],
        [
            "실패 코드 분포",
            (
                _mapping_text(_mapping(validation.get("by_code"))) or "없음"
                if has_generated_documents
                else None
            ),
        ],
    ]
    canary = _mapping(validation.get("canary"))
    injection = _mapping(validation.get("injections"))
    expected_canaries = canaries.get("count", canary.get("expected"))
    expected_injections = injections.get("count", injection.get("expected"))
    registry_rows = [
        [
            "카나리아",
            expected_canaries,
            canary.get("present") if has_generated_documents else None,
            canary.get("leaks") if has_generated_documents else None,
        ],
        [
            "인젝션",
            expected_injections,
            injection.get("inserted") if has_generated_documents else None,
            "-",
        ],
    ]
    return "\n".join(
        [
            "## 4. 검증",
            "",
            _table(["항목", "값"], rows),
            "",
            "### 카나리아 30·인젝션 12 확인",
            "",
            _table(["레지스트리", "예상", "확인", "누설"], registry_rows),
        ]
    )


def _quality_section(metrics: Mapping[str, object]) -> str:
    duplicate = _mapping(metrics.get("duplicate"))
    repeated = _mapping(metrics.get("repeated_sentences"))
    length = _mapping(_mapping(metrics.get("length")).get("by_type"))
    lexical = _mapping(_mapping(metrics.get("lexical_diversity")).get("by_type"))
    style = _mapping(metrics.get("style"))
    distribution = _mapping(metrics.get("distribution"))
    facts = _mapping(metrics.get("fact_coverage"))
    sections = [
        "## 5. 품질 지표",
        "",
        "모든 비율 지표는 검증 통과 문서 집합 안에서 분자와 분모를 계산한다.",
        "",
        "### 중복 — 글자 5-gram Jaccard",
        "",
        _duplicate_table(duplicate),
        "",
        _near_duplicate_table(duplicate),
        "",
        "### 반복 문장",
        "",
        _repeated_table(repeated),
        "",
        "### 길이",
        "",
        _length_table(length),
        "",
        "### distinct-2",
        "",
        _lexical_table(lexical),
        "",
        "### 문체",
        "",
        _style_table(style),
        "",
        "### 분포",
        "",
        _distribution_table(distribution),
        "",
        "### 사실 커버리지",
        "",
        _facts_table(facts),
    ]
    return "\n".join(sections)


def _duplicate_table(duplicate: Mapping[str, object]) -> str:
    labels = (
        ("전체", "overall"),
        ("유형 안", "within_type"),
        ("유형 간", "between_type"),
    )
    rows = []
    for label, key in labels:
        summary = _mapping(duplicate.get(key))
        rows.append(
            [
                label,
                _decimal(summary.get("max")),
                _decimal(summary.get("p95")),
                _decimal(summary.get("mean")),
            ]
        )
    return _table(["범위", "최댓값", "p95", "평균"], rows)


def _near_duplicate_table(duplicate: Mapping[str, object]) -> str:
    rows = []
    near = duplicate.get("near_duplicates")
    if isinstance(near, list):
        for pair in near[:20]:
            if isinstance(pair, Mapping):
                rows.append(
                    [
                        pair.get("doc_id_a"),
                        pair.get("doc_id_b"),
                        pair.get("doc_type_a"),
                        pair.get("doc_type_b"),
                        _decimal(pair.get("jaccard")),
                    ]
                )
    if not rows and duplicate:
        rows = [["없음", "-", "-", "-", "-"]]
    elif not rows:
        rows = [[None, None, None, None, None]]
    return "\n".join(
        [
            "#### 근접 중복 상위 20쌍(임계값 이상)",
            "",
            _table(["doc_id A", "doc_id B", "유형 A", "유형 B", "Jaccard"], rows),
        ]
    )


def _repeated_table(repeated: Mapping[str, object]) -> str:
    rows: list[list[object]] = [
        ["3개 이상 문서의 동일 문장 수", repeated.get("sentences_in_3_plus_docs")]
    ]
    top = repeated.get("top")
    if isinstance(top, list):
        rows.extend(
            [str(item.get("sentence_prefix"))[:40], item.get("document_count")]
            for item in top[:10]
            if isinstance(item, Mapping)
        )
    return _table(["문장 앞 40자", "문서 수"], rows)


def _length_table(length: Mapping[str, object]) -> str:
    rows = [
        [
            doc_type,
            values.get("documents"),
            _decimal(values.get("mean_chars")),
            _decimal(values.get("stddev_chars")),
            _decimal(values.get("coefficient_of_variation")),
            values.get("min_chars"),
            values.get("max_chars"),
            _decimal(values.get("target_mean_chars")),
            _percent(values.get("target_ratio")),
        ]
        for doc_type, raw in sorted(length.items())
        if (values := _mapping(raw))
    ]
    if not rows:
        rows = [[None] * 9]
    return _table(
        [
            "유형",
            "문서",
            "평균",
            "표준편차",
            "변동계수",
            "최소",
            "최대",
            "계획 목표",
            "목표 대비",
        ],
        rows,
    )


def _lexical_table(lexical: Mapping[str, object]) -> str:
    rows = [
        [
            doc_type,
            _decimal(values.get("distinct_2")),
            values.get("unique_bigrams"),
            values.get("total_bigrams"),
        ]
        for doc_type, raw in sorted(lexical.items())
        if (values := _mapping(raw))
    ]
    if not rows:
        rows = [[None] * 4]
    return _table(["유형", "distinct-2", "고유 bigram", "전체 bigram"], rows)


def _style_table(style: Mapping[str, object]) -> str:
    rows: list[list[object]] = []
    overall = _mapping(style.get("overall"))
    if overall:
        rows.extend(_style_rows("전체", overall))
    by_type = _mapping(style.get("by_type"))
    for doc_type, raw in sorted(by_type.items()):
        values = _mapping(raw)
        rows.extend(_style_rows(doc_type, values))
    if not rows:
        rows = [[None] * 4]
    return _table(["범위", "종결 유형", "문장 수", "비율"], rows)


def _style_rows(scope: str, values: Mapping[str, object]) -> list[list[object]]:
    counts, ratios = _mapping(values.get("counts")), _mapping(values.get("ratios"))
    return [
        [scope, label, counts.get(key), _percent(ratios.get(key))]
        for key, label in _STYLE_LABELS.items()
    ]


def _distribution_table(distribution: Mapping[str, object]) -> str:
    rows: list[list[object]] = []
    for dimension, label in (
        ("by_type", "유형"),
        ("by_access", "권한"),
        ("by_team", "팀"),
    ):
        values = _mapping(distribution.get(dimension))
        for category, raw in sorted(values.items()):
            counts = _mapping(raw)
            rows.append([label, category, counts.get("planned"), counts.get("passed")])
    rows.extend(
        [
            ["특수", "카나리아 포함", None, distribution.get("canary_documents")],
            ["특수", "인젝션 포함", None, distribution.get("injection_documents")],
        ]
    )
    return _table(["차원", "값", "계획", "통과"], rows)


def _facts_table(facts: Mapping[str, object]) -> str:
    distribution = _mapping(facts.get("documents_per_fact_distribution"))
    rows = [
        ["normal 사실 수", facts.get("normal_facts_total")],
        ["커버된 사실 수", facts.get("covered_facts")],
        ["커버리지", _percent(facts.get("coverage_rate"))],
        ["사실당 등장 문서 수 분포", _mapping_text(distribution)],
        ["2개 이상 문서에 등장한 사실 수", facts.get("facts_in_2_plus_documents")],
    ]
    return _table(["항목", "값"], rows)


def _database_section(synth_db: Mapping[str, object]) -> str:
    row_counts = _mapping(synth_db.get("row_counts"))
    access_counts = _mapping(synth_db.get("access_counts"))
    rows: list[list[object]] = []
    for table in ("tickets", "approvals"):
        counts = _mapping(access_counts.get(table))
        rows.append(
            [
                table,
                row_counts.get(table),
                counts.get("public"),
                counts.get("team"),
                counts.get("restricted"),
            ]
        )
    return "\n".join(
        [
            "## 6. 티켓·결재 DB",
            "",
            _table(["테이블", "행 수", "전사", "팀", "제한"], rows),
        ]
    )


def _generation_records(
    provenance: Mapping[str, object], batches: Sequence[Mapping[str, object]]
) -> list[str]:
    records: list[Mapping[str, object]] = []
    raw_records = provenance.get("generation_records")
    if isinstance(raw_records, list):
        records.extend(value for value in raw_records if isinstance(value, Mapping))
    records.extend(
        generation
        for batch in batches
        if (generation := _mapping(batch.get("generation")))
    )
    rendered = sorted({_generation_text(record) for record in records})
    return [value for value in rendered if value]


def _generation_text(record: Mapping[str, object]) -> str:
    sampling = record.get("sampling")
    temperature = record.get("temperature")
    if temperature is None:
        reason = (
            str(sampling) if isinstance(sampling, str) and sampling else "사유 미기록"
        )
        temperature_text = f"null (모델 미지원: {reason})"
    else:
        temperature_text = str(temperature)
    effort = "null" if record.get("effort") is None else str(record.get("effort"))
    values = [
        f"model={_display(record.get('model'))}",
        f"temperature={temperature_text}",
        f"thinking={_display(record.get('thinking'))}",
        f"effort={effort}",
        f"max_tokens={_display(record.get('max_tokens'))}",
    ]
    return ", ".join(values)


def _latest_batches(
    records: Sequence[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    latest: dict[str, Mapping[str, object]] = {}
    order: list[str] = []
    for record in records:
        submission_id = record.get("submission_id")
        if not isinstance(submission_id, str):
            continue
        if submission_id not in latest:
            order.append(submission_id)
        latest[submission_id] = record
    return [latest[submission_id] for submission_id in order]


def _estimated_input_tokens(batch: Mapping[str, object]) -> int | None:
    for key in ("estimated_input_tokens", "dry_run_estimated_input_tokens"):
        value = _integer(batch.get(key))
        if value is not None:
            return value
    estimate = _mapping(batch.get("estimate"))
    return _integer(estimate.get("input_tokens"))


def _validation_pass_rate(validation: Mapping[str, object]) -> str | None:
    passed = _integer(validation.get("passed"))
    generated = _integer(validation.get("generated"))
    if passed is None or generated is None or generated == 0:
        return None
    return _percent(passed / generated)


def _measurement_environment() -> str:
    processor = platform.processor() or "unknown"
    return (
        f"{platform.platform()} / machine={platform.machine()} / processor={processor}"
    )


def _package_version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


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


def _file_hash(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash_mapping(values: Mapping[str, object]) -> str | None:
    rows = [
        f"{name}={value}"
        for name, value in sorted(values.items())
        if isinstance(name, str) and isinstance(value, str)
    ]
    return "<br>".join(rows) or None


def _counter_text(values: Mapping[str, int]) -> str:
    return ", ".join(f"{key}={values[key]}" for key in sorted(values))


def _mapping_text(values: Mapping[str, object]) -> str | None:
    if not values:
        return None
    return ", ".join(f"{key}={values[key]}" for key in sorted(values))


def _usage_text(values: Mapping[str, object]) -> str | None:
    if not values:
        return None
    labels = (
        ("input_tokens", "input"),
        ("output_tokens", "output"),
        ("cache_creation_input_tokens", "cache_create"),
        ("cache_read_input_tokens", "cache_read"),
    )
    return ", ".join(f"{label}={values.get(key, 0)}" for key, label in labels)


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


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _string_list(value: object) -> list[str]:
    return (
        [str(item) for item in value if isinstance(item, str)]
        if isinstance(value, list)
        else []
    )


def _integer(value: object) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _decimal(value: object) -> str | None:
    return f"{float(value):.4f}" if _is_number(value) else None


def _usd(value: object) -> str | None:
    return f"{float(value):.4f}" if _is_number(value) else None


def _percent(value: object) -> str | None:
    return f"{float(value) * 100:.1f}%" if _is_number(value) else None


def _display(value: object) -> str:
    return _PLACEHOLDER if value is None or value == "" else str(value)


def _markdown(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


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
    records: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise TypeError(f"JSONL 객체가 아닙니다: {path}:{line_number}")
            records.append(value)
    return records


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
        raise SynthReportError("generated 표지가 없거나 짝이 맞지 않습니다.")
    start, finish = text.index(BEGIN), text.index(END)
    if start >= finish:
        raise SynthReportError("generated 표지 순서가 잘못되었습니다.")
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
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


if __name__ == "__main__":  # pragma: no cover - CLI acceptance check
    raise SystemExit(main())
