"""Tests for deterministic, parser-independent article splitting."""

from collections.abc import Sequence

import pytest

import kodocs.parsing.articles as articles_module
from kodocs.parsing.articles import law_to_lines, split_document, split_text
from kodocs.parsing.schema import SCHEMA_VERSION, Block, ParsedDocument


def _document(
    blocks: Sequence[Block],
    *,
    page_count: int | None = None,
    parser: str = "test-parser",
) -> ParsedDocument:
    return ParsedDocument(
        schema_version=SCHEMA_VERSION,
        parser=parser,
        parser_version="1.0",
        settings={},
        settings_hash="test-settings",
        input_path="data/raw/pdf/test.pdf",
        input_sha256="a" * 64,
        input_kind="pdf",
        page_count=page_count,
        blocks=tuple(blocks),
        tables=(),
        elapsed_s=0.0,
    )


def test_fp_043_counts_all_main_articles_without_reordering_or_normalizing() -> None:
    result = split_text(
        [
            (None, "규정명"),
            (1, "제1조 (목적) 이 규정은 원문을 보존한다."),
            (1, "제7조제3항에 따라 한다."),
            (2, "제3조【적용 범위】 적용 범위이다."),
            (2, "제3조의2 삭제 <2021. 3. 16.>"),
            (3, "제2조（재등장） 번호가 줄어든다."),
            (3, "제2조(중복) 중복 번호도 남긴다."),
        ]
    )

    assert [(item.number, item.branch) for item in result.articles] == [
        (1, 0),
        (3, 0),
        (3, 2),
        (2, 0),
        (2, 0),
    ]
    assert result.articles[0].title == "목적"
    assert result.articles[0].text == (
        "제1조 (목적) 이 규정은 원문을 보존한다.\n제7조제3항에 따라 한다."
    )
    assert result.articles[1].title == "적용 범위"
    assert result.articles[2].deleted is True
    assert result.articles[2].title is None
    assert result.articles[3].title == "재등장"
    assert result.articles[0].first_page == 1
    assert result.preamble == "규정명"


def test_fp_043_counting_rule_is_documented_in_the_module() -> None:
    docstring = articles_module.__doc__ or ""

    assert "FP-043" in docstring
    assert "제N조의M" in docstring
    assert "삭제" in docstring
    assert "부칙" in docstring
    assert "별표" in docstring


def test_boundary_regular_expressions_are_module_constants() -> None:
    assert articles_module.ARTICLE_RE.match("제1조 (목적) 본문")
    assert articles_module.ARTICLE_RE.match("제2조【정의】 본문")
    assert articles_module.ARTICLE_RE.match("제3조 삭제")
    assert not articles_module.ARTICLE_RE.match("제7조제3항에 따라 한다.")

    assert articles_module.HEADING_RE.match("제1장총칙")
    assert not articles_module.HEADING_RE.match("제1장의2 세부사항")
    assert not articles_module.HEADING_RE.match(f"{' ' * 40}제1장 총칙")
    assert articles_module.ADDENDUM_RE.match(" 부 칙 <제1호>")
    assert articles_module.ANNEX_RE.match("【 별표 1 】 수당표")


def test_chapter_section_and_subsection_headings_are_excluded_from_articles() -> None:
    result = split_text(
        [
            (1, "여비규정"),
            (1, "제정 2020. 1. 1."),
            (1, "제1장 총칙"),
            (1, "제1조(목적) 본문"),
            (1, "제2절 운영"),
            (1, "이 줄은 조문이 아니라 새 절의 안내문이다."),
            (2, "제2조(절차) 절차"),
            (2, "제3관보칙"),
            (2, "제3조(보칙) 보칙"),
        ]
    )

    assert result.preamble == "여비규정\n제정 2020. 1. 1."
    assert result.headings == ("제1장 총칙", "제2절 운영", "제3관보칙")
    assert tuple(item.text for item in result.articles) == (
        "제1조(목적) 본문",
        "제2조(절차) 절차",
        "제3조(보칙) 보칙",
    )


def test_each_addendum_has_its_own_articles_with_numbering_restarted() -> None:
    result = split_text(
        [
            (1, "제1조(본문) 본문 조문"),
            (2, "부칙 <제9999호, 2021.03.16.>"),
            (2, "공포한 날부터 시행한다."),
            (2, "제1조(시행일) 이 규정은 즉시 시행한다."),
            (2, "경과 문구"),
            (3, "부 칙 <제10000호, 2022.01.01.>"),
            (3, "제1조 (시행일) 이 규정은 2월 1일부터 시행한다."),
        ]
    )

    assert [(item.number, item.branch) for item in result.articles] == [(1, 0)]
    assert len(result.addenda) == 2
    assert result.addenda[0].heading == "부칙 <제9999호, 2021.03.16.>"
    assert result.addenda[0].text == (
        "부칙 <제9999호, 2021.03.16.>\n"
        "공포한 날부터 시행한다.\n"
        "제1조(시행일) 이 규정은 즉시 시행한다.\n"
        "경과 문구"
    )
    assert [(item.number, item.first_page) for item in result.addenda[0].articles] == [
        (1, 2)
    ]
    assert [item.number for item in result.addenda[1].articles] == [1]


@pytest.mark.parametrize(
    ("marker", "kind", "label", "number"),
    [
        ("[별표 1] 수당표", "별표", "[별표 1]", "1"),
        ("【 별지 제1호서식 】 신청서", "별지", "【 별지 제1호서식 】", "1"),
        ("<서 식 2의 3> 양식", "서식", "<서 식 2의 3>", "2의3"),
        ("〔 부 록 〕 참고", "부록", "〔 부 록 〕", None),
    ],
)
def test_annex_bracket_variants_start_an_excluded_region(
    marker: str,
    kind: str,
    label: str,
    number: str | None,
) -> None:
    result = split_text(
        [
            (1, "제1조(목적) 본문"),
            (1, "별표 1과 같다."),
            (2, marker),
            (2, "이 줄은 별표 내용이다."),
            (2, "제9장 별표 안의 제목형 텍스트"),
            (3, "제2조(재개) 다음 조문"),
        ]
    )

    assert tuple(item.text for item in result.articles) == (
        "제1조(목적) 본문\n별표 1과 같다.",
        "제2조(재개) 다음 조문",
    )
    assert result.annexes == (
        articles_module.AnnexMarker(
            kind=kind,
            label=label,
            number=number,
            page=2,
            block_index=2,
        ),
    )
    assert result.headings == ()


def test_split_document_drops_labeled_repeated_and_page_number_noise() -> None:
    blocks: list[Block] = [
        Block(page=1, kind="header", text="명시 머리말\n명시 머리말 둘"),
    ]
    for page in range(1, 4):
        blocks.extend(
            [
                Block(page=page, kind="text", text="반복 머리말"),
                Block(page=page, kind="text", text=f"제{page}조(제목) 본문 {page}"),
                Block(
                    page=page, kind="table", text=f"|항목|값|\n|---|---|\n|쪽|{page}|"
                ),
                Block(page=page, kind="text", text="반복 꼬리말"),
                Block(
                    page=page,
                    kind="text",
                    text=("12", "2 / 3", "- 3 -")[page - 1],
                ),
            ]
        )

    result = split_document(_document(blocks, page_count=3))

    assert result.dropped_noise == 11
    assert len(result.articles) == 3
    assert result.articles[0].text == (
        "제1조(제목) 본문 1\n|항목|값|\n|---|---|\n|쪽|1|"
    )
    assert all("반복" not in item.text for item in result.articles)


def test_boundary_lines_are_not_dropped_even_when_parser_labels_them_noise() -> None:
    document = _document(
        [
            Block(page=1, kind="header", text="제1조(목적) 보존"),
            Block(page=2, kind="footer", text="부칙 <제1호, 2020.01.01.>"),
            Block(page=3, kind="page_number", text="【 별표 1 】"),
        ],
        page_count=3,
    )

    result = split_document(document)

    assert [item.text for item in result.articles] == ["제1조(목적) 보존"]
    assert len(result.addenda) == 1
    assert len(result.annexes) == 1
    assert result.dropped_noise == 0


def test_law_to_lines_reaches_the_structured_law_upper_bound_exactly() -> None:
    law = {
        "articles": [
            "제1장 총칙",
            "제1조 (목적) 이 규정은 목적을 정한다. <개정 2021. 3. 16.>",
            "제2조의2【특례】 특례를 정한다.",
            "제3조 삭제 <2022. 1. 1.>",
        ],
        "addenda": [
            {
                "date": "2021-03-16",
                "text": (
                    "부칙 <제9999호, 2021.03.16.>\n"
                    "제1조(시행일) 이 규정은 공포한 날부터 시행한다."
                ),
            },
            {
                "date": "2022-01-01",
                "text": (
                    "부칙 <제10000호, 2022.01.01.>\n"
                    "제1조(시행일) 이 규정은 2022년 2월 1일부터 시행한다."
                ),
            },
        ],
        "annexes": [
            {"number": "0001", "branch": "00", "kind": "별표", "title": "수당표"},
            {
                "number": "0002",
                "branch": "01",
                "kind": "별지",
                "title": "신청서",
            },
        ],
    }

    result = split_text(law_to_lines(law))

    expected_articles = tuple(law["articles"][1:])
    assert [(item.number, item.branch) for item in result.articles] == [
        (1, 0),
        (2, 2),
        (3, 0),
    ]
    assert tuple(item.text for item in result.articles) == expected_articles
    assert len(result.addenda) == len(law["addenda"]) == 2
    assert [[item.number for item in block.articles] for block in result.addenda] == [
        [1],
        [1],
    ]
    assert [(item.kind, item.number) for item in result.annexes] == [
        ("별표", "1"),
        ("별지", "2의1"),
    ]
