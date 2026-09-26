"""Tests for signature-based source file detection."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fakes import HWP3_HEADER, hwp5_bytes, ole_other_bytes

from kodocs.ingest.filetype import FileKind, sniff, sniff_bytes


def test_ole_with_hwp_file_header_is_hwp5_even_with_hwpx_extension(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mislabeled.hwpx"
    path.write_bytes(hwp5_bytes())

    assert sniff(path) is FileKind.HWP5


def test_ole_without_hwp_file_header_is_ole_other(tmp_path: Path) -> None:
    path = tmp_path / "legacy.hwp"
    path.write_bytes(ole_other_bytes())

    assert sniff(path) is FileKind.OLE_OTHER


def test_ole_signature_with_corrupt_body_is_unknown(tmp_path: Path) -> None:
    path = tmp_path / "broken.hwp"
    path.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + b"payload")

    assert sniff(path) is FileKind.UNKNOWN


def test_ole_signature_with_zero_filled_body_is_unknown() -> None:
    """olefile 0.47 raises ValueError (not OSError) for this corrupt header."""
    data = bytes.fromhex("D0CF11E0A1B11AE1") + b"\0" * 600

    assert sniff_bytes(data) is FileKind.UNKNOWN


def test_hwp3_header_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "old.hwp"
    path.write_bytes(HWP3_HEADER + b"\x00" * 64)

    assert sniff(path) is FileKind.HWP3


def test_sniff_bytes_reads_full_ole_content() -> None:
    assert sniff_bytes(hwp5_bytes()) is FileKind.HWP5
    assert sniff_bytes(ole_other_bytes()) is FileKind.OLE_OTHER


@pytest.mark.parametrize("head_size", [512, 1536])
def test_sniff_bytes_does_not_assume_hwp5_from_truncated_ole_head(
    head_size: int,
) -> None:
    """An OLE signature alone is not evidence of HWP 5.x without its streams."""
    assert sniff_bytes(hwp5_bytes()[:head_size]) is not FileKind.HWP5


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"%PDF-1.7\ncontent", FileKind.PDF),
        (b"  <html>error</html>", FileKind.HTML),
        (HWP3_HEADER, FileKind.HWP3),
        (b"", FileKind.UNKNOWN),
    ],
)
def test_sniff_bytes_matches_sniff_for_non_container_formats(
    content: bytes, expected: FileKind
) -> None:
    assert sniff_bytes(content) is expected


def test_sniff_bytes_inspects_zip_content(tmp_path: Path) -> None:
    path = tmp_path / "doc.hwpx"
    with ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/hwp+zip")

    assert sniff_bytes(path.read_bytes()) is FileKind.HWPX


def test_hwpx_mimetype_is_detected_even_with_hwp_extension(tmp_path: Path) -> None:
    path = tmp_path / "mislabeled.hwp"
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/hwp+zip")
        archive.writestr("Contents/content.hpf", "content")

    assert sniff(path) is FileKind.HWPX


def test_zip_without_hwpx_mimetype_is_plain_zip(tmp_path: Path) -> None:
    path = tmp_path / "bundle.hwpx"
    with ZipFile(path, "w") as archive:
        archive.writestr("document.txt", "content")

    assert sniff(path) is FileKind.ZIP


def test_unreadable_zip_is_unknown(tmp_path: Path) -> None:
    path = tmp_path / "broken.zip"
    path.write_bytes(b"PK\x03\x04not-a-valid-zip")

    assert sniff(path) is FileKind.UNKNOWN


def test_pdf_signature_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "document.hwp"
    path.write_bytes(b"%PDF-1.7\ncontent")

    assert sniff(path) is FileKind.PDF


def test_html_is_detected_after_leading_whitespace(tmp_path: Path) -> None:
    path = tmp_path / "download.hwp"
    path.write_bytes(b" \r\n\t<html><body>error</body></html>")

    assert sniff(path) is FileKind.HTML


def test_empty_and_unrecognized_files_are_unknown(tmp_path: Path) -> None:
    empty = tmp_path / "empty.pdf"
    unknown = tmp_path / "unknown.hwp"
    empty.write_bytes(b"")
    unknown.write_bytes(b"not a known format")

    assert sniff(empty) is FileKind.UNKNOWN
    assert sniff(unknown) is FileKind.UNKNOWN
