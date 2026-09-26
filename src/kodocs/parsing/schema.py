"""Parser-independent parsed document schema and serialization helpers."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SCHEMA_VERSION: int = 1

BlockKind = Literal[
    "text",
    "heading",
    "list_item",
    "header",
    "footer",
    "page_number",
    "table",
    "caption",
    "other",
]
InputKind = Literal["pdf", "hwp5", "hwpx"]


class ParseError(RuntimeError):
    """A parser adapter could not produce a parsed document."""


@dataclass(frozen=True)
class Block:
    """One parser-labeled block, preserving the parser's text."""

    page: int
    kind: BlockKind
    text: str


@dataclass(frozen=True)
class TableCell:
    """One table cell at its top-left anchor position."""

    row: int
    col: int
    row_span: int
    col_span: int
    text: str


@dataclass(frozen=True)
class ParsedTable:
    """A structured table corresponding to a table block."""

    page: int
    block_position: int
    cells: tuple[TableCell, ...]


@dataclass(frozen=True)
class ParsedDocument:
    """Common output shared by every parser adapter."""

    schema_version: int
    parser: str
    parser_version: str
    settings: Mapping[str, object]
    settings_hash: str
    input_path: str
    input_sha256: str
    input_kind: InputKind
    page_count: int | None
    blocks: tuple[Block, ...]
    tables: tuple[ParsedTable, ...]
    elapsed_s: float


def settings_hash(settings: Mapping[str, object]) -> str:
    """Return the first 12 hex characters of canonical settings JSON's SHA-256."""
    serialized = json.dumps(
        dict(settings),
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode()).hexdigest()[:12]


def to_json(doc: ParsedDocument) -> str:
    """Serialize a parsed document to deterministic, unescaped UTF-8 JSON text."""
    payload = {
        "schema_version": doc.schema_version,
        "parser": doc.parser,
        "parser_version": doc.parser_version,
        "settings": dict(doc.settings),
        "settings_hash": doc.settings_hash,
        "input_path": doc.input_path,
        "input_sha256": doc.input_sha256,
        "input_kind": doc.input_kind,
        "page_count": doc.page_count,
        "blocks": [
            {"page": block.page, "kind": block.kind, "text": block.text}
            for block in doc.blocks
        ],
        "tables": [
            {
                "page": table.page,
                "block_position": table.block_position,
                "cells": [
                    {
                        "row": cell.row,
                        "col": cell.col,
                        "row_span": cell.row_span,
                        "col_span": cell.col_span,
                        "text": cell.text,
                    }
                    for cell in table.cells
                ],
            }
            for table in doc.tables
        ],
        "elapsed_s": doc.elapsed_s,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def from_json(text: str) -> ParsedDocument:
    """Deserialize parsed document JSON into the common immutable dataclasses."""
    payload = json.loads(text)
    blocks = tuple(Block(**block) for block in payload.pop("blocks"))
    tables = tuple(
        ParsedTable(
            page=table["page"],
            block_position=table["block_position"],
            cells=tuple(TableCell(**cell) for cell in table["cells"]),
        )
        for table in payload.pop("tables")
    )
    return ParsedDocument(**payload, blocks=blocks, tables=tables)


def output_path(root: Path, parser: str, input_sha256: str) -> Path:
    """Return the parser- and input-addressed interim output path."""
    return root / "data/interim/parsed" / parser / f"{input_sha256}.json"


def content_stats(doc: ParsedDocument) -> dict[str, int]:
    """Return format-independent content sanity metrics for a parsed document."""
    text = "".join(block.text for block in doc.blocks)
    return {
        "text_chars": len(text),
        "hangul_distinct": len(
            {character for character in text if "가" <= character <= "힣"}
        ),
        "blocks": len(doc.blocks),
        "tables": len(doc.tables),
    }
