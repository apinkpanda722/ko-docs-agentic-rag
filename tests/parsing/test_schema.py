"""Tests for the parser-independent parsed document schema."""

import hashlib
import json
from pathlib import Path

from kodocs.parsing.schema import (
    SCHEMA_VERSION,
    Block,
    ParsedDocument,
    ParsedTable,
    TableCell,
    content_stats,
    from_json,
    output_path,
    settings_hash,
    to_json,
)


def _document() -> ParsedDocument:
    settings = {"ocr": False, "options": {"language": "ko", "pages": [1, 2]}}
    return ParsedDocument(
        schema_version=SCHEMA_VERSION,
        parser="docling",
        parser_version="2.0.0",
        settings=settings,
        settings_hash=settings_hash(settings),
        input_path="data/raw/pdf/example.pdf",
        input_sha256="a" * 64,
        input_kind="pdf",
        page_count=2,
        blocks=(
            Block(page=1, kind="heading", text="제1조 목적"),
            Block(page=2, kind="table", text="항목\t값\n이름\t한글"),
        ),
        tables=(
            ParsedTable(
                page=2,
                block_position=1,
                cells=(
                    TableCell(
                        row=0,
                        col=0,
                        row_span=1,
                        col_span=2,
                        text="항목",
                    ),
                ),
            ),
        ),
        elapsed_s=0.125,
    )


def test_json_round_trip_preserves_document_and_korean() -> None:
    document = _document()

    serialized = to_json(document)

    assert "제1조 목적" in serialized
    assert "\\uc81c" not in serialized
    assert from_json(serialized) == document


def test_settings_hash_uses_sorted_key_json_and_twelve_hex_characters() -> None:
    first = {"ocr": False, "language": "ko"}
    reordered = {"language": "ko", "ocr": False}
    expected = hashlib.sha256(
        json.dumps(
            first,
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()[:12]

    assert settings_hash(first) == expected
    assert settings_hash(reordered) == expected


def test_output_path_is_parser_and_input_hash_scoped(tmp_path: Path) -> None:
    assert output_path(tmp_path, "pypdf", "b" * 64) == (
        tmp_path / "data/interim/parsed/pypdf" / f"{'b' * 64}.json"
    )


def test_content_stats_counts_raw_block_text_hangul_blocks_and_tables() -> None:
    document = _document()

    assert content_stats(document) == {
        "text_chars": sum(len(block.text) for block in document.blocks),
        "hangul_distinct": 10,
        "blocks": 2,
        "tables": 1,
    }
