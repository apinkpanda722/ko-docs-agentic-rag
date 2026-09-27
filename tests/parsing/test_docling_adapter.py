"""Tests for the optional Docling PDF parsing adapter."""

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from kodocs.parsing import docling_adapter
from kodocs.parsing.docling_adapter import (
    PARSER_NAME,
    DoclingAdapter,
    DoclingSettings,
    document_to_parsed,
)
from kodocs.parsing.run import ParseInput
from kodocs.parsing.schema import Block, ParseError, content_stats, settings_hash
from kodocs.parsing.tables import cells_to_grid


@dataclass(frozen=True)
class FakeLabel:
    value: str


@dataclass(frozen=True)
class FakeProvenance:
    page_no: int


@dataclass(frozen=True)
class FakeCell:
    start_row_offset_idx: int
    start_col_offset_idx: int
    row_span: int
    col_span: int
    text: str


@dataclass(frozen=True)
class FakeTableData:
    table_cells: tuple[FakeCell, ...]


@dataclass(frozen=True)
class FakeItem:
    label: FakeLabel
    prov: tuple[FakeProvenance, ...] = ()
    text: str = ""
    data: object | None = None


class FakeDocument:
    def __init__(self, items: list[FakeItem]) -> None:
        self.items = items
        self.included_content_layers: set[str] | None = None

    def iterate_items(
        self, *, included_content_layers: set[str]
    ) -> list[tuple[FakeItem, int]]:
        self.included_content_layers = included_content_layers
        return [(item, 0) for item in self.items]


class FakeConverter:
    def __init__(self, documents: list[FakeDocument]) -> None:
        self.documents = documents
        self.paths: list[Path] = []

    def convert(self, path: Path) -> object:
        self.paths.append(path)
        return SimpleNamespace(document=self.documents[len(self.paths) - 1])


def _input(root: Path, name: str = "input.pdf") -> ParseInput:
    path = root / "data/raw/pdf" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake pdf")
    return ParseInput(
        target_key=name,
        input_path=path,
        input_sha256="a" * 64,
        input_kind="pdf",
        page_count=3,
    )


def _settings() -> dict[str, object]:
    return {
        "do_ocr": False,
        "do_table_structure": True,
        "do_cell_matching": True,
        "device": "auto",
        "torch_mps_available": None,
        "torch_cuda_available": None,
        "docling_version": "2.68.0",
        "docling_core_version": "2.50.0",
        "torch_version": "2.8.0",
    }


@pytest.fixture(autouse=True)
def _no_torch_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        docling_adapter,
        "_torch_device_availability",
        lambda: {"torch_mps_available": None, "torch_cuda_available": None},
    )


def test_settings_record_options_and_optional_package_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    versions = {
        "docling": "2.68.0",
        "docling-core": "2.50.0",
        "torch": "2.8.0",
    }
    monkeypatch.setattr(
        docling_adapter.metadata,
        "version",
        lambda package: versions[package],
    )

    adapter = DoclingAdapter(DoclingSettings())

    assert adapter.name == PARSER_NAME == "docling"
    assert adapter.settings() == _settings()


def test_settings_use_not_installed_for_missing_optional_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_version(package: str) -> str:
        raise docling_adapter.metadata.PackageNotFoundError(package)

    monkeypatch.setattr(docling_adapter.metadata, "version", missing_version)

    actual = DoclingAdapter().settings()

    assert actual["docling_version"] == "not-installed"
    assert actual["docling_core_version"] == "not-installed"
    assert actual["torch_version"] == "not-installed"


def test_document_maps_labels_including_furniture_and_reuses_previous_page(
    tmp_path: Path,
) -> None:
    fake_document = FakeDocument(
        [
            FakeItem(FakeLabel("title"), (FakeProvenance(1),), "문서 제목"),
            FakeItem(FakeLabel("section_header"), text="섹션"),
            FakeItem(FakeLabel("text"), (FakeProvenance(2),), "본문"),
            FakeItem(FakeLabel("paragraph"), text="문단"),
            FakeItem(FakeLabel("list_item"), text="항목"),
            FakeItem(FakeLabel("page_header"), text="머리말"),
            FakeItem(FakeLabel("page_footer"), text="꼬리말"),
            FakeItem(FakeLabel("caption"), text="캡션"),
            FakeItem(FakeLabel("picture"), text="그림"),
        ]
    )

    parsed = document_to_parsed(
        fake_document,
        item=_input(tmp_path),
        root=tmp_path,
        settings=_settings(),
        elapsed_s=1.25,
    )

    assert fake_document.included_content_layers == {"body", "furniture"}
    assert parsed.blocks == (
        Block(1, "heading", "문서 제목"),
        Block(1, "heading", "섹션"),
        Block(2, "text", "본문"),
        Block(2, "text", "문단"),
        Block(2, "list_item", "항목"),
        Block(2, "header", "머리말"),
        Block(2, "footer", "꼬리말"),
        Block(2, "caption", "캡션"),
        Block(2, "other", "그림"),
    )
    assert parsed.parser_version == "2.68.0"
    assert parsed.settings_hash == settings_hash(_settings())
    assert parsed.input_path == "data/raw/pdf/input.pdf"
    assert parsed.page_count == 3
    assert parsed.elapsed_s == 1.25


def test_overlapping_table_is_invalid_and_following_table_stays_valid(
    tmp_path: Path,
) -> None:
    overlapping = FakeItem(
        FakeLabel("table"),
        (FakeProvenance(1),),
        data=FakeTableData(
            (
                FakeCell(0, 0, 1, 2, "겹침"),
                FakeCell(0, 1, 1, 1, "셀"),
            )
        ),
    )
    normal = FakeItem(
        FakeLabel("table"),
        (FakeProvenance(2),),
        data=FakeTableData(
            (
                FakeCell(0, 0, 1, 1, "열1"),
                FakeCell(0, 1, 1, 1, "열2"),
                FakeCell(1, 0, 1, 1, "값1"),
                FakeCell(1, 1, 1, 1, "값2"),
            )
        ),
    )

    parsed = document_to_parsed(
        FakeDocument([overlapping, normal]),
        item=_input(tmp_path),
        root=tmp_path,
        settings=_settings(),
        elapsed_s=0.1,
    )

    assert len(parsed.tables) == 2
    assert "ValueError" in (parsed.tables[0].invalid_reason or "")
    assert parsed.tables[1].invalid_reason is None
    assert cells_to_grid(parsed.tables[1].cells) == [
        ["열1", "열2"],
        ["값1", "값2"],
    ]
    assert parsed.blocks[1].text == "|열1|열2|\n|---|---|\n|값1|값2|"
    assert content_stats(parsed)["invalid_tables"] == 1


def test_table_deduplicates_anchor_and_isolates_missing_cells_attribute(
    tmp_path: Path,
) -> None:
    duplicate_anchor = FakeItem(
        FakeLabel("table"),
        data=FakeTableData(
            (
                FakeCell(0, 0, 1, 1, "첫 셀"),
                FakeCell(0, 0, 1, 1, "중복 셀"),
            )
        ),
    )
    missing_cells = FakeItem(FakeLabel("table"), text="표 대체 텍스트", data=object())

    parsed = document_to_parsed(
        FakeDocument([duplicate_anchor, missing_cells]),
        item=_input(tmp_path),
        root=tmp_path,
        settings=_settings(),
        elapsed_s=0.1,
    )

    assert len(parsed.tables[0].cells) == 1
    assert parsed.tables[0].cells[0].text == "첫 셀"
    assert parsed.tables[0].invalid_reason is None
    assert "AttributeError" in (parsed.tables[1].invalid_reason or "")
    assert parsed.blocks[1] == Block(1, "table", "")


def test_adapter_builds_converter_once_and_converts_each_input(
    tmp_path: Path,
) -> None:
    first_document = FakeDocument([FakeItem(FakeLabel("text"), text="첫 문서")])
    second_document = FakeDocument([FakeItem(FakeLabel("text"), text="둘째 문서")])
    converter = FakeConverter([first_document, second_document])
    factory_calls: list[DoclingSettings] = []

    def factory(settings: DoclingSettings) -> object:
        factory_calls.append(settings)
        return converter

    adapter = DoclingAdapter(converter_factory=factory)
    first = adapter.parse(_input(tmp_path, "first.pdf"), root=tmp_path)
    second = adapter.parse(_input(tmp_path, "second.pdf"), root=tmp_path)

    assert factory_calls == [DoclingSettings()]
    assert converter.paths == [
        tmp_path / "data/raw/pdf/first.pdf",
        tmp_path / "data/raw/pdf/second.pdf",
    ]
    assert first.blocks[0].text == "첫 문서"
    assert second.blocks[0].text == "둘째 문서"


def test_conversion_error_is_wrapped_with_cause_type(tmp_path: Path) -> None:
    class RaisingConverter:
        def convert(self, path: Path) -> object:
            raise OSError(f"cannot convert {path.name}")

    adapter = DoclingAdapter(converter_factory=lambda settings: RaisingConverter())

    with pytest.raises(ParseError, match="OSError") as caught:
        adapter.parse(_input(tmp_path), root=tmp_path)

    assert isinstance(caught.value.__cause__, OSError)


def test_missing_docling_group_has_install_guidance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def missing_factory(settings: DoclingSettings) -> object:
        raise ModuleNotFoundError("No module named 'docling'")

    monkeypatch.setattr(docling_adapter, "_default_converter_factory", missing_factory)
    adapter = DoclingAdapter()

    with pytest.raises(
        ParseError, match=r"uv sync --group docling.*make docling-models"
    ):
        adapter.parse(_input(tmp_path), root=tmp_path)


def test_settings_record_device_setting_and_torch_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        docling_adapter,
        "_torch_device_availability",
        lambda: {"torch_mps_available": True, "torch_cuda_available": False},
    )

    actual = DoclingAdapter(DoclingSettings(device="cpu")).settings()

    assert actual["device"] == "cpu"
    assert actual["torch_mps_available"] is True
    assert actual["torch_cuda_available"] is False
    assert settings_hash(actual) != settings_hash(DoclingAdapter().settings())


def test_prepare_loads_converter_once_and_parse_times_only_conversion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(docling_adapter.time, "perf_counter", lambda: clock[0])

    class SlowConverter(FakeConverter):
        def convert(self, path: Path) -> object:
            clock[0] += 2.0
            return super().convert(path)

    created: list[DoclingSettings] = []

    def slow_factory(settings: DoclingSettings) -> object:
        created.append(settings)
        clock[0] += 100.0  # model loading / pipeline initialization
        return SlowConverter([FakeDocument([]), FakeDocument([])])

    adapter = DoclingAdapter(converter_factory=slow_factory)
    adapter.prepare()
    first = adapter.parse(_input(tmp_path, "a.pdf"), root=tmp_path)
    second = adapter.parse(_input(tmp_path, "b.pdf"), root=tmp_path)

    assert len(created) == 1
    assert first.elapsed_s == 2.0
    assert second.elapsed_s == 2.0


def test_parse_without_prepare_still_excludes_converter_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(docling_adapter.time, "perf_counter", lambda: clock[0])

    def slow_factory(settings: DoclingSettings) -> object:
        del settings
        clock[0] += 100.0
        return FakeConverter([FakeDocument([])])

    parsed = DoclingAdapter(converter_factory=slow_factory).parse(
        _input(tmp_path), root=tmp_path
    )

    assert parsed.elapsed_s == 0.0
