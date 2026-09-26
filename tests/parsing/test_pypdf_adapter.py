"""Tests for the baseline pypdf parsing adapter."""

from importlib.metadata import version
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
)

from kodocs.ingest.manifest import sha256_file
from kodocs.parsing.pypdf_adapter import PARSER_NAME, parse_pdf
from kodocs.parsing.schema import SCHEMA_VERSION, Block, ParseError, settings_hash


def _write_blank_pdf(path: Path, pages: int) -> None:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    with path.open("wb") as output:
        writer.write(output)


def _write_text_pdf(path: Path) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference})}
    )
    contents = DecodedStreamObject()
    contents.set_data(
        b"BT /F1 12 Tf 20 180 Td (  first  value  ) Tj 0 -20 Td (second line) Tj ET"
    )
    page[NameObject("/Contents")] = writer._add_object(contents)
    with path.open("wb") as output:
        writer.write(output)


def test_parse_blank_pdf_records_reproducible_metadata(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    pdf_path = root / "data/raw/pdf/blank.pdf"
    pdf_path.parent.mkdir(parents=True)
    _write_blank_pdf(pdf_path, pages=2)
    expected_settings = {"extraction_mode": "plain"}

    document = parse_pdf(pdf_path, root=root)

    assert document.schema_version == SCHEMA_VERSION
    assert document.parser == PARSER_NAME == "pypdf"
    assert document.parser_version == version("pypdf")
    assert document.settings == expected_settings
    assert document.settings_hash == settings_hash(expected_settings)
    assert document.input_path == "data/raw/pdf/blank.pdf"
    assert document.input_sha256 == sha256_file(pdf_path)
    assert document.input_kind == "pdf"
    assert document.page_count == 2
    assert document.blocks == ()
    assert document.tables == ()
    assert document.elapsed_s >= 0


def test_parse_pdf_makes_one_text_block_per_nonempty_trimmed_line(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    pdf_path = root / "data/raw/pdf/text.pdf"
    pdf_path.parent.mkdir(parents=True)
    _write_text_pdf(pdf_path)

    document = parse_pdf(pdf_path, root=root)

    assert document.blocks == (
        Block(page=1, kind="text", text="first  value"),
        Block(page=1, kind="text", text="second line"),
    )


def test_corrupt_pdf_is_wrapped_with_parse_error_and_cause_type(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    pdf_path = root / "data/raw/pdf/corrupt.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"%PDF-1.7\nthis is deliberately truncated and corrupt")

    with pytest.raises(ParseError) as caught:
        parse_pdf(pdf_path, root=root)

    assert caught.value.__cause__ is not None
    assert type(caught.value.__cause__).__name__ in str(caught.value)
