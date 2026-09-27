"""Render the M2 parsing report from committed, text-free manifests only."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from kodocs.eval.direct_compare import compare as compare_direct
from kodocs.ingest.manifest import now_iso, sha256_file

TEMPLATE_PATH = Path(__file__).parent / "templates" / "m2_parsing.md"
BEGIN, END = "<!-- BEGIN generated -->", "<!-- END generated -->"

_PARSERS: tuple[str, ...] = ("pypdf", "docling", "upstage")
_PLACEHOLDER = "(실행 후 기록)"
_MANIFEST_NAMES: tuple[str, ...] = (
    "parse_targets.json",
    "parse_runs.jsonl",
    "parse_scores.json",
    "table_golden.json",
    "table_scores.json",
    "costs.jsonl",
)
_UPSTAGE_SERVICE = "upstage.document-parse.standard"


class ParsingReportError(RuntimeError):
    """The report inputs or generated-region markers are invalid."""


def render_tables(root: Path) -> str:
    """Read text-free manifests and render all generated Markdown tables."""
    manifest_dir = root / "data/manifest"
    manifest_paths = {name: manifest_dir / name for name in _MANIFEST_NAMES}
    if not any(path.is_file() for path in manifest_paths.values()):
        return _PLACEHOLDER

    runs = _load_jsonl(manifest_paths["parse_runs.jsonl"])
    scores = _load_json(manifest_paths["parse_scores.json"])
    table_golden = _load_json(manifest_paths["table_golden.json"])
    table_scores = _load_json(manifest_paths["table_scores.json"])
    costs = _load_jsonl(manifest_paths["costs.jsonl"])

    sections = [
        _measurement_table(root, runs),
        _ceiling_table(scores),
        _excluded_table(scores),
        _structure_table(scores, denominator="all_targets"),
        _structure_table(scores, denominator="ok_only"),
        _answer_difference_table(scores),
        _text_table(scores),
        _editorial_table(scores),
        _table_score_table(table_golden, table_scores),
        _direct_compare_table(root),
        _cost_table(runs, costs, costs_present=manifest_paths["costs.jsonl"].is_file()),
        _failure_table(runs),
        _law_without_text_table(scores),
    ]
    return "\n\n".join(sections)


def write_report(
    root: Path,
    *,
    run_date: str,
    out_path: Path | None = None,
) -> Path:
    """Create a report from the template or replace only its generated region."""
    _validate_run_date(run_date)
    destination = out_path or root / "docs/results" / f"{run_date}-m2-parsing.md"
    source_path = destination if destination.exists() else TEMPLATE_PATH
    try:
        original = source_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ParsingReportError(
            f"결과 문서 틀을 읽을 수 없습니다: {source_path}"
        ) from exc

    start, finish = _marker_indexes(original)
    generated = render_tables(root)
    updated = f"{original[: start + len(BEGIN)]}\n\n{generated}\n\n{original[finish:]}"
    _write_atomic(destination, updated)
    return destination


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the offline M2 report generator."""
    parser = argparse.ArgumentParser(
        description="manifest에서 M2 파싱 결과표를 생성하거나 갱신합니다."
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--date",
        default=datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat(),
        help="결과 파일 실행일(YYYY-MM-DD, 기본값: Asia/Seoul 오늘)",
    )
    parser.add_argument("--out", type=Path, help="기본 결과 경로 대신 쓸 파일")
    args = parser.parse_args(argv)
    try:
        path = write_report(args.root, run_date=args.date, out_path=args.out)
    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        ParsingReportError,
    ) as exc:
        print(f"파싱 결과표 생성 실패: {exc}", file=sys.stderr)
        return 1
    print(path)
    return 0


def _measurement_table(root: Path, runs: Sequence[Mapping[str, object]]) -> str:
    latest = _latest_parser_records(runs)
    rows: list[list[object]] = [
        ["생성 시각", now_iso()],
        ["커밋 해시 (`git rev-parse HEAD`)", _git_commit(root)],
    ]
    for parser in _PARSERS:
        record = latest.get(parser)
        rows.extend(
            [
                [f"{parser} parser_version", _field(record, "parser_version")],
                [f"{parser} settings", _settings(record)],
            ]
        )
    models = sorted(
        {
            value
            for record in runs
            if record.get("parser") == "upstage"
            and isinstance((value := record.get("parser_version")), str)
            and value
        }
    )
    api_versions = sorted(
        {
            value
            for record in runs
            if record.get("parser") == "upstage"
            and isinstance((value := record.get("api_version")), str)
            and value
        }
    )
    rows.extend(
        [
            ["업스테이지 응답 model 버전 목록", ", ".join(models) or None],
            ["업스테이지 응답 API 버전 목록", ", ".join(api_versions) or None],
            [
                "`data/manifest/parse_targets.json` sha256",
                _file_hash(root / "data/manifest/parse_targets.json"),
            ],
            [
                "`infra/fontconfig/fonts.conf` sha256",
                _file_hash(root / "infra/fontconfig/fonts.conf"),
            ],
            [
                "`data/manifest/table_golden.json` sha256",
                _file_hash(root / "data/manifest/table_golden.json"),
            ],
        ]
    )
    return _table(
        "측정 정보",
        "분모: `parse_runs.jsonl`의 파서별 최신 기록. 고정 조건: 커밋과 입력·글꼴·표 정답 해시로 실행 조건을 식별한다.",
        ["항목", "값"],
        rows,
    )


def _ceiling_table(scores: Mapping[str, object] | None) -> str:
    ceiling = _parser_payload(scores, "splitter-ceiling")
    overall = _overall(ceiling)
    metrics = _mapping(overall.get("all_targets")) if overall else None
    documents = _documents(ceiling)
    eligible = [doc for doc in documents if doc.get("status") != "no_law_text"]
    mismatch_count = _ceiling_mismatch_count(eligible) if documents else None
    excluded = _excluded_count(overall)
    denominator = _field(metrics, "documents")
    note = (
        f"분모: {_display(denominator)}개 대상, excluded(no_law_text) "
        f"{_display(excluded)}개. 고정 조건: 법제처 조문을 이어 붙인 뒤 실제 파서와 같은 분할기·채점기를 적용한다."
    )
    rows = [
        [
            "splitter-ceiling",
            _percent(_field(metrics, "structure_exact_rate")),
            _decimal(_field(metrics, "key_f1_mean")),
            _decimal(_field(metrics, "text_sim_norm_mean_micro")),
            mismatch_count,
        ]
    ]
    return _table(
        "분할기 상한",
        note,
        [
            "대상",
            "구조 완전 일치율",
            "키 F1",
            "정규화 유사도(micro)",
            "100%가 아닌 대상 수",
        ],
        rows,
    )


def _excluded_table(scores: Mapping[str, object] | None) -> str:
    values = scores.get("excluded_targets") if scores else None
    rows: list[list[object]] = []
    if isinstance(values, list):
        for value in values:
            item = _mapping(value)
            if item is not None:
                rows.append(
                    [
                        item.get("law_rule_id"),
                        item.get("law_serial"),
                        item.get("role"),
                        item.get("reason"),
                    ]
                )
        if not rows:
            rows.append(["없음", "-", "-", "-"])
    else:
        rows.append([None, None, None, None])
    return _table(
        "제외 대상",
        "분모: `excluded_targets`. 고정 조건: 이 대상도 파싱·분할하지만 조 구조·조문 텍스트의 전체·ok만·분할기 상한 분모에서는 제외한다. 표 채점은 법제처와 무관하므로 영향이 없다.",
        ["rule_id", "serial", "role", "사유"],
        rows,
    )


def _structure_table(scores: Mapping[str, object] | None, *, denominator: str) -> str:
    title = (
        "조 구조 — 전체 대상 기준"
        if denominator == "all_targets"
        else "조 구조 — ok만 기준"
    )
    rows: list[list[object]] = []
    for parser in _PARSERS:
        payload = _parser_payload(scores, parser)
        overall = _overall(payload)
        metrics = _mapping(overall.get(denominator)) if overall else None
        statuses = _mapping(overall.get("status_counts")) if overall else None
        ok_count = statuses.get("ok", 0) if statuses is not None else None
        total_count = _field(metrics, "documents")
        document_count = (
            f"{ok_count}/{total_count}"
            if _is_number(ok_count) and _is_number(total_count)
            else None
        )
        rows.append(
            [
                parser,
                document_count,
                _percent(_field(metrics, "structure_exact_rate")),
                _decimal(_field(metrics, "key_precision_mean")),
                _decimal(_field(metrics, "key_recall_mean")),
                _decimal(_field(metrics, "order_lcs_ratio_mean")),
                _percent(_addenda_match_rate(payload, denominator)),
            ]
        )
    denominator_text = (
        "법제처 조문 텍스트가 있는 전체 파싱 대상(실패·빈 결과·누락은 0점)"
        if denominator == "all_targets"
        else "법제처 조문 텍스트가 있고 status=ok인 대상"
    )
    return _table(
        title,
        f"분모: {denominator_text}. 고정 조건: 동일한 조문 분할기와 법제처 조 번호·순서·부칙 블록을 사용한다.",
        [
            "파서",
            "문서 수(ok/전체)",
            "구조 완전 일치율",
            "키 정밀도",
            "키 재현율",
            "순서 LCS",
            "부칙 블록 수 일치율",
        ],
        rows,
    )


def _answer_difference_table(scores: Mapping[str, object] | None) -> str:
    differences = _mapping(scores.get("answer_differences")) if scores else None
    scored = differences is not None and differences.get("status") == "scored"
    recall = _mapping(differences.get("key_recall")) if scored else None
    rows: list[list[object]] = []
    for parser in _PARSERS:
        overall = _overall(_parser_payload(scores, parser))
        parser_recall = _mapping(recall.get(parser)) if recall else None
        row: list[object] = [
            parser,
            _field(
                _mapping(overall.get("all_targets")) if overall else None,
                "notation_variants",
            ),
        ]
        for denominator in ("all_targets", "ok_only"):
            if scored:
                metrics = (
                    _mapping(parser_recall.get(denominator)) if parser_recall else None
                )
                adjusted = _decimal(_field(metrics, "key_recall_mean_adjusted"))
            else:
                metrics = _mapping(overall.get(denominator)) if overall else None
                adjusted = "-"
            row.extend([_decimal(_field(metrics, "key_recall_mean")), adjusted])
        rows.append(row)

    if scored:
        note = (
            f"분모: 조 구조 표와 같다. 정답 차이 키는 문서별로 status=ok인 비교 파서가 2개 이상이고 "
            f"그 파서가 모두 놓친 법제처 조문 키이며 총 {_display(differences.get('total_keys'))}개다. "
            "보정 재현율은 그 키를 분모에서 뺀 값이다. 원본 표기 변형은 원본의 '의' 누락 오타(제N조M(제목))를 제N조의M으로 인식한 조 머리 수다. "
            "고정 조건: 동일한 분할기·채점기, 비교 파서는 "
            f"{', '.join(str(parser) for parser in differences.get('parsers') or [])}."
        )
    elif (
        differences is not None and differences.get("status") == "insufficient_parsers"
    ):
        parsers = ", ".join(str(parser) for parser in differences.get("parsers") or [])
        note = (
            f"분모: 조 구조 표와 같다. 채점된 비교 파서가 2개 미만({parsers})이라 정답 차이(원본≠법제처 추정)를 집계하지 않았다. "
            "원본 표기 변형은 원본의 '의' 누락 오타(제N조M(제목))를 제N조의M으로 인식한 조 머리 수다."
        )
    else:
        note = "분모: 조 구조 표와 같다. `answer_differences`가 없는 채점 결과다."
    sections = [
        _table(
            "원본 표기 변형·정답 차이",
            note,
            [
                "파서",
                "원본 표기 변형",
                "키 재현율(전체)",
                "정답 차이 제외 키 재현율(전체)",
                "키 재현율(ok만)",
                "정답 차이 제외 키 재현율(ok만)",
            ],
            rows,
        )
    ]
    if scored:
        target_rows: list[list[object]] = []
        values = differences.get("targets")
        for value in values if isinstance(values, list) else []:
            item = _mapping(value)
            if item is None:
                continue
            keys = item.get("law_keys")
            target_rows.append(
                [
                    item.get("law_rule_id"),
                    item.get("law_serial"),
                    item.get("role"),
                    ", ".join(str(parser) for parser in item.get("ok_parsers") or []),
                    ", ".join(
                        _article_label(key) for key in keys if isinstance(key, list)
                    )
                    if isinstance(keys, list)
                    else None,
                ]
            )
        if not target_rows:
            target_rows.append(["없음", "-", "-", "-", "-"])
        sections.append(
            _table(
                "정답 차이 대상",
                "분모: 정답 차이 키가 1개 이상인 대상. 고정 조건: 원문 대조로 원인(원본 오타·원본 누락·번호 체계 차이)을 사람이 확인해 대표 실패 사례에 적는다.",
                ["rule_id", "serial", "role", "ok 파서", "법제처에만 있는 조"],
                target_rows,
            )
        )
    return "\n\n".join(sections)


def _article_label(key: Sequence[object]) -> str:
    number, branch = key
    return f"제{number}조의{branch}" if branch else f"제{number}조"


def _text_table(scores: Mapping[str, object] | None) -> str:
    rows: list[list[object]] = []
    for parser in _PARSERS:
        payload = _parser_payload(scores, parser)
        overall = _overall(payload)
        for key, label in (("all_targets", "전체 대상"), ("ok_only", "ok만")):
            metrics = _mapping(overall.get(key)) if overall else None
            docs = _field(metrics, "documents")
            articles = _field(metrics, "matched_articles")
            denominator = (
                f"문서 {docs} / 짝지은 조문 {articles}"
                if _is_number(docs) and _is_number(articles)
                else None
            )
            rows.append(
                [
                    parser,
                    label,
                    denominator,
                    _decimal(_field(metrics, "text_sim_raw_mean_micro")),
                    _percent(_field(metrics, "text_exact_raw_rate_micro")),
                    _decimal(_field(metrics, "text_sim_norm_mean_micro")),
                    _percent(_field(metrics, "text_exact_norm_rate_micro")),
                    _decimal(_field(metrics, "text_sim_raw_mean_macro")),
                    _percent(_field(metrics, "text_exact_raw_rate_macro")),
                    _decimal(_field(metrics, "text_sim_norm_mean_macro")),
                    _percent(_field(metrics, "text_exact_norm_rate_macro")),
                ]
            )
    return _table(
        "조문 텍스트",
        "분모: micro는 조 번호로 짝지은 조문, macro는 해당 문서 분모. 고정 조건: 원문 비교는 NFC·바깥 공백 제거만, 정규화 비교는 N1~N5를 순서대로 적용한다.",
        [
            "파서",
            "대상",
            "분모",
            "원문 유사도 micro",
            "원문 완전 일치 micro",
            "정규화 유사도 micro",
            "정규화 완전 일치 micro",
            "원문 유사도 macro",
            "원문 완전 일치 macro",
            "정규화 유사도 macro",
            "정규화 완전 일치 macro",
        ],
        rows,
    )


def _editorial_table(scores: Mapping[str, object] | None) -> str:
    rows: list[list[object]] = []
    for parser in _PARSERS:
        payload = _parser_payload(scores, parser)
        overall = _overall(payload)
        metrics = _mapping(overall.get("all_targets")) if overall else None
        rows.append(
            [
                parser,
                _field(metrics, "editorial_law_only"),
                _field(metrics, "editorial_parsed_only"),
                _field(metrics, "editorial_both"),
            ]
        )
    return _table(
        "편집 차이",
        "분모: 법제처 조문 텍스트가 있는 전체 대상에서 짝지은 조문. 고정 조건: N2 편집 주석 존재 위치를 법제처만·파싱만·양쪽으로 분리 집계한다.",
        ["파서", "editorial_law_only", "editorial_parsed_only", "editorial_both"],
        rows,
    )


def _table_score_table(
    table_golden: Mapping[str, object] | None,
    table_scores: Mapping[str, object] | None,
) -> str:
    golden_values = table_golden.get("tables") if table_golden else None
    golden_count = len(golden_values) if isinstance(golden_values, list) else None
    summaries = _mapping(table_scores.get("parsers")) if table_scores else None
    excluding = (
        _mapping(table_scores.get("parsers_excluding_nested")) if table_scores else None
    )
    score_rows = table_scores.get("scores") if table_scores else None
    rows: list[list[object]] = []
    for parser in _PARSERS:
        if parser == "pypdf":
            rows.append([parser, "해당 없음(표 추출 기능 없음)", "-", "-", "-", "-"])
            continue
        variants: list[tuple[str, Mapping[str, object] | None, bool]] = [
            (parser, _mapping(summaries.get(parser)) if summaries else None, True)
        ]
        if excluding is not None:
            variants.append(
                (f"{parser} (중첩 제외)", _mapping(excluding.get(parser)), False)
            )
        for label, summary, include_nested in variants:
            rows.append(
                [
                    label,
                    "채점됨" if summary and summary.get("applicable") else None,
                    _field(summary, "measured"),
                    _decimal(_field(summary, "cell_exact_rate_mean")),
                    _decimal(_field(summary, "cell_sim_mean")),
                    _shape_match_count(
                        score_rows, parser, include_nested=include_nested
                    ),
                ]
            )
    return _table(
        "표 셀 일치",
        f"분모: 사람 검수 표 정답 {_display(golden_count)}개 중 파서별 채점 가능 정답. '중첩 제외' 행은 정답이 중첩 표이거나 선택된 파서 표가 중첩 표를 평탄화한 경우를 뺀다. 고정 조건: 병합 셀은 덮는 모든 칸에 복제하고, 중첩 표 셀 텍스트는 바깥 셀에 문서 순서대로 이어 붙이며(HWPX 정답과 같은 규칙), 여러 쪽 표도 엄격하게 채점한다.",
        [
            "파서",
            "적용",
            "채점된 정답 수",
            "셀 완전 일치율 평균",
            "셀 유사도 평균",
            "크기 일치 수",
        ],
        rows,
    )


def _direct_compare_table(root: Path) -> str:
    comparison = compare_direct(root)
    if comparison is None:
        return "### 변환 경유 vs 직접 입력\n\n(실행 후 기록)"

    rows: list[list[object]] = []
    for parser, label in (
        ("upstage", "변환 PDF"),
        ("upstage-direct", "HWP/HWPX 원본"),
    ):
        metrics = _mapping(comparison.get(parser))
        rows.append(
            [
                label,
                _percent(_field(metrics, "structure_exact_rate")),
                _decimal(_field(metrics, "key_f1_mean")),
                _decimal(_field(metrics, "text_sim_raw_mean")),
                _decimal(_field(metrics, "text_sim_norm_mean")),
                _decimal(_field(metrics, "table_cell_exact_rate")),
                _field(metrics, "billed_pages"),
                _seconds(_field(metrics, "seconds_per_page")),
            ]
        )
    return _table(
        "변환 경유 vs 직접 입력",
        "분모: 양쪽 결과가 모두 있는 대상 "
        f"{_display(comparison.get('paired_targets'))}개(표 정답 "
        f"{_display(comparison.get('paired_tables'))}개). 고정 조건: 동일한 요청 옵션·"
        "조문 분할기·채점 함수를 쓴다. HWP 과금 쪽수 = 응답 usage.pages, "
        "PDF 쪽수와 다를 수 있음.",
        [
            "입력",
            "구조 완전 일치율",
            "키 F1",
            "원문 유사도",
            "정규화 유사도",
            "표 셀 일치율",
            "과금 쪽수 합",
            "쪽당 시간(초)",
        ],
        rows,
    )


def _cost_table(
    runs: Sequence[Mapping[str, object]],
    costs: Sequence[Mapping[str, object]],
    *,
    costs_present: bool,
) -> str:
    attempts = _latest_actual_records(runs)
    cost_by_service: dict[str, list[float]] = {}
    for entry in costs:
        service = entry.get("service")
        amount = entry.get("amount_usd")
        if isinstance(service, str) and _is_number(amount):
            cost_by_service.setdefault(service, []).append(float(amount))

    rows: list[list[object]] = []
    for parser in _PARSERS:
        parser_runs = [record for record in attempts if record.get("parser") == parser]
        if parser_runs:
            pages = sum(
                int(page_count)
                for record in parser_runs
                if record.get("status") in {"ok", "empty"}
                and _is_number(page_count := record.get("page_count"))
            )
            ok_timed = [
                (float(elapsed_value), int(page_value))
                for record in parser_runs
                if record.get("status") == "ok"
                and _is_number(elapsed_value := record.get("elapsed_s"))
                and _is_number(page_value := record.get("page_count"))
            ]
            elapsed = math.fsum(value for value, _ in ok_timed)
            ok_pages = sum(page for _, page in ok_timed)
            # Model loading is shown separately and never enters seconds per page.
            seconds_per_page: float | None = elapsed / ok_pages if ok_pages else None
        else:
            pages = elapsed = seconds_per_page = None

        if parser == "upstage":
            service = _UPSTAGE_SERVICE
            amount = (
                math.fsum(cost_by_service.get(service, [])) if costs_present else None
            )
        else:
            service = "로컬(원장 기록 없음)"
            amount = 0.0 if parser_runs else None
        cost_per_page = (
            float(amount) / pages
            if _is_number(amount) and _is_number(pages) and pages
            else None
        )
        rows.append(
            [
                parser,
                service,
                pages,
                _seconds(elapsed),
                _seconds(seconds_per_page),
                _initialization_text(
                    [record for record in runs if record.get("parser") == parser]
                ),
                _usd(amount),
                _usd(cost_per_page),
            ]
        )
    return _table(
        "처리 비용",
        "분모: 총 쪽수·쪽당 비용은 파서·입력별 최신 실제 처리 기록의 성공·빈 결과 쪽수다. 문서 시간 합·쪽당 초는 최신 ok 기록만 쓴다(elapsed_s 합 ÷ page_count 합, 실패·빈 결과 제외). 고정 조건: 모델 로딩(init_s)은 쪽당 초에 넣지 않고 실행 순서대로 따로 적는다(최초가 캐시 없는 상태). 비용은 `costs.jsonl`의 service별 정가와 부가세 포함 금액을 합산한다.",
        [
            "파서",
            "비용 service",
            "총 쪽수",
            "문서 시간 합(초, ok)",
            "쪽당 초(ok)",
            "모델 로딩(초)",
            "원장 금액 합(USD, 세금 포함)",
            "쪽당 비용(USD)",
        ],
        rows,
    )


def _failure_table(runs: Sequence[Mapping[str, object]]) -> str:
    attempts = _latest_actual_records(runs)
    rows: list[list[object]] = []
    for parser in _PARSERS:
        parser_runs = [record for record in attempts if record.get("parser") == parser]
        if not parser_runs:
            rows.append([parser, None, None, None])
            continue
        failed = [record for record in parser_runs if record.get("status") == "failed"]
        errors = Counter(
            value
            for record in failed
            if isinstance((value := record.get("error_type")), str) and value
        )
        rows.append(
            [
                parser,
                len(failed),
                sum(record.get("status") == "empty" for record in parser_runs),
                ", ".join(f"{name}={count}" for name, count in sorted(errors.items()))
                or "없음",
            ]
        )
    return _table(
        "실패 목록",
        "분모: 파서·입력별 최신 실제 처리 기록. 고정 조건: failed와 한글이 없는 empty를 분리하고 failed의 error_type만 집계한다.",
        ["파서", "failed", "empty", "error_type 분포"],
        rows,
    )


def _law_without_text_table(scores: Mapping[str, object] | None) -> str:
    corpus = (
        _mapping(scores.get("law_versions_without_article_text")) if scores else None
    )
    versions = corpus.get("versions") if corpus else None
    rows: list[list[object]] = []
    if isinstance(versions, list):
        for value in versions:
            item = _mapping(value)
            if item is not None:
                current = item.get("is_current")
                role = (
                    "현행" if current is True else "과거" if current is False else None
                )
                rows.append(
                    [
                        item.get("law_rule_id"),
                        item.get("law_serial"),
                        role,
                        item.get("reason"),
                    ]
                )
        if not rows:
            rows.append(["없음", "-", "-", "-"])
    else:
        rows.append([None, None, None, None])
    total = _field(corpus, "versions_total")
    without = _field(corpus, "versions_without_article_text")
    return _table(
        "법제처 본문에 조문 텍스트가 없는 버전",
        f"분모: 수집한 전체 법제처 버전 {_display(total)}개, 조 머리 0개인 버전 {_display(without)}개(파싱 대상 밖 포함). 고정 조건: M4 질의셋에서 이 규정의 조항 단위 정답은 법제처에서 얻을 수 없다고 기록한다.",
        ["rule_id", "serial", "현행 여부", "사유"],
        rows,
    )


def _table(
    title: str,
    note: str,
    headers: Sequence[str],
    rows: Sequence[Sequence[object]],
) -> str:
    lines = [f"### {title}", "", note, ""]
    lines.append("| " + " | ".join(_markdown(value) for value in headers) + " |")
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    lines.extend(
        "| " + " | ".join(_markdown(_display(value)) for value in row) + " |"
        for row in rows
    )
    return "\n".join(lines)


def _load_json(path: Path) -> Mapping[str, object] | None:
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ParsingReportError(f"manifest 최상위 값이 객체가 아닙니다: {path}")
    return value


def _load_jsonl(path: Path) -> list[Mapping[str, object]]:
    if not path.exists():
        return []
    records: list[Mapping[str, object]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, Mapping):
            raise ParsingReportError(
                f"manifest 레코드가 객체가 아닙니다: {path}:{line_number}"
            )
        records.append(value)
    return records


def _latest_parser_records(
    runs: Sequence[Mapping[str, object]],
) -> dict[str, Mapping[str, object]]:
    latest: dict[str, Mapping[str, object]] = {}
    for record in runs:
        parser = record.get("parser")
        if isinstance(parser, str):
            latest[parser] = record
    return latest


def _latest_actual_records(
    runs: Sequence[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    latest: dict[tuple[str, str], Mapping[str, object]] = {}
    for index, record in enumerate(runs):
        if record.get("status") == "skipped":
            continue
        parser = record.get("parser")
        input_sha = record.get("input_sha256")
        if isinstance(parser, str):
            key = (parser, input_sha if isinstance(input_sha, str) else str(index))
            latest[key] = record
    return list(latest.values())


def _parser_payload(
    scores: Mapping[str, object] | None, parser: str
) -> Mapping[str, object] | None:
    parsers = _mapping(scores.get("parsers")) if scores else None
    payload = _mapping(parsers.get(parser)) if parsers else None
    return payload if payload and payload.get("status") == "scored" else None


def _overall(payload: Mapping[str, object] | None) -> Mapping[str, object] | None:
    aggregate = _mapping(payload.get("aggregate")) if payload else None
    return _mapping(aggregate.get("overall")) if aggregate else None


def _documents(
    payload: Mapping[str, object] | None,
) -> list[Mapping[str, object]]:
    values = payload.get("documents") if payload else None
    return (
        [item for value in values if (item := _mapping(value)) is not None]
        if isinstance(values, list)
        else []
    )


def _ceiling_mismatch_count(documents: Sequence[Mapping[str, object]]) -> int | None:
    if any(
        not _is_number(document.get("key_precision"))
        or not _is_number(document.get("key_recall"))
        or not _is_number(document.get("text_sim_norm_mean"))
        for document in documents
    ):
        return None
    count = 0
    for document in documents:
        precision = float(document["key_precision"])
        recall = float(document["key_recall"])
        f1 = (
            2 * precision * recall / (precision + recall) if precision + recall else 0.0
        )
        if (
            document.get("structure_exact") is not True
            or f1 < 1.0
            or float(document["text_sim_norm_mean"]) < 1.0
        ):
            count += 1
    return count


def _excluded_count(overall: Mapping[str, object] | None) -> object:
    excluded = _mapping(overall.get("excluded")) if overall else None
    return excluded.get("no_law_text", 0) if excluded is not None else None


def _addenda_match_rate(
    payload: Mapping[str, object] | None, denominator: str
) -> float | None:
    documents = _documents(payload)
    if denominator == "all_targets":
        selected = [doc for doc in documents if doc.get("status") != "no_law_text"]
    else:
        selected = [doc for doc in documents if doc.get("status") == "ok"]
    if not selected or any(
        not _is_number(doc.get("addenda_law"))
        or not _is_number(doc.get("addenda_parsed"))
        for doc in selected
    ):
        return None
    return sum(
        doc.get("status") == "ok"
        and doc.get("addenda_law") == doc.get("addenda_parsed")
        for doc in selected
    ) / len(selected)


def _shape_match_count(
    values: object, parser: str, *, include_nested: bool = True
) -> int | None:
    if not isinstance(values, list):
        return None
    measured = [
        item
        for value in values
        if (item := _mapping(value)) is not None
        and item.get("parser") == parser
        and _is_number(item.get("cell_exact_rate"))
        and (include_nested or item.get("nested") is not True)
    ]
    return sum(item.get("shape_match") is True for item in measured)


def _initialization_text(records: Sequence[Mapping[str, object]]) -> str | None:
    """List one init_s per run in manifest order, marking the first (cold) run."""
    by_run: dict[object, float] = {}
    for record in records:
        value = record.get("init_s")
        if _is_number(value):
            run_id = record.get("run_id") or None
            by_run[run_id] = max(by_run.get(run_id, 0.0), float(value))
    if not by_run:
        return "-" if records else None
    values = [_seconds(value) for value in by_run.values()]
    if len(values) == 1:
        return values[0]
    return ", ".join([f"{values[0]}(최초)", *values[1:]])


def _settings(record: Mapping[str, object] | None) -> object:
    if record is None:
        return None
    settings = record.get("settings")
    if isinstance(settings, Mapping):
        return json.dumps(
            settings, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    if settings is not None:
        return settings
    return record.get("settings_hash")


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _field(mapping: Mapping[str, object] | None, key: str) -> object:
    return mapping.get(key) if mapping is not None else None


def _file_hash(path: Path) -> str | None:
    return sha256_file(path) if path.is_file() else None


def _git_commit(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _validate_run_date(value: str) -> None:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("실행일은 YYYY-MM-DD 형식이어야 합니다.") from exc
    if parsed.isoformat() != value:
        raise ValueError("실행일은 YYYY-MM-DD 형식이어야 합니다.")


def _marker_indexes(text: str) -> tuple[int, int]:
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise ParsingReportError("generated 표지가 없거나 짝이 맞지 않습니다.")
    start = text.index(BEGIN)
    finish = text.index(END)
    if start >= finish:
        raise ParsingReportError("generated 표지 순서가 잘못되었습니다.")
    return start, finish


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
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
            temporary_path = Path(destination.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _percent(value: object) -> str | None:
    return f"{float(value) * 100:.1f}%" if _is_number(value) else None


def _decimal(value: object) -> str | None:
    return f"{float(value):.4f}" if _is_number(value) else None


def _seconds(value: object) -> str | None:
    return f"{float(value):.3f}" if _is_number(value) else None


def _usd(value: object) -> str | None:
    return f"{float(value):.4f}" if _is_number(value) else None


def _display(value: object) -> str:
    return _PLACEHOLDER if value is None else str(value)


def _markdown(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":  # pragma: no cover - exercised by CLI acceptance check
    raise SystemExit(main())
