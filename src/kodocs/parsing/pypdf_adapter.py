"""Baseline PDF parser adapter backed by pypdf plain text extraction."""

import time
from importlib.metadata import version
from pathlib import Path

from pypdf import PdfReader

from kodocs.ingest.manifest import repo_relative, sha256_file
from kodocs.parsing.schema import (
    SCHEMA_VERSION,
    Block,
    ParsedDocument,
    ParseError,
    settings_hash,
)

PARSER_NAME = "pypdf"


def parse_pdf(pdf_path: Path, *, root: Path) -> ParsedDocument:
    """Parse one PDF with pypdf's fixed default plain-text extraction mode."""
    input_path = repo_relative(pdf_path, root)
    input_sha256 = sha256_file(pdf_path)
    parser_version = version("pypdf")
    settings = {"extraction_mode": "plain"}

    started = time.perf_counter()
    try:
        reader = PdfReader(pdf_path)
        page_count = len(reader.pages)
        blocks = tuple(
            Block(page=page_number, kind="text", text=stripped)
            for page_number, page in enumerate(reader.pages, start=1)
            for line in (page.extract_text() or "").splitlines()
            if (stripped := line.strip())
        )
    except Exception as exc:
        raise ParseError(
            f"pypdf parsing failed with {type(exc).__name__}: {exc}"
        ) from exc
    elapsed_s = time.perf_counter() - started

    return ParsedDocument(
        schema_version=SCHEMA_VERSION,
        parser=PARSER_NAME,
        parser_version=parser_version,
        settings=settings,
        settings_hash=settings_hash(settings),
        input_path=input_path,
        input_sha256=input_sha256,
        input_kind="pdf",
        page_count=page_count,
        blocks=blocks,
        tables=(),
        elapsed_s=elapsed_s,
    )
