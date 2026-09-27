"""Tests for deterministic parser table-cell scoring."""

import io
import json
from pathlib import Path

import pytest

from kodocs.eval.table_score import run, score_table
from kodocs.ingest.manifest import sha256_file
from kodocs.parsing.schema import (
    SCHEMA_VERSION,
    ParsedDocument,
    ParsedTable,
    TableCell,
    output_path,
    to_json,
)


def _document(
    parser: str,
    input_sha256: str,
    tables: tuple[ParsedTable, ...],
) -> ParsedDocument:
    return ParsedDocument(
        schema_version=SCHEMA_VERSION,
        parser=parser,
        parser_version="test",
        settings={},
        settings_hash="settings",
        input_path="data/raw/pdf/test.pdf",
        input_sha256=input_sha256,
        input_kind="pdf",
        page_count=1,
        blocks=(),
        tables=tables,
        elapsed_s=0.0,
    )


def _table(
    *cells: TableCell,
    invalid_reason: str | None = None,
) -> ParsedTable:
    return ParsedTable(
        page=1,
        block_position=0,
        cells=tuple(cells),
        invalid_reason=invalid_reason,
    )


def _write_document(root: Path, document: ParsedDocument) -> None:
    path = output_path(root, document.parser, document.input_sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_json(document), encoding="utf-8")


def _golden_markdown(pdf_sha256: str, cell: str) -> str:
    return (
        "---\n"
        'rule_no: "47161"\n'
        f'source_sha256: "{"a" * 64}"\n'
        f'pdf_sha256: "{pdf_sha256}"\n'
        'section: "Contents/section0.xml"\n'
        "table_ordinal: 0\n"
        'annex_label: "[별표 1] 제목"\n'
        'annex_kind: "별표"\n'
        "shape: [1, 1]\n"
        "nested: false\n"
        'reviewed_by: "reviewer"\n'
        'reviewed_on: "2026-09-26"\n'
        "---\n"
        f"|{cell}|\n|---|\n"
    )


def _write_golden(
    root: Path,
    golden_id: str,
    pdf_sha256: str,
    cell: str,
) -> dict[str, object]:
    path = root / "data/golden/tables" / f"{golden_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_golden_markdown(pdf_sha256, cell), encoding="utf-8")
    return {
        "id": golden_id,
        "rule_no": "47161",
        "source_sha256": "a" * 64,
        "pdf_sha256": pdf_sha256,
        "section": "Contents/section0.xml",
        "table_ordinal": 0,
        "annex_label": "[별표 1] 제목",
        "shape": [1, 1],
        "golden_file_sha256": sha256_file(path),
        "reviewed_by": "reviewer",
        "reviewed_on": "2026-09-26",
    }


def test_score_table_selects_most_exact_overlap_and_scores_golden_denominator() -> None:
    parsed = _document(
        "docling",
        "a" * 64,
        (
            _table(TableCell(0, 0, 1, 1, "다름")),
            _table(
                TableCell(0, 0, 1, 1, " 가 "),
                TableCell(0, 1, 1, 1, "나"),
                TableCell(1, 0, 1, 1, "다"),
            ),
        ),
    )

    score = score_table([["가", "나"], ["다", "라"]], parsed)

    assert score.status == "ok"
    assert score.applicable is True
    assert score.best_table_index == 1
    assert score.shape_match is True
    assert score.cell_exact_rate == pytest.approx(0.75)
    assert score.cell_sim_mean is not None
    assert score.cell_sim_mean >= 0.75
    assert score.invalid_candidates == 0


def test_invalid_overlap_is_isolated_and_normal_table_is_still_scored() -> None:
    overlap = _table(
        TableCell(0, 0, 1, 2, "겹침"),
        TableCell(0, 1, 1, 1, "충돌"),
    )
    normal = _table(TableCell(0, 0, 1, 1, "정답"))
    parsed = _document("upstage", "b" * 64, (overlap, normal))

    score = score_table([["정답"]], parsed)

    assert score.status == "ok"
    assert score.best_table_index == 1
    assert score.cell_exact_rate == 1.0
    assert score.invalid_candidates == 1
    assert score.reason is not None and "overlap" in score.reason


def test_all_invalid_scores_zero_with_reason_and_pypdf_is_not_applicable() -> None:
    invalid = _table(
        TableCell(0, 0, 1, 1, "비밀 셀"),
        invalid_reason="cell_overlap: bad coordinates",
    )

    score = score_table([["정답"]], _document("docling", "c" * 64, (invalid,)))
    pypdf = score_table([["정답"]], _document("pypdf", "c" * 64, ()))

    assert score.status == "all_invalid"
    assert score.cell_exact_rate == score.cell_sim_mean == 0.0
    assert score.shape_match is False
    assert score.invalid_candidates == 1
    assert score.reason == "cell_overlap: bad coordinates"
    assert pypdf.status == "no_tables"
    assert pypdf.applicable is False
    assert pypdf.cell_exact_rate is pypdf.cell_sim_mean is None


def test_run_continues_after_all_invalid_excludes_pypdf_and_writes_no_cell_text(
    tmp_path: Path,
) -> None:
    first_sha = "1" * 64
    second_sha = "2" * 64
    first = _write_golden(tmp_path, "first", first_sha, "첫 비밀 셀")
    second = _write_golden(tmp_path, "second", second_sha, "둘째 비밀 셀")
    manifest_path = tmp_path / "data/manifest/table_golden.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps({"tables": [first, second]}, ensure_ascii=False), encoding="utf-8"
    )
    invalid = _table(
        TableCell(0, 0, 1, 2, "예측 비밀"),
        TableCell(0, 1, 1, 1, "충돌"),
    )
    _write_document(tmp_path, _document("docling", first_sha, (invalid,)))
    _write_document(
        tmp_path,
        _document(
            "docling",
            second_sha,
            (_table(TableCell(0, 0, 1, 1, "둘째 비밀 셀")),),
        ),
    )
    out = io.StringIO()

    payload = run(tmp_path, out=out)

    docling = [row for row in payload["scores"] if row["parser"] == "docling"]
    assert [row["status"] for row in docling] == ["all_invalid", "ok"]
    assert docling[0]["cell_exact_rate"] == 0.0
    assert docling[1]["cell_exact_rate"] == 1.0
    assert payload["parsers"]["docling"]["invalid_candidates"] == 1
    assert payload["parsers"]["docling"]["cell_exact_rate_mean"] == 0.5
    assert payload["parsers"]["pypdf"]["cell_exact_rate_mean"] is None
    assert "해당 없음(표 추출 기능 없음)" in out.getvalue()
    score_path = tmp_path / "data/manifest/table_scores.json"
    serialized = score_path.read_text(encoding="utf-8")
    assert "첫 비밀 셀" not in serialized
    assert "둘째 비밀 셀" not in serialized
    assert "예측 비밀" not in serialized
    assert "첫 비밀 셀" not in out.getvalue()


def test_run_marks_changed_golden_without_scoring_and_missing_or_no_tables_are_zero(
    tmp_path: Path,
) -> None:
    changed_sha = "3" * 64
    no_tables_sha = "4" * 64
    missing_sha = "5" * 64
    changed = _write_golden(tmp_path, "changed", changed_sha, "변경 전")
    no_tables = _write_golden(tmp_path, "no-tables", no_tables_sha, "정답")
    missing = _write_golden(tmp_path, "missing", missing_sha, "정답")
    changed_path = tmp_path / "data/golden/tables/changed.md"
    changed_path.write_text(_golden_markdown(changed_sha, "변경 후"), encoding="utf-8")
    manifest_path = tmp_path / "data/manifest/table_golden.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps({"tables": [changed, no_tables, missing]}, ensure_ascii=False),
        encoding="utf-8",
    )
    _write_document(tmp_path, _document("upstage", no_tables_sha, ()))

    payload = run(tmp_path, out=io.StringIO())

    upstage = {
        row["golden_id"]: row for row in payload["scores"] if row["parser"] == "upstage"
    }
    assert upstage["changed"]["status"] == "golden_changed"
    assert upstage["changed"]["cell_exact_rate"] is None
    assert upstage["no-tables"]["status"] == "no_tables"
    assert upstage["no-tables"]["cell_exact_rate"] == 0.0
    assert upstage["missing"]["status"] == "parse_missing"
    assert upstage["missing"]["cell_sim_mean"] == 0.0
