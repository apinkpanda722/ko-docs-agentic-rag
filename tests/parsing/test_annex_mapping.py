"""Tests for deterministic pypdf-to-Upstage annex mapping."""

from collections.abc import Sequence

import pytest

from kodocs.parsing.annex_mapping import (
    annex_regions,
    article_region_text,
    map_annex_tables,
)
from kodocs.parsing.schema import (
    SCHEMA_VERSION,
    Block,
    ParsedDocument,
    ParsedTable,
    TableCell,
)


def _document(
    parser: str,
    blocks: Sequence[Block],
    *,
    tables: Sequence[ParsedTable] = (),
) -> ParsedDocument:
    return ParsedDocument(
        schema_version=SCHEMA_VERSION,
        parser=parser,
        parser_version="test",
        settings={},
        settings_hash="test-settings",
        input_path="data/raw/pdf/test.pdf",
        input_sha256="a" * 64,
        input_kind="pdf",
        page_count=2,
        blocks=tuple(blocks),
        tables=tuple(tables),
        elapsed_s=0.0,
    )


def _table(block_position: int, value: str = "값") -> ParsedTable:
    return ParsedTable(
        page=1,
        block_position=block_position,
        cells=(
            TableCell(row=0, col=0, row_span=1, col_span=1, text="항목"),
            TableCell(row=0, col=1, row_span=1, col_span=1, text="내용"),
            TableCell(row=1, col=0, row_span=1, col_span=1, text="가"),
            TableCell(row=1, col=1, row_span=1, col_span=1, text=value),
        ),
    )


def test_annex_regions_preserve_raw_blocks_and_article_text_excludes_them() -> None:
    document = _document(
        "pypdf",
        [
            Block(page=1, kind="text", text="규정명"),
            Block(page=1, kind="text", text="제1조(목적) 원문"),
            Block(page=1, kind="text", text="[별표 2] 수당표"),
            Block(page=1, kind="text", text="  정제하지 않을  내용  "),
            Block(page=1, kind="text", text="부 칙 <2026.1.1.>"),
            Block(page=2, kind="heading", text="〈별지 제2호의1 서식〉"),
            Block(page=2, kind="text", text="별지 원문"),
            Block(page=2, kind="text", text="제2조(재개) 본문"),
            Block(page=2, kind="text", text="〔별표3〕내용을 개정한다."),
            Block(page=2, kind="text", text="[서식 제3호]에 따른 신청"),
        ],
    )

    regions = annex_regions(document)

    assert [(region.annex_id, region.kind) for region in regions] == [
        ("별표2", "별표"),
        ("별지2의1", "별지"),
    ]
    assert regions[0].label == "[별표 2]"
    assert (regions[0].block_start, regions[0].block_end) == (2, 4)
    assert regions[0].text == "[별표 2] 수당표\n  정제하지 않을  내용  "
    assert regions[1].label == "〈별지 제2호의1 서식〉"
    assert regions[1].text == "〈별지 제2호의1 서식〉\n별지 원문"
    assert article_region_text(document) == (
        "규정명\n"
        "제1조(목적) 원문\n"
        "부 칙 <2026.1.1.>\n"
        "제2조(재개) 본문\n"
        "〔별표3〕내용을 개정한다.\n"
        "[서식 제3호]에 따른 신청"
    )


@pytest.mark.parametrize(
    ("label", "annex_id"),
    [
        ("[별표 2의 1]", "별표2의1"),
        ("[별지 제2호의2서식]", "별지2의2"),
        ("[별지 제1-1호서식]", "별지1의1"),
        ("【 서식 제3호 】", "서식3"),
        ("<부록 2>", "부록2"),
        ("[별표]", "별표"),
    ],
)
def test_annex_id_normalizes_numbering_variants(label: str, annex_id: str) -> None:
    document = _document(
        "pypdf",
        [
            Block(page=1, kind="text", text=label),
            Block(page=1, kind="text", text="원문"),
        ],
    )

    assert [region.annex_id for region in annex_regions(document)] == [annex_id]


def test_map_annex_tables_keeps_table_order_and_renders_raw_cell_grids() -> None:
    pypdf = _document(
        "pypdf",
        [
            Block(page=1, kind="text", text="[별표 1]"),
            Block(page=1, kind="text", text="pypdf fallback"),
            Block(page=2, kind="text", text="〈별지 제2호 서식〉"),
            Block(page=2, kind="text", text="표 없는 서식"),
            Block(page=2, kind="text", text="[부록 3]"),
            Block(page=2, kind="text", text="pypdf fallback"),
        ],
    )
    upstage = _document(
        "upstage",
        [
            Block(page=1, kind="table", text="본문 표"),
            Block(page=1, kind="heading", text="[별표 1] 제목"),
            Block(page=1, kind="table", text="parser markdown one"),
            Block(page=1, kind="table", text="parser markdown two"),
            Block(page=2, kind="heading", text="〈별지 제2호 서식〉"),
            Block(page=2, kind="text", text="표 없는 서식"),
            Block(page=2, kind="heading", text="[부록 3]"),
        ],
        tables=[_table(0, "본문"), _table(2, "첫째"), _table(3, "둘째")],
    )

    mapping = map_annex_tables(pypdf, upstage)

    assert list(mapping.matched) == ["별표1"]
    assert mapping.matched["별표1"].tables == tuple(upstage.tables[1:])
    assert mapping.matched["별표1"].text == (
        "[별표1]\n"
        "|항목|내용|\n|---|---|\n|가|첫째|\n\n"
        "|항목|내용|\n|---|---|\n|가|둘째|"
    )
    # 표가 없는 별지·서식은 정상이며 fallback으로 세지 않는다.
    assert mapping.fallback == ["부록3"]
    assert mapping.unmapped_tables == [0]


def test_mapping_stops_at_addendum_and_article_boundaries() -> None:
    pypdf = _document(
        "pypdf",
        [
            Block(page=1, kind="text", text="[별표 1]"),
            Block(page=2, kind="text", text="[별표 2]"),
        ],
    )
    upstage = _document(
        "upstage",
        [
            Block(page=1, kind="heading", text="[별표 1]"),
            Block(page=1, kind="table", text="one"),
            Block(page=1, kind="text", text="부칙 <2026.1.1.>"),
            Block(page=1, kind="table", text="outside"),
            Block(page=2, kind="heading", text="[별표 2]"),
            Block(page=2, kind="table", text="two"),
            Block(page=2, kind="text", text="제2조(재개) 본문"),
            Block(page=2, kind="table", text="outside again"),
        ],
        tables=[_table(1, "하나"), _table(3), _table(5, "둘"), _table(7)],
    )

    mapping = map_annex_tables(pypdf, upstage)

    assert list(mapping.matched) == ["별표1", "별표2"]
    assert mapping.unmapped_tables == [1, 3]


def test_reference_like_label_does_not_switch_the_current_annex() -> None:
    pypdf = _document(
        "pypdf",
        [Block(page=1, kind="text", text="[별표 1]"), Block(1, "text", "원문")],
    )
    upstage = _document(
        "upstage",
        [
            Block(page=1, kind="heading", text="[별표 1]"),
            Block(page=1, kind="table", text="one"),
            Block(page=1, kind="text", text="[서식 제3호]에 따른 항목"),
            Block(page=1, kind="table", text="two"),
            Block(page=1, kind="text", text="〔별표2〕내용을 개정한다."),
            Block(page=1, kind="table", text="three"),
        ],
        tables=[_table(1, "하나"), _table(3, "둘"), _table(5, "셋")],
    )

    mapping = map_annex_tables(pypdf, upstage)

    assert mapping.matched["별표1"].tables == upstage.tables
    assert mapping.unmapped_tables == []


def test_repeated_label_groups_are_combined_in_original_table_order() -> None:
    pypdf = _document(
        "pypdf",
        [Block(page=1, kind="text", text="[별표 1]"), Block(1, "text", "원문")],
    )
    upstage = _document(
        "upstage",
        [
            Block(page=1, kind="heading", text="[별표 1]"),
            Block(page=1, kind="table", text="one"),
            Block(page=2, kind="heading", text="[별표 1]"),
            Block(page=2, kind="table", text="two"),
        ],
        tables=[_table(1, "하나"), _table(3, "둘")],
    )

    mapping = map_annex_tables(pypdf, upstage)

    assert mapping.matched["별표1"].tables == upstage.tables


def test_invalid_table_is_isolated_as_unmapped_and_annex_falls_back() -> None:
    pypdf = _document(
        "pypdf",
        [Block(page=1, kind="text", text="[별표 1]"), Block(1, "text", "원문")],
    )
    invalid = ParsedTable(
        page=1,
        block_position=1,
        cells=(),
        invalid_reason="cell_overlap: ValueError",
    )
    upstage = _document(
        "upstage",
        [
            Block(page=1, kind="heading", text="[별표 1]"),
            Block(page=1, kind="table", text="invalid"),
        ],
        tables=[invalid],
    )

    mapping = map_annex_tables(pypdf, upstage)

    assert mapping.matched == {}
    assert mapping.fallback == ["별표1"]
    assert mapping.unmapped_tables == [0]


def test_form_with_missing_upstage_label_is_a_fallback_not_normal_no_table() -> None:
    pypdf = _document(
        "pypdf",
        [Block(page=1, kind="text", text="[별지 제1호서식]"), Block(1, "text", "원문")],
    )
    upstage = _document(
        "upstage",
        [Block(page=1, kind="text", text="라벨이 누락된 출력")],
    )

    mapping = map_annex_tables(pypdf, upstage)

    assert mapping.matched == {}
    assert mapping.fallback == ["별지1"]
