"""Tests for signature-based source file detection."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from kodocs.ingest.filetype import FileKind, sniff


def test_ole_signature_is_hwp5_even_with_hwpx_extension(tmp_path: Path) -> None:
    path = tmp_path / "mislabeled.hwpx"
    path.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + b"payload")

    assert sniff(path) is FileKind.HWP5


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
