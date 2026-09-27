"""Tests for comparison-only legal text normalization."""

import unicodedata

from kodocs.eval.text_normalize import (
    EDITORIAL_PATTERNS,
    normalize,
    raw_compare_form,
    strip_editorial,
)


def test_raw_compare_form_only_applies_nfc_and_trims_outer_whitespace() -> None:
    decomposed = unicodedata.normalize("NFD", "가")

    assert raw_compare_form(f" \t{decomposed} ① “문장”\n") == "가 ① “문장”"


def test_normalize_applies_n1_to_n5_in_order_with_nfkc_in_n3() -> None:
    source = " 제1조\u3000(목적)\n“이\t규정”은 ①ㆍA・B･C‧D <개정 2021. 3. 16.> "

    # NFKC는 원문자 번호를 숫자로 바꾼다(①→1). 양쪽 텍스트에 똑같이 적용된다.
    assert normalize(source) == '제1조(목적)"이규정"은1·A·B·C·D'


def test_strip_editorial_removes_all_supported_balanced_annotations() -> None:
    source = (
        "본문 <개정 2021. 3. 16., 제2조> <신설 2020. 1. 1.> "
        "<삭제 2019. 2. 3.> <종전 2018. 4. 5.> "
        "[본조신설 2021. 3. 16.] [전문개정 2022. 1. 1.] "
        "[종전 제3조, 2020. 2. 2.] [제목개정 2023. 4. 5.] "
        "[시행일 2024. 6. 7.] 끝"
    )

    stripped, count = strip_editorial(source)

    assert len(EDITORIAL_PATTERNS) >= 2
    assert count == 9
    assert " ".join(stripped.split()) == "본문 끝"


def test_strip_editorial_keeps_unbalanced_or_non_metadata_brackets() -> None:
    source = "<개정 2021. 3. 16. [시행일 2022. 1. 1. <개정 취지를 설명>"

    assert strip_editorial(source) == (source, 0)


def test_strip_editorial_accepts_compact_empty_and_mixed_history_variants() -> None:
    source = (
        "제3조<삭제> 제4조<개정2021. 3. 16.> 제5조<신설 2015. 11. 3., 삭제 2017. 8. 9.>"
    )

    assert strip_editorial(source) == ("제3조 제4조 제5조", 3)


def test_normalize_unifies_quote_variants_and_all_unicode_whitespace() -> None:
    source = "“가”„나‟ ‘다’‚라‛\u00a0마\u3000바\r\n사"

    assert normalize(source) == '"가""나""다""라"마바사'


def test_normalize_unifies_every_quote_variant_to_one_character() -> None:
    variants = ['"', "'", "“", "”", "‘", "’", "`", "´", "＂", "＇"]

    assert {normalize(f"가{quote}나") for quote in variants} == {'가"나'}


def test_normalize_unifies_every_middle_dot_variant_to_one_character() -> None:
    variants = ["ㆍ", "·", "․", "‧", "∙", "・", "･"]

    assert {normalize(f"장{dot}절") for dot in variants} == {"장·절"}


def test_normalize_applies_nfkc_to_compatibility_forms() -> None:
    assert normalize("목적：（가）　１２") == "목적:(가)12"


def test_nfkc_keeps_hangul_syllables_digits_and_article_heads() -> None:
    source = "제12조의3(정의) 가나다라마바사 힣 0123456789 ①항 제2호"

    assert normalize(source) == "제12조의3(정의)가나다라마바사힣01234567891항제2호"


def test_raw_compare_form_does_not_apply_nfkc_or_unification() -> None:
    assert raw_compare_form(" 목적：ㆍ“가” ① ") == "목적：ㆍ“가” ①"
