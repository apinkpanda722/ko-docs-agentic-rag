"""Detect downloaded document formats from file signatures."""

import io
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO
from zipfile import BadZipFile, ZipFile

import olefile

_OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
_ZIP_SIGNATURE = b"PK\x03\x04"
_PDF_SIGNATURE = b"%PDF-"
_HWP3_SIGNATURE = b"HWP Document File V3.00"
_HWP5_FILE_HEADER_SIGNATURE = b"HWP Document File"
_HWPX_MIMETYPE = b"application/hwp+zip"


class FileKind(StrEnum):
    """Supported source formats identified independently of their extensions."""

    HWP5 = "hwp5"  # OLE whose FileHeader stream starts with the HWP signature
    HWP3 = "hwp3"  # non-OLE HWP 3.x (not supported by H2Orestart)
    OLE_OTHER = "ole_other"  # other OLE files such as legacy .doc/.xls
    HWPX = "hwpx"
    ZIP = "zip"
    PDF = "pdf"
    HTML = "html"
    UNKNOWN = "unknown"


def sniff(path: Path) -> FileKind:
    """Identify a file from its signature and, for containers, its contents."""
    with path.open("rb") as source:
        return _sniff_stream(source)


def sniff_bytes(data: bytes) -> FileKind:
    """Identify complete file content held in memory (e.g. before saving).

    OLE and ZIP detection reads container internals, so ``data`` must be the whole
    file. If the OLE internals cannot be read, the result is UNKNOWN.
    """
    return _sniff_stream(io.BytesIO(data))


def _sniff_stream(source: BinaryIO) -> FileKind:
    head = source.read(len(_HWP3_SIGNATURE))
    source.seek(0)
    if head.startswith(_OLE_SIGNATURE):
        return _sniff_ole(source)
    if head.startswith(_ZIP_SIGNATURE):
        return _sniff_zip(source)
    if head.startswith(_HWP3_SIGNATURE):
        return FileKind.HWP3
    if head.startswith(_PDF_SIGNATURE):
        return FileKind.PDF
    if source.read(4096).lstrip().startswith(b"<"):
        return FileKind.HTML
    return FileKind.UNKNOWN


def _sniff_ole(source: BinaryIO) -> FileKind:
    try:
        with olefile.OleFileIO(source) as ole:
            if not ole.exists("FileHeader"):
                return FileKind.OLE_OTHER
            stream = ole.openstream("FileHeader")
            header = stream.read(len(_HWP5_FILE_HEADER_SIGNATURE))
    except (OSError, ValueError):  # olefile 0.47: OleFileError(OSError) or ValueError
        return FileKind.UNKNOWN

    if header == _HWP5_FILE_HEADER_SIGNATURE:
        return FileKind.HWP5
    return FileKind.OLE_OTHER


def _sniff_zip(source: BinaryIO) -> FileKind:
    try:
        with ZipFile(source) as archive:
            try:
                mimetype = archive.read("mimetype")
            except KeyError:
                return FileKind.ZIP
    except (BadZipFile, OSError):
        return FileKind.UNKNOWN

    if mimetype == _HWPX_MIMETYPE:
        return FileKind.HWPX
    return FileKind.ZIP
