"""Detect downloaded document formats from file signatures."""

from enum import StrEnum
from pathlib import Path
from zipfile import BadZipFile, ZipFile

_HWP5_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
_ZIP_SIGNATURE = b"PK\x03\x04"
_PDF_SIGNATURE = b"%PDF-"
_HWPX_MIMETYPE = b"application/hwp+zip"


class FileKind(StrEnum):
    """Supported source formats identified independently of their extensions."""

    HWP5 = "hwp5"
    HWPX = "hwpx"
    ZIP = "zip"
    PDF = "pdf"
    HTML = "html"
    UNKNOWN = "unknown"


def sniff(path: Path) -> FileKind:
    """Identify a file from its signature and, for ZIP files, its contents."""
    with path.open("rb") as source:
        head = source.read(4096)
    return sniff_bytes(head, zip_path=path)


def sniff_bytes(head: bytes, *, zip_path: Path | None = None) -> FileKind:
    """Identify bytes, optionally inspecting a ZIP file path for HWPX metadata."""
    if head.startswith(_HWP5_SIGNATURE):
        return FileKind.HWP5
    if head.startswith(_ZIP_SIGNATURE):
        if zip_path is None:
            return FileKind.ZIP
        return _sniff_zip(zip_path)
    if head.startswith(_PDF_SIGNATURE):
        return FileKind.PDF
    if head.lstrip().startswith(b"<"):
        return FileKind.HTML
    return FileKind.UNKNOWN


def _sniff_zip(path: Path) -> FileKind:
    try:
        with ZipFile(path) as archive:
            try:
                mimetype = archive.read("mimetype")
            except KeyError:
                return FileKind.ZIP
    except (BadZipFile, OSError):
        return FileKind.UNKNOWN

    if mimetype == _HWPX_MIMETYPE:
        return FileKind.HWPX
    return FileKind.ZIP
