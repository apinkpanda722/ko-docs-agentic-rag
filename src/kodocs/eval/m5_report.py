"""Render the fixed M5 retrieval report from deterministic local artifacts."""

from __future__ import annotations

import argparse
import json
import math
import platform
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TEMPLATE_PATH = Path(__file__).parent / "templates" / "m5_retrieval.md"
REPORT_PATH = Path("docs/results/2026-09-30-m5-retrieval.md")
BEGIN, END = "<!-- BEGIN generated -->", "<!-- END generated -->"

_PLACEHOLDER = "(실행 후 기록)"
_MANIFEST_DIR = Path("data/manifest")
_CONFIG_PATH = _MANIFEST_DIR / "m5_config.json"
_DEV_PATH = _MANIFEST_DIR / "m5_dev.json"
_INDEX_PATH = _MANIFEST_DIR / "m5_index.json"
_ANNEX_PATH = _MANIFEST_DIR / "m5_annex_mapping.json"
_EVAL_INPUTS_PATH = _MANIFEST_DIR / "m5_eval_inputs.json"
_PRIOR_VERSIONS_PATH = _MANIFEST_DIR / "m5_prior_versions.json"
_RUNS_PATH = _MANIFEST_DIR / "m5_test_runs.jsonl"
_RESULTS_DIR = _MANIFEST_DIR / "m5_test_results"
_KST = ZoneInfo("Asia/Seoul")
_INDEX_NAMES = (
    "kodocs-m5-composite-fixed",
    "kodocs-m5-composite-structural",
    "kodocs-m5-pypdf-structural",
    "kodocs-m5-upstage-structural",
)
_DOC_TYPES = (
    "regulation",
    "meeting",
    "incident",
    "weekly",
    "runbook",
    "guide",
    "notice",
)


class M5ReportError(RuntimeError):
    """Report inputs or generated-region markers are invalid."""


def render_tables(root: Path) -> str:
    """Render the generated region, leaving unavailable measurements blank."""
    return _render_tables(Path(root).resolve(), run_date=None)


def write_report(root: Path, *, run_date: date) -> Path:
    """Create the fixed M5 report or replace only its generated region."""
    if not isinstance(run_date, date):
        raise TypeError("run_date는 date여야 합니다.")
    root = Path(root).resolve()
    destination = root / REPORT_PATH
    source = destination if destination.exists() else TEMPLATE_PATH
    try:
        original = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise M5ReportError(f"결과 문서 틀을 읽을 수 없습니다: {source}") from exc
    start, finish = _marker_indexes(original)
    generated = _render_tables(root, run_date=run_date)
    updated = f"{original[: start + len(BEGIN)]}\n\n{generated}\n\n{original[finish:]}"
    _write_atomic(destination, updated)
    return destination


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the offline, fixed-path M5 report generator."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--date",
        default=datetime.now(_KST).date().isoformat(),
        help="결과 문서에 기록할 실행일(YYYY-MM-DD, 기본값: Asia/Seoul 오늘)",
    )
    args = parser.parse_args(argv)
    try:
        path = write_report(args.root, run_date=_parse_date(args.date))
    except (
        M5ReportError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"M5 검색 결과표 생성 실패: {exc}", file=sys.stderr)
        return 1
    print(path)
    return 0


def _render_tables(root: Path, *, run_date: date | None) -> str:
    config = _load_optional_json(root / _CONFIG_PATH)
    dev = _load_optional_json(root / _DEV_PATH)
    index = _load_optional_json(root / _INDEX_PATH)
    annex = _load_optional_json(root / _ANNEX_PATH)
    eval_inputs = _load_optional_json(root / _EVAL_INPUTS_PATH)
    prior_versions = _load_optional_json(root / _PRIOR_VERSIONS_PATH)
    run_records = _load_optional_jsonl(root / _RUNS_PATH)
    results = _load_results(root / _RESULTS_DIR)
    latest = max(results, key=_sequence_number, default={})
    return "\n\n".join(
        (
            _execution_section(root, run_date, config, eval_inputs, index, latest),
            _corpus_section(index, annex, prior_versions),
            _retrieval_results_section(latest),
            _hypotheses_section(latest),
            _auxiliary_section(latest),
            _run_history_section(run_records, results),
            _dev_section(dev),
        )
    )


def _execution_section(
    root: Path,
    run_date: date | None,
    config: Mapping[str, object],
    eval_inputs: Mapping[str, object],
    index: Mapping[str, object],
    latest: Mapping[str, object],
) -> str:
    environment = _mapping(latest.get("environment"))
    result_embedder = _mapping(environment.get("embedder"))
    index_entry = _index_entry(index, "kodocs-m5-composite-structural")
    index_embedder = _mapping(index_entry.get("embedder"))
    embedder = result_embedder or index_embedder
    opensearch = _mapping(environment.get("opensearch"))
    bootstrap = _mapping(environment.get("bootstrap"))
    chunking = _mapping(index.get("chunking"))
    counts = _mapping(latest.get("question_counts")) or _mapping(
        eval_inputs.get("target_counts")
    )
    counting_units = _mapping(latest.get("counting_units"))
    fixed_configuration = _mapping(latest.get("fixed_configuration"))
    hybrid_method = config.get(
        "hybrid_method", fixed_configuration.get("hybrid_method")
    )
    candidate_chunks = config.get(
        "candidate_chunks", fixed_configuration.get("candidate_chunks")
    )
    bm25_weight = config.get("bm25_weight", fixed_configuration.get("bm25_weight"))
    rank_constant = config.get(
        "rank_constant", fixed_configuration.get("rank_constant")
    )
    fixed_size = config.get("fixed_size", chunking.get("fixed_size"))
    fixed_overlap = config.get("fixed_overlap", chunking.get("fixed_overlap"))
    fixed_text = _fixed_conditions(
        fixed_size,
        fixed_overlap,
        hybrid_method,
        candidate_chunks,
        bm25_weight,
        rank_constant,
    )
    embedder_text = _join_fields(
        (
            ("model", embedder.get("model_id")),
            ("revision", embedder.get("revision")),
            ("device", embedder.get("device")),
            ("settings_hash", embedder.get("settings_hash")),
            ("init_s", embedder.get("init_s")),
        )
    )
    opensearch_text = _join_fields(
        (("version", opensearch.get("version")), ("heap", opensearch.get("heap")))
    )
    question_text = _join_fields(
        (
            ("gold test", counts.get("gold_test")),
            ("silver test", counts.get("silver_test")),
        )
    )
    return "\n".join(
        (
            "## 실행 정보",
            "",
            _table(
                ("항목", "값"),
                (
                    ("실행일(Asia/Seoul)", run_date.isoformat() if run_date else None),
                    ("코드 커밋", environment.get("code_commit") or _git_commit(root)),
                    (
                        "플랫폼",
                        environment.get("platform") or _measurement_environment(),
                    ),
                    ("임베딩", embedder_text),
                    ("OpenSearch", opensearch_text),
                    ("청킹·hybrid 고정 조건", fixed_text),
                    (
                        "config_hash",
                        config.get("config_hash") or latest.get("config_hash"),
                    ),
                    (
                        "부트스트랩",
                        _join_fields(
                            (
                                ("seed", bootstrap.get("seed")),
                                ("resamples", bootstrap.get("resamples")),
                            )
                        ),
                    ),
                    ("검색 지표 문항 수", question_text),
                    (
                        "세는 기준",
                        counting_units.get("retrieval")
                        or _mapping(eval_inputs.get("target_definition")).get("unit"),
                    ),
                ),
            ),
        )
    )


def _fixed_conditions(
    fixed_size: object,
    fixed_overlap: object,
    method: object,
    candidate_chunks: object,
    weight: object,
    rank_constant: object,
) -> str | None:
    fields = (
        ("fixed_size", fixed_size),
        ("fixed_overlap", fixed_overlap),
        ("hybrid", method),
        ("candidate_chunks", candidate_chunks),
        ("bm25_weight", weight),
        ("rank_constant", rank_constant),
    )
    return _join_fields(fields)


def _corpus_section(
    index: Mapping[str, object],
    annex: Mapping[str, object],
    prior_versions: Mapping[str, object],
) -> str:
    index_rows = []
    for name in _INDEX_NAMES:
        entry = _index_entry(index, name)
        documents = _mapping(entry.get("document_counts"))
        index_rows.append(
            (
                name,
                documents.get("total"),
                documents.get("real_current"),
                documents.get("real_prior"),
                documents.get("synthetic"),
                entry.get("chunk_count"),
                entry.get("indexed_chunk_count"),
                entry.get("failure_count"),
            )
        )
    counts = _mapping(annex.get("counts"))
    gold_tables = _mapping(annex.get("gold_table_qrels"))
    mapping_failure = None
    if gold_tables:
        checked = gold_tables.get("checked_count")
        matched = gold_tables.get("matched_count")
        if _is_number(checked) and _is_number(matched):
            mapping_failure = int(checked) - int(matched)
    chunking = _mapping(index.get("chunking"))
    length_summary = _mapping(chunking.get("structural_body_length_chars"))
    length_intro = (
        "고정 크기 "
        f"{_display(chunking.get('fixed_size'))}자·겹침 "
        f"{_display(chunking.get('fixed_overlap'))}자. "
        "전체 구조 청크 중앙값 "
        f"{_display(length_summary.get('median'))}자·p90 "
        f"{_display(length_summary.get('p90'))}자·최대 "
        f"{_display(length_summary.get('max'))}자."
    )
    versions = prior_versions.get("versions")
    version_rows: list[tuple[object, ...]] = []
    if isinstance(versions, Sequence) and not isinstance(versions, (str, bytes)):
        for value in versions:
            row = _mapping(value)
            version_rows.append(
                (
                    row.get("rule_id"),
                    row.get("current_serial"),
                    row.get("current_issued_date"),
                    row.get("prior_serial"),
                    row.get("prior_issued_date"),
                )
            )
    if not version_rows:
        version_rows = [(None, None, None, None, None)]
    return "\n\n".join(
        (
            "\n".join(
                (
                    "## 코퍼스·인덱스 요약",
                    "",
                    _table(
                        (
                            "인덱스",
                            "문서 계",
                            "실제 현행",
                            "실제 개정 전",
                            "합성",
                            "청크",
                            "색인 청크",
                            "색인 실패",
                        ),
                        index_rows,
                    ),
                    "",
                    _table(
                        ("별표 매핑 항목", "값"),
                        (
                            ("골드 qrels 매핑 실패", mapping_failure),
                            ("전체 fallback 별표", counts.get("fallback_annex_count")),
                            (
                                "미매핑 업스테이지 표",
                                counts.get("unmapped_table_count"),
                            ),
                        ),
                    ),
                )
            ),
            "\n".join(
                (
                    "### 개정 전 5건",
                    "",
                    _table(
                        (
                            "rule_id",
                            "현행 serial",
                            "현행 발령일",
                            "개정 전 serial",
                            "개정 전 발령일",
                        ),
                        version_rows,
                    ),
                )
            ),
            "\n".join(
                (
                    "### 구조 청크 길이(문자)",
                    "",
                    length_intro,
                    "",
                    _chunk_length_table(index),
                )
            ),
            "\n".join(
                (
                    "### 임베딩 잘림",
                    "",
                    _table(
                        (
                            "인덱스",
                            "max_seq_length",
                            "한도 초과 청크",
                            "골드 qrels 청크",
                            "실버 qrels 청크",
                        ),
                        [
                            (
                                name,
                                _index_entry(index, name).get("max_seq_length"),
                                _index_entry(index, name).get("over_limit_chunk_count"),
                                _index_entry(index, name).get(
                                    "gold_answer_chunks_over_limit"
                                ),
                                _index_entry(index, name).get(
                                    "silver_answer_chunks_over_limit"
                                ),
                            )
                            for name in _INDEX_NAMES
                        ],
                    ),
                )
            ),
        )
    )


def _chunk_length_table(index: Mapping[str, object]) -> str:
    entry = _index_entry(index, "kodocs-m5-composite-structural")
    by_source = _mapping(entry.get("chunk_length_chars_by_source"))
    by_type = _mapping(entry.get("chunk_length_chars_by_doc_type"))
    rows: list[tuple[object, ...]] = []
    for source in ("real", "synthetic"):
        stats = _mapping(by_source.get(source))
        rows.append(("출처", source, stats.get("count"), stats.get("median")))
    for document_type in _DOC_TYPES:
        stats = _mapping(by_type.get(document_type))
        rows.append(
            ("문서 유형", document_type, stats.get("count"), stats.get("median"))
        )
    return _table(("구분", "값", "청크 수", "중앙값(자)"), rows)


def _retrieval_results_section(latest: Mapping[str, object]) -> str:
    analysis = _mapping(latest.get("analysis"))
    silver = _mapping(_mapping(analysis.get("silver_direction")).get("all"))
    hypotheses = _mapping(analysis.get("hypotheses"))
    h7 = _mapping(hypotheses.get("H7"))
    source_rows = []
    for scope, payload in (
        ("H7 동일 유형", h7),
        ("전체 검색 유형", _mapping(h7.get("all_types_descriptive"))),
    ):
        for source in ("real", "synthetic"):
            stats = _mapping(payload.get(source))
            source_rows.append(
                (
                    scope,
                    source,
                    stats.get("n"),
                    stats.get("mean"),
                    stats.get("sd"),
                    stats.get("median"),
                    stats.get("min"),
                    stats.get("max"),
                )
            )
    return "\n\n".join(
        (
            "## 검색 결과\n\n명목 16, kNN 두 행 합쳐 14조합을 보고한다.",
            _ladder_section("골드 test 21 — 주 표 — 사다리", analysis.get("ladder")),
            _ladder_section("실버 test 65 — 방향 확인용 사다리", silver.get("ladder")),
            _ablation_section(analysis.get("ablation")),
            "\n".join(
                (
                    "### 실제/합성 문항 점수 분리",
                    "",
                    "최종 구성의 문서 단위 Recall@10 기술 통계다. H7 동일 유형 행만 H7 판정 표본이다.",
                    "",
                    _table(
                        ("범위", "출처", "n", "평균", "sd", "중앙값", "최소", "최대"),
                        source_rows,
                    ),
                )
            ),
            _full_grid_section(analysis.get("full_grid")),
        )
    )


def _ladder_section(title: str, value: object) -> str:
    payload = _mapping(value)
    stages = payload.get("stages")
    rows: list[tuple[object, ...]] = []
    if isinstance(stages, Sequence) and not isinstance(stages, (str, bytes)):
        for stage_value in stages:
            stage = _mapping(stage_value)
            delta = _mapping(stage.get("delta_from_previous"))
            rows.append(
                (
                    stage.get("id"),
                    stage.get("mean_recall10"),
                    stage.get("mean_ndcg10"),
                    _ci_text(delta.get("recall10")),
                    _ci_text(delta.get("ndcg10")),
                )
            )
    if not rows:
        rows = [(None, None, None, None, None)]
    return "\n".join(
        (
            f"### {title}",
            "",
            _table(
                (
                    "단계",
                    "Recall@10",
                    "NDCG@10",
                    "이전 대비 Recall Δ [95% CI]",
                    "이전 대비 NDCG Δ [95% CI]",
                ),
                rows,
            ),
        )
    )


def _ablation_section(value: object) -> str:
    payload = _mapping(value)
    removals = payload.get("removals")
    rows: list[tuple[object, ...]] = []
    if isinstance(removals, Sequence) and not isinstance(removals, (str, bytes)):
        for removal_value in removals:
            row = _mapping(removal_value)
            delta = _mapping(row.get("delta_final_minus_removed"))
            rows.append(
                (
                    row.get("element"),
                    row.get("configuration"),
                    row.get("mean_recall10"),
                    row.get("mean_ndcg10"),
                    _ci_text(delta.get("recall10")),
                    _ci_text(delta.get("ndcg10")),
                )
            )
    if not rows:
        rows = [(None, None, None, None, None, None)]
    return "\n".join(
        (
            "### 보조 표 — 제거 실험",
            "",
            _table(
                (
                    "제거 요소",
                    "남은 구성",
                    "Recall@10",
                    "NDCG@10",
                    "최종-제거 Recall Δ [95% CI]",
                    "최종-제거 NDCG Δ [95% CI]",
                ),
                rows,
            ),
        )
    )


def _full_grid_section(value: object) -> str:
    payload = _mapping(value)
    values = payload.get("rows")
    rows: list[tuple[object, ...]] = []
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        for grid_value in values:
            row = _mapping(grid_value)
            rows.append(
                (
                    row.get("chunking"),
                    row.get("method"),
                    row.get("analyzer"),
                    row.get("candidate_chunks"),
                    row.get("bm25_weight"),
                    row.get("selected_hybrid"),
                    row.get("mean_recall10"),
                    row.get("mean_ndcg10"),
                    row.get("span_hit_rate"),
                    row.get("top1_hit_rate"),
                )
            )
    if not rows:
        rows = [(None,) * 10]
    return "\n".join(
        (
            "### 부록 — 전체 조합표",
            "",
            _table(
                (
                    "청킹",
                    "방식",
                    "분석기",
                    "후보 청크",
                    "BM25 가중치",
                    "확정 hybrid",
                    "Recall@10",
                    "NDCG@10",
                    "구간 적중률",
                    "Top1 적중률",
                ),
                rows,
            ),
        )
    )


def _hypotheses_section(latest: Mapping[str, object]) -> str:
    analysis = _mapping(latest.get("analysis"))
    hypotheses = _mapping(analysis.get("hypotheses"))
    rows = []
    fixed_counts: dict[str, object] = {"H1": 21, "H2": 21, "H3": 21, "H4": 4}
    for hypothesis_id in ("H1", "H2", "H3", "H4", "H7"):
        value = _mapping(hypotheses.get(hypothesis_id))
        statistic = _mapping(value.get("statistic"))
        count: object = value.get("n", fixed_counts.get(hypothesis_id))
        if hypothesis_id == "H7":
            count = _join_fields(
                (
                    ("합성", value.get("n_synthetic", 8)),
                    ("실제", value.get("n_real", 6)),
                )
            )
        rows.append(
            (
                hypothesis_id,
                value.get("verdict"),
                count,
                value.get("comparison"),
                _ci_text(statistic),
                _hypothesis_reason(value),
            )
        )
    rows.extend(
        (
            ("H5", "M6·M7 대상", 13, "Agentic RAG - 단순 RAG", None, "M5 판정 범위 밖"),
            (
                "H6",
                "M6·M7 대상",
                "200 이상",
                "관측 누설률 0%",
                None,
                "M7 보안 테스트셋 대상",
            ),
        )
    )
    h3 = _mapping(analysis.get("h3_breakdown"))
    h3_rows = []
    for set_name in ("gold_test", "silver_test"):
        by_source = _mapping(h3.get(set_name))
        for source in ("real", "synthetic"):
            value = _mapping(by_source.get(source))
            difference = _mapping(value.get("difference"))
            h3_rows.append(
                (
                    set_name,
                    source,
                    value.get("n"),
                    value.get("metric_n"),
                    _mapping(value.get("structural")).get("mean"),
                    _mapping(value.get("fixed")).get("mean"),
                    difference.get("mean"),
                    difference.get("sd"),
                    difference.get("median"),
                    difference.get("min"),
                    difference.get("max"),
                    _mapping(value.get("excluded_no_span")).get("n"),
                )
            )
    return "\n\n".join(
        (
            "\n".join(
                (
                    "## 가설 판정",
                    "",
                    "판정 문항 수는 H1·H2·H3 골드 test 21, H4 identifier 4, H7 합성 8·실제 6으로 고정했다.",
                    "",
                    _table(
                        ("가설", "결론", "n", "비교", "차이 [95% CI]", "원인·한계"),
                        rows,
                    ),
                )
            ),
            "\n".join(
                (
                    "### H3 출처별 기술 통계",
                    "",
                    "사전 등록 21문항 표와 분리한 출처별 기술 통계다.",
                    "",
                    _table(
                        (
                            "세트",
                            "출처",
                            "n",
                            "지표 n",
                            "구조 적중률",
                            "고정 적중률",
                            "차이 평균",
                            "sd",
                            "중앙값",
                            "최소",
                            "최대",
                            "구간 없어 제외",
                        ),
                        h3_rows or [(None,) * 12],
                    ),
                )
            ),
        )
    )


def _hypothesis_reason(value: Mapping[str, object]) -> str | None:
    note = value.get("note")
    if note:
        return str(note)
    statistic = _mapping(value.get("statistic"))
    verdict_value = value.get("verdict")
    low, high = statistic.get("ci_low"), statistic.get("ci_high")
    if not verdict_value:
        return None
    if verdict_value == "판정 보류(차이 입증 못 함)":
        return f"95% CI가 0을 포함; n={_display(value.get('n'))}"
    return f"95% CI={_interval(low, high)}; n={_display(value.get('n'))}"


def _auxiliary_section(latest: Mapping[str, object]) -> str:
    analysis = _mapping(latest.get("analysis"))
    return "\n\n".join(
        (
            "## 보조 분석(판정 아님)",
            _h2_breakdown_section(analysis.get("h2_breakdown")),
            _lexical_section(analysis.get("lexical_mismatch_subset")),
            _corpus_v2_section(analysis.get("corpus_v2_rule")),
            _temporal_section(analysis.get("temporal_prior_vs_current")),
            _parser_section(analysis.get("parser_comparison")),
        )
    )


def _h2_breakdown_section(value: object) -> str:
    payload = _mapping(value)
    rows = []
    for set_name in ("gold_test", "silver_test"):
        groups = _mapping(
            _mapping(payload.get(set_name)).get("by_source_and_answer_repeated")
        )
        for source in ("real", "synthetic"):
            source_groups = _mapping(groups.get(source))
            for repeated in ("repeated", "not_repeated"):
                stats = _mapping(source_groups.get(repeated))
                rows.append(
                    (
                        set_name,
                        source,
                        repeated,
                        stats.get("n"),
                        stats.get("mean"),
                        stats.get("sd"),
                        stats.get("median"),
                        stats.get("min"),
                        stats.get("max"),
                    )
                )
    return "\n".join(
        (
            "### H2 출처별·answer_repeated별 기술 통계",
            "",
            _table(
                (
                    "세트",
                    "출처",
                    "answer_repeated",
                    "n",
                    "차이 평균",
                    "sd",
                    "중앙값",
                    "최소",
                    "최대",
                ),
                rows,
            ),
        )
    )


def _lexical_section(value: object) -> str:
    payload = _mapping(value)
    rows = []
    for set_name in ("gold_test", "silver_test"):
        subset = _mapping(payload.get(set_name))
        rows.append(
            (
                set_name,
                subset.get("n"),
                _mapping(subset.get("bm25")).get("mean"),
                _mapping(subset.get("hybrid")).get("mean"),
                _mapping(subset.get("difference")).get("mean"),
            )
        )
    return "\n".join(
        (
            "### 어휘 불일치 부분집합",
            "",
            _table(("세트", "n", "BM25 NDCG@10", "hybrid NDCG@10", "차이"), rows),
        )
    )


def _corpus_v2_section(value: object) -> str:
    payload = _mapping(value)
    groups = _mapping(payload.get("group_sizes"))
    return "\n".join(
        (
            "### 코퍼스 v2 착수 규칙",
            "",
            _table(
                ("반복 아님 n", "반복 n", "차이 [95% CI]", "결정"),
                (
                    (
                        groups.get("not_repeated"),
                        groups.get("repeated"),
                        _ci_text(payload.get("statistic")),
                        payload.get("decision"),
                    ),
                ),
            ),
        )
    )


def _temporal_section(value: object) -> str:
    payload = _mapping(value)
    values = payload.get("rows")
    rows = []
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        for temporal_value in values:
            row = _mapping(temporal_value)
            rows.append(
                (
                    row.get("qid"),
                    row.get("split"),
                    row.get("current_doc_id"),
                    row.get("current_rank"),
                    row.get("prior_doc_id"),
                    row.get("prior_rank"),
                    row.get("prior_above_current"),
                )
            )
    if not rows:
        rows = [(None,) * 7]
    return "\n".join(
        (
            "### temporal 개정 전/현행 순위",
            "",
            _table(
                (
                    "문항",
                    "split",
                    "현행 문서",
                    "현행 순위",
                    "개정 전 문서",
                    "개정 전 순위",
                    "개정 전 우선",
                ),
                rows,
            ),
        )
    )


def _parser_section(value: object) -> str:
    payload = _mapping(value)
    label = payload.get("label")
    subsets = _mapping(payload.get("subsets"))
    descriptive_rows = []
    comparison_rows = []
    for subset_name in ("real_all_19", "real_test_13"):
        subset = _mapping(subsets.get(subset_name))
        conditions = _mapping(subset.get("conditions"))
        for condition in ("pypdf", "upstage", "composite"):
            metrics = _mapping(conditions.get(condition))
            descriptive_rows.append(
                (
                    subset_name,
                    condition,
                    subset.get("n"),
                    _mapping(metrics.get("recall10")).get("mean"),
                    _mapping(metrics.get("ndcg10")).get("mean"),
                )
            )
        comparisons = _mapping(subset.get("paired_comparisons"))
        for comparison, metrics_value in sorted(comparisons.items()):
            metrics = _mapping(metrics_value)
            comparison_rows.append(
                (
                    subset_name,
                    comparison,
                    _ci_text(metrics.get("recall10")),
                    _ci_text(metrics.get("ndcg10")),
                )
            )
    return "\n\n".join(
        (
            "\n".join(
                (
                    f"### 파서 {label or _PLACEHOLDER}",
                    "",
                    _table(
                        ("부분집합", "조건", "n", "Recall@10 평균", "NDCG@10 평균"),
                        descriptive_rows or [(None,) * 5],
                    ),
                )
            ),
            "\n".join(
                (
                    "짝 비교 CI(탐색적 비교이며 채택·기각을 붙이지 않음)",
                    "",
                    _table(
                        (
                            "부분집합",
                            "비교",
                            "Recall 차이 [95% CI]",
                            "NDCG 차이 [95% CI]",
                        ),
                        comparison_rows or [(None,) * 4],
                    ),
                )
            ),
        )
    )


def _run_history_section(
    records: Sequence[Mapping[str, object]],
    results: Sequence[Mapping[str, object]],
) -> str:
    history_rows = [
        (
            record.get("seq"),
            record.get("ran_at"),
            record.get("status"),
            record.get("rerun_reason"),
            record.get("previous_result_sha256"),
            record.get("result_sha256"),
        )
        for record in sorted(records, key=_sequence_number)
    ]
    if not history_rows:
        history_rows = [(None,) * 6]
    core_rows = []
    for result in sorted(results, key=_sequence_number):
        hypotheses = _mapping(_mapping(result.get("analysis")).get("hypotheses"))
        for hypothesis_id in ("H1", "H2", "H3", "H4", "H7"):
            hypothesis = _mapping(hypotheses.get(hypothesis_id))
            core_rows.append(
                (
                    result.get("seq"),
                    hypothesis_id,
                    hypothesis.get("verdict"),
                    _mapping(hypothesis.get("statistic")).get("mean_diff"),
                    _interval(
                        _mapping(hypothesis.get("statistic")).get("ci_low"),
                        _mapping(hypothesis.get("statistic")).get("ci_high"),
                    ),
                )
            )
    if not core_rows:
        core_rows = [(None,) * 5]
    return "\n\n".join(
        (
            "\n".join(
                (
                    "## 첫 실행과 재실행",
                    "",
                    _table(
                        ("seq", "시각", "상태", "사유", "이전 결과 해시", "결과 해시"),
                        history_rows,
                    ),
                )
            ),
            "\n".join(
                (
                    "### 실행별 핵심 결과",
                    "",
                    _table(("seq", "가설", "결론", "차이", "95% CI"), core_rows),
                )
            ),
        )
    )


def _dev_section(dev: Mapping[str, object]) -> str:
    values = dev.get("grid")
    rows = []
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        for value in values:
            row = _mapping(value)
            rows.append(
                (
                    row.get("method"),
                    row.get("candidate_chunks"),
                    row.get("bm25_weight"),
                    row.get("gold_dev_mean_ndcg10"),
                    row.get("silver_dev_mean_ndcg10"),
                    row.get("selection_score"),
                )
            )
    if not rows:
        rows = [(None,) * 6]
    selected = _mapping(dev.get("selected"))
    return "\n".join(
        (
            "## dev 튜닝",
            "",
            "그리드는 사전 등록한 18설정이다.",
            "",
            _table(
                (
                    "방식",
                    "후보 청크",
                    "BM25 가중치",
                    "골드 dev NDCG",
                    "실버 dev NDCG",
                    "선택 점수",
                ),
                rows,
            ),
            "",
            _table(
                ("선택 항목", "값"),
                (
                    ("방식", selected.get("method")),
                    ("후보 청크", selected.get("candidate_chunks")),
                    ("BM25 가중치", selected.get("bm25_weight")),
                    ("선택 점수", selected.get("selection_score")),
                ),
            ),
        )
    )


def _index_entry(index: Mapping[str, object], name: str) -> Mapping[str, object]:
    return _mapping(_mapping(index.get("indexes")).get(name))


def _sequence_number(value: Mapping[str, object]) -> int:
    seq = value.get("seq")
    return seq if isinstance(seq, int) and not isinstance(seq, bool) else -1


def _ci_text(value: object) -> str | None:
    statistic = _mapping(value)
    if not statistic:
        return None
    mean = statistic.get("mean_diff")
    interval = _interval(statistic.get("ci_low"), statistic.get("ci_high"))
    if mean is None and interval is None:
        return None
    return f"{_display(mean)} {interval or _PLACEHOLDER}"


def _interval(low: object, high: object) -> str | None:
    if low is None and high is None:
        return None
    return f"[{_display(low)}, {_display(high)}]"


def _join_fields(fields: Sequence[tuple[str, object]]) -> str | None:
    available = [(name, value) for name, value in fields if value is not None]
    if not available:
        return None
    return ", ".join(f"{name}={_display(value)}" for name, value in available)


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
    if value is None or value == "":
        return _PLACEHOLDER
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


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
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


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


def _load_results(path: Path) -> list[dict[str, object]]:
    if not path.is_dir():
        return []
    return [_load_optional_json(result) for result in sorted(path.glob("run-*.json"))]


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
        raise M5ReportError("generated 표지가 없거나 짝이 맞지 않습니다.")
    start, finish = text.index(BEGIN), text.index(END)
    if start >= finish:
        raise M5ReportError("generated 표지 순서가 잘못되었습니다.")
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
