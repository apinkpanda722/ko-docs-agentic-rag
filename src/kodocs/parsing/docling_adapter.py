"""Optional Docling adapter for layout- and table-aware PDF parsing."""

from __future__ import annotations

import importlib.util
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING

from kodocs.ingest.manifest import repo_relative
from kodocs.parsing.schema import (
    SCHEMA_VERSION,
    Block,
    BlockKind,
    ParsedDocument,
    ParsedTable,
    ParseError,
    TableCell,
    build_table,
    settings_hash,
)

if TYPE_CHECKING:
    from kodocs.parsing.run import ParseInput

PARSER_NAME = "docling"

_CONTENT_LAYERS = {"body", "furniture"}
_LABEL_KINDS: dict[str, BlockKind] = {
    "title": "heading",
    "section_header": "heading",
    "text": "text",
    "paragraph": "text",
    "list_item": "list_item",
    "page_header": "header",
    "page_footer": "footer",
    "caption": "caption",
    "table": "table",
}
_MISSING_GROUP_MESSAGE = (
    "docling 그룹이 설치되지 않았다: `uv sync --group docling` 후 `make docling-models`"
)


@dataclass(frozen=True)
class DoclingSettings:
    """Fixed Docling options used for a comparable parser experiment."""

    do_ocr: bool = False
    do_table_structure: bool = True
    do_cell_matching: bool = True
    # Docling AcceleratorDevice value: "auto" | "cpu" | "mps" | "cuda".
    device: str = "auto"


class DoclingAdapter:
    """Lazily initialize one Docling converter and reuse its loaded models."""

    name = PARSER_NAME

    def __init__(
        self,
        settings: DoclingSettings = DoclingSettings(),  # noqa: B008
        *,
        converter_factory: Callable[[DoclingSettings], object] | None = None,
    ) -> None:
        self._settings = settings
        self._converter_factory = converter_factory
        self._converter: object | None = None
        self._torch_devices: Mapping[str, bool | None] | None = None
        self.parser_version = _package_version("docling")

    def settings(self) -> Mapping[str, object]:
        """Return fixed pipeline options and reproducibility package versions."""
        return {
            "do_ocr": self._settings.do_ocr,
            "do_table_structure": self._settings.do_table_structure,
            "do_cell_matching": self._settings.do_cell_matching,
            "device": self._settings.device,
            **self._torch_device_flags(),
            "docling_version": self.parser_version,
            "docling_core_version": _package_version("docling-core"),
            "torch_version": _package_version("torch"),
        }

    def prepare(self) -> None:
        """Build the converter and load its models before any document is timed."""
        self._get_converter()

    def parse(self, item: ParseInput, *, root: Path) -> ParsedDocument:
        """Convert one PDF and normalize the Docling document to common schema."""
        current_settings = dict(self.settings())
        converter = self._get_converter()
        # Only the conversion is timed; model loading happens in prepare().
        started = time.perf_counter()
        try:
            result = converter.convert(item.input_path)  # type: ignore[attr-defined]
            doc = result.document
        except Exception as exc:
            raise ParseError(
                f"docling conversion failed with {type(exc).__name__}: {exc}"
            ) from exc
        elapsed_s = time.perf_counter() - started
        return document_to_parsed(
            doc,
            item=item,
            root=root,
            settings=current_settings,
            elapsed_s=elapsed_s,
        )

    def _torch_device_flags(self) -> Mapping[str, bool | None]:
        if self._torch_devices is None:
            self._torch_devices = _torch_device_availability()
        return self._torch_devices

    def _get_converter(self) -> object:
        if self._converter is None:
            factory = self._converter_factory or _default_converter_factory
            try:
                self._converter = factory(self._settings)
            except ParseError:
                raise
            except ModuleNotFoundError as exc:
                raise ParseError(_MISSING_GROUP_MESSAGE) from exc
        return self._converter


def document_to_parsed(
    doc: object,
    *,
    item: ParseInput,
    root: Path,
    settings: Mapping[str, object],
    elapsed_s: float,
) -> ParsedDocument:
    """Normalize a converted Docling document without importing Docling types."""
    blocks: list[Block] = []
    tables: list[ParsedTable] = []
    previous_page = 1

    for source_item, _level in doc.iterate_items(  # type: ignore[attr-defined]
        included_content_layers=_CONTENT_LAYERS
    ):
        if source_item.prov:
            previous_page = source_item.prov[0].page_no
        page = previous_page
        label = _label_value(source_item.label)
        kind = _LABEL_KINDS.get(label, "other")

        if kind == "table":
            table, text = _build_docling_table(
                source_item,
                page=page,
                block_position=len(blocks),
            )
            tables.append(table)
        else:
            text = source_item.text
        blocks.append(Block(page=page, kind=kind, text=text))

    stable_settings = dict(settings)
    return ParsedDocument(
        schema_version=SCHEMA_VERSION,
        parser=PARSER_NAME,
        parser_version=str(stable_settings["docling_version"]),
        settings=stable_settings,
        settings_hash=settings_hash(stable_settings),
        input_path=repo_relative(item.input_path, root),
        input_sha256=item.input_sha256,
        input_kind="pdf",
        page_count=item.page_count,
        blocks=tuple(blocks),
        tables=tuple(tables),
        elapsed_s=elapsed_s,
    )


def _default_converter_factory(settings: DoclingSettings) -> object:
    """Build Docling using lazy imports so the default environment stays small."""
    try:
        from docling.datamodel.accelerator_options import (
            AcceleratorDevice,
            AcceleratorOptions,
        )
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ModuleNotFoundError as exc:
        raise ParseError(_MISSING_GROUP_MESSAGE) from exc

    options = PdfPipelineOptions()
    options.do_ocr = settings.do_ocr
    options.do_table_structure = settings.do_table_structure
    options.table_structure_options.do_cell_matching = settings.do_cell_matching
    options.accelerator_options = AcceleratorOptions(
        device=AcceleratorDevice(settings.device)
    )
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=options),
        }
    )
    # Official warm-up API: builds the PDF pipeline (layout/table models) and
    # caches it so later convert() calls reuse it.
    converter.initialize_pipeline(InputFormat.PDF)
    return converter


def _torch_device_availability() -> dict[str, bool | None]:
    """Report which torch accelerators exist; None when torch is not installed."""
    if importlib.util.find_spec("torch") is None:
        return {"torch_mps_available": None, "torch_cuda_available": None}
    try:
        import torch
    except Exception:  # noqa: BLE001 - availability probe must not fail settings
        return {"torch_mps_available": None, "torch_cuda_available": None}
    return {
        "torch_mps_available": bool(torch.backends.mps.is_available()),
        "torch_cuda_available": bool(torch.cuda.is_available()),
    }


def _build_docling_table(
    source_item: object,
    *,
    page: int,
    block_position: int,
) -> tuple[ParsedTable, str]:
    cells: list[TableCell] = []
    try:
        anchors: set[tuple[int, int]] = set()
        for source_cell in source_item.data.table_cells:  # type: ignore[attr-defined]
            anchor = (
                source_cell.start_row_offset_idx,
                source_cell.start_col_offset_idx,
            )
            if anchor in anchors:
                continue
            anchors.add(anchor)
            cells.append(
                TableCell(
                    row=source_cell.start_row_offset_idx,
                    col=source_cell.start_col_offset_idx,
                    row_span=source_cell.row_span,
                    col_span=source_cell.col_span,
                    text=source_cell.text,
                )
            )
        return build_table(page, block_position, cells)
    except Exception as exc:  # noqa: BLE001 - one malformed table must stay isolated
        reason = _first_line(f"table_parse: {type(exc).__name__}: {exc}")
        table, text = build_table(page, block_position, cells)
        return replace(table, invalid_reason=reason), text


def _label_value(label: object) -> str:
    value = getattr(label, "value", label)
    return str(value)


def _package_version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "not-installed"


def _first_line(message: str) -> str:
    lines = message.splitlines()
    return (lines[0] if lines else "")[:200]
