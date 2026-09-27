"""Tests for parser-neutral HWPX table drafts and golden indexing."""

import io
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

import pytest

from kodocs.eval.table_drafts import extract_hwpx_tables
from kodocs.eval.table_drafts import main as drafts_main
from kodocs.eval.table_golden import (
    TableGoldenError,
    index_golden,
)
from kodocs.eval.table_golden import (
    main as golden_main,
)
from kodocs.ingest.manifest import sha256_file
from kodocs.parsing.tables import cells_to_grid, grid_to_markdown

HP_NS = "http://www.hancom.co.kr/hwpml/2011/paragraph"


class FlushTrackingIO(io.StringIO):
    """Remember explicit progress flushes."""

    def __init__(self) -> None:
        super().__init__()
        self.flush_count = 0

    def flush(self) -> None:
        self.flush_count += 1
        super().flush()


def _cell(
    row: int,
    col: int,
    text: str,
    *,
    row_span: int = 1,
    col_span: int = 1,
    extra: str = "",
) -> str:
    return f"""
    <hp:tc>
      <hp:cellAddr rowAddr="{row}" colAddr="{col}"/>
      <hp:cellSpan rowSpan="{row_span}" colSpan="{col_span}"/>
      <hp:subList><hp:p><hp:run><hp:t>{text}</hp:t></hp:run></hp:p>{extra}</hp:subList>
    </hp:tc>
    """


def _table(cells: str) -> str:
    return f"<hp:tbl><hp:tr>{cells}</hp:tr></hp:tbl>"


def _write_hwpx(
    path: Path, section: str, *, mimetype: bytes = b"application/hwp+zip"
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", mimetype)
        archive.writestr("Contents/section0.xml", section)


def _section(body: str) -> str:
    return f'<hp:sec xmlns:hp="{HP_NS}">{body}</hp:sec>'


def _golden_markdown(
    *,
    rule_no: str = "47161",
    pdf_sha256: str = "b" * 64,
    reviewed: bool = True,
    cell_text: str = "검수 셀 원문",
) -> str:
    review = 'reviewed_by: "reviewer"\nreviewed_on: "2026-09-26"\n' if reviewed else ""
    return (
        "---\n"
        f'rule_no: "{rule_no}"\n'
        f'source_sha256: "{"a" * 64}"\n'
        f'pdf_sha256: "{pdf_sha256}"\n'
        'section: "Contents/section0.xml"\n'
        "table_ordinal: 0\n"
        'annex_label: "[별표 1] 제목"\n'
        'annex_kind: "별표"\n'
        "shape: [1, 1]\n"
        "nested: false\n"
        'draft_generator: "kodocs.eval.table_drafts"\n'
        f"{review}"
        "---\n"
        f"|{cell_text}|\n|---|\n"
    )


def test_extract_hwpx_uses_signature_coordinates_spans_and_step1_grid_logic(
    tmp_path: Path,
) -> None:
    path = tmp_path / "47161" / "source.not-hwpx"
    cells = "".join(
        [
            _cell(0, 0, "병합", row_span=2, col_span=2),
            _cell(
                0, 2, "첫 문단", extra="<hp:p><hp:run><hp:t>둘째</hp:t></hp:run></hp:p>"
            ),
            _cell(1, 2, "아래"),
        ]
    )
    _write_hwpx(
        path,
        _section(
            "<hp:p><hp:run><hp:t>[별표 1] 단말기 단계별 보안관리 관련 규정</hp:t></hp:run></hp:p>"
            + _table(cells)
        ),
    )

    drafts = extract_hwpx_tables(path)

    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.rule_no == "47161"
    assert draft.source_sha256 == sha256_file(path)
    assert draft.pdf_sha256 is None
    assert draft.section == "Contents/section0.xml"
    assert draft.table_ordinal == 0
    assert draft.annex_label == "[별표 1] 단말기 단계별 보안관리 관련 규정"
    assert draft.annex_kind == "별표"
    assert (draft.rows, draft.cols, draft.nested) == (2, 3, False)
    expected = [["병합", "병합", "첫 문단 둘째"], ["병합", "병합", "아래"]]
    assert cells_to_grid(draft.cells) == expected
    assert grid_to_markdown(cells_to_grid(draft.cells)) == (
        "|병합|병합|첫 문단 둘째|\n|---|---|---|\n|병합|병합|아래|"
    )


def test_extract_hwpx_includes_nested_table_text_and_marks_warning(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rule" / "nested.bin"
    nested = _table(_cell(0, 0, "중첩 내용"))
    outer = _table(_cell(0, 0, "바깥", extra=nested))
    _write_hwpx(path, _section(outer))

    drafts = extract_hwpx_tables(path)

    assert len(drafts) == 1  # 중첩 표는 최상위 표 순번에 포함하지 않는다.
    assert drafts[0].nested is True
    assert cells_to_grid(drafts[0].cells) == [["바깥 중첩 내용"]]


def test_extract_hwpx_exposes_actual_bad_zip_and_xml_parse_errors(
    tmp_path: Path,
) -> None:
    broken_zip = tmp_path / "broken.hwpx"
    broken_zip.write_bytes(b"PK\x03\x04broken")
    broken_xml = tmp_path / "broken-xml.hwpx"
    _write_hwpx(broken_xml, "<hp:sec>")

    with pytest.raises(BadZipFile):
        extract_hwpx_tables(broken_zip)
    with pytest.raises(Exception) as caught:
        extract_hwpx_tables(broken_xml)
    assert type(caught.value).__name__ == "ParseError"


def test_drafts_cli_continues_after_bad_file_writes_safe_frontmatter_and_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "data/raw/alio/47161/source.bin"
    _write_hwpx(
        source,
        _section(
            "<hp:p><hp:run><hp:t>【 별표 1 】 제목</hp:t></hp:run></hp:p>"
            + _table(_cell(0, 0, "초안 원문"))
        ),
    )
    bad = tmp_path / "data/raw/alio/99999/bad.hwpx"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"PK\x03\x04broken")
    pdf = tmp_path / "data/raw/pdf/47161.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"pdf bytes")
    target_path = tmp_path / "data/manifest/parse_targets.json"
    target_path.parent.mkdir(parents=True)
    target_path.write_text(
        json.dumps(
            {
                "targets": [
                    {
                        "rule_no": "47161",
                        "source_kind": "hwpx",
                        "source_path": source.relative_to(tmp_path).as_posix(),
                        "source_sha256": sha256_file(source),
                        "pdf_path": pdf.relative_to(tmp_path).as_posix(),
                    },
                    {
                        "rule_no": "99999",
                        "source_kind": "hwpx",
                        "source_path": bad.relative_to(tmp_path).as_posix(),
                        "source_sha256": "f" * 64,
                        "pdf_path": None,
                    },
                    {"rule_no": "ignored", "source_kind": "hwp5"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    output = FlushTrackingIO()
    monkeypatch.setattr("sys.stdout", output)

    exit_code = drafts_main(["--root", str(tmp_path)])

    assert exit_code == 1
    assert output.flush_count == 2
    assert "47161: 1개 표" in output.getvalue()
    assert "99999: 실패 BadZipFile" in output.getvalue()
    draft_path = tmp_path / "data/interim/table_drafts/47161/0-00.md"
    written = draft_path.read_text(encoding="utf-8")
    assert f'pdf_sha256: "{sha256_file(pdf)}"' in written
    assert 'annex_kind: "별표"' in written
    assert "shape: [1, 1]" in written
    assert "|초안 원문|" in written
    candidates = (tmp_path / "data/interim/table_drafts/candidates.md").read_text(
        encoding="utf-8"
    )
    assert "47161" in candidates
    assert "초안 원문" not in candidates


def test_index_golden_records_only_selection_metadata_and_warns_missing_review(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "data/golden/tables"
    directory.mkdir(parents=True)
    reviewed = directory / "approved.md"
    reviewed.write_text(_golden_markdown(), encoding="utf-8")
    unreviewed = directory / "needs-review.md"
    unreviewed.write_text(
        _golden_markdown(rule_no="52921", reviewed=False, cell_text="다른 비밀 셀"),
        encoding="utf-8",
    )

    with pytest.warns(UserWarning, match="reviewed_by.*reviewed_on"):
        payload = index_golden(tmp_path)

    assert [item["id"] for item in payload["tables"]] == [
        "approved",
        "needs-review",
    ]
    first = payload["tables"][0]
    assert first == {
        "id": "approved",
        "rule_no": "47161",
        "source_sha256": "a" * 64,
        "pdf_sha256": "b" * 64,
        "section": "Contents/section0.xml",
        "table_ordinal": 0,
        "annex_label": "[별표 1] 제목",
        "shape": [1, 1],
        "golden_file_sha256": sha256_file(reviewed),
        "reviewed_by": "reviewer",
        "reviewed_on": "2026-09-26",
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "검수 셀 원문" not in serialized
    assert "다른 비밀 셀" not in serialized


def test_table_golden_cli_writes_manifest_and_empty_directory_fails_with_instructions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "data/golden/tables"
    directory.mkdir(parents=True)

    with pytest.raises(TableGoldenError, match="make table-drafts.*검수.*복사"):
        index_golden(tmp_path)
    assert golden_main(["--root", str(tmp_path)]) == 1
    assert "make table-drafts" in capsys.readouterr().err

    (directory / "one.md").write_text(_golden_markdown(), encoding="utf-8")
    assert golden_main(["--root", str(tmp_path)]) == 0
    manifest = json.loads(
        (tmp_path / "data/manifest/table_golden.json").read_text(encoding="utf-8")
    )
    assert manifest["tables"][0]["id"] == "one"
    assert "검수 셀 원문" not in json.dumps(manifest, ensure_ascii=False)
