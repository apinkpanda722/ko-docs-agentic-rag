"""Fake source files for ingestion tests."""

import struct

_SECTOR_SIZE = 512
_FREESECT = 0xFFFFFFFF
_ENDOFCHAIN = 0xFFFFFFFE
_FATSECT = 0xFFFFFFFD
_NOSTREAM = 0xFFFFFFFF
_STREAM_SIZE = 4096  # mini stream cutoff 이상이라 일반 FAT 섹터에 저장된다

HWP5_FILE_HEADER = b"HWP Document File".ljust(32, b"\x00")
HWP3_HEADER = b"HWP Document File V3.00 \x1a\x01\x02\x03\x04\x05"


def _dir_entry(name: str, entry_type: int, child: int, start: int, size: int) -> bytes:
    encoded = (name + "\x00").encode("utf-16-le") if name else b""
    return (
        encoded.ljust(64, b"\x00")
        + struct.pack("<HBB", len(encoded), entry_type, 1)
        + struct.pack("<III", _NOSTREAM, _NOSTREAM, child)
        + b"\x00" * 16  # CLSID
        + struct.pack("<I", 0)  # state bits
        + b"\x00" * 16  # creation / modified time
        + struct.pack("<IQ", start, size)
    )


def ole_bytes(stream_name: str, stream_data: bytes) -> bytes:
    """Minimal OLE compound file (CFB v3) holding one root-level stream."""
    data = stream_data[:_STREAM_SIZE].ljust(_STREAM_SIZE, b"\x00")
    data_sectors = _STREAM_SIZE // _SECTOR_SIZE

    header = (
        bytes.fromhex("D0CF11E0A1B11AE1")
        + b"\x00" * 16
        + struct.pack("<HHHHH", 0x003E, 0x0003, 0xFFFE, 9, 6)
        + b"\x00" * 6
        + struct.pack("<IIII", 0, 1, 1, 0)  # dir sectors, FAT sectors, first dir, txn
        + struct.pack("<IIIII", _STREAM_SIZE, _ENDOFCHAIN, 0, _ENDOFCHAIN, 0)
        + struct.pack("<I", 0)  # DIFAT[0] = sector 0 (FAT)
        + struct.pack("<I", _FREESECT) * 108
    )

    fat = [_FATSECT, _ENDOFCHAIN]
    fat += [2 + i + 1 for i in range(data_sectors - 1)] + [_ENDOFCHAIN]
    fat += [_FREESECT] * (_SECTOR_SIZE // 4 - len(fat))
    fat_sector = struct.pack(f"<{len(fat)}I", *fat)

    directory = (
        _dir_entry("Root Entry", 5, 1, _ENDOFCHAIN, 0)
        + _dir_entry(stream_name, 2, _NOSTREAM, 2, _STREAM_SIZE)
        + _dir_entry("", 0, _NOSTREAM, 0, 0) * 2
    )

    return header + fat_sector + directory + data


def hwp5_bytes() -> bytes:
    """OLE whose FileHeader stream carries the HWP 5.x signature."""
    return ole_bytes("FileHeader", HWP5_FILE_HEADER)


def ole_other_bytes() -> bytes:
    """OLE without an HWP FileHeader (e.g. a legacy .doc)."""
    return ole_bytes("WordDocument", b"\xec\xa5\xc1\x00")
