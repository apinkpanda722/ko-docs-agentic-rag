"""Helpers for safe, reproducible ingestion manifests."""

import hashlib
import json
from collections.abc import Collection, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Final
from urllib.parse import unquote_plus, urlsplit, urlunsplit

LICENSE_NOTE_NO_KOGL: str = "공공누리 표시 없음: 원문 무가공, 로컬 전용, 재배포 금지"
SECRET_QUERY_PARAMS: frozenset[str] = frozenset({"oc"})

_RAW_FILE_REQUIRED_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "source",
        "source_url",
        "local_path",
        "sha256",
        "size_bytes",
        "collected_at",
        "license_note",
    }
)
_FORBIDDEN_KEYS: Final[frozenset[str]] = frozenset({"담당자명", "전화번호"})
_KST: Final[timezone] = timezone(timedelta(hours=9))


def sanitize_url(url: str) -> str:
    """Remove secret query parameters while preserving all other parameter order."""
    parts = urlsplit(url)
    if not parts.query:
        return url

    safe_parts = []
    for part in parts.query.split("&"):
        raw_name = part.partition("=")[0]
        if unquote_plus(raw_name).casefold() not in SECRET_QUERY_PARAMS:
            safe_parts.append(part)

    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, "&".join(safe_parts), parts.fragment)
    )


def sha256_file(path: Path) -> str:
    """Return the lowercase SHA-256 digest for a file."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def now_iso() -> str:
    """Return the current KST time as ISO 8601 at second precision."""
    return datetime.now(_KST).isoformat(timespec="seconds")


def append_record(
    manifest_path: Path,
    record: Mapping[str, object],
    *,
    required: Collection[str] = _RAW_FILE_REQUIRED_FIELDS,
) -> None:
    """Append one validated, deterministically serialized JSONL record."""
    missing = sorted(set(required).difference(record))
    if missing:
        raise ValueError(
            f"Manifest record is missing required fields: {', '.join(missing)}"
        )

    forbidden_key = _find_forbidden_key(record)
    if forbidden_key is not None:
        raise ValueError(f"Manifest record contains forbidden key: {forbidden_key}")

    if "source_url" in record:
        source_url = record["source_url"]
        if not isinstance(source_url, str):
            raise ValueError("Manifest source_url must be a string")
        if sanitize_url(source_url) != source_url:
            raise ValueError("Manifest source_url contains a secret query parameter")

    if "local_path" in record:
        local_path = record["local_path"]
        if not isinstance(local_path, str):
            raise ValueError("Manifest local_path must be a string")
        if PurePosixPath(local_path).is_absolute():
            raise ValueError("Manifest local_path must be repository-relative")

    serialized = json.dumps(record, ensure_ascii=False, sort_keys=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("a", encoding="utf-8") as destination:
        destination.write(f"{serialized}\n")


def load_records(manifest_path: Path) -> list[dict[str, object]]:
    """Load JSONL records, returning an empty list when the file is absent."""
    if not manifest_path.exists():
        return []

    records: list[dict[str, object]] = []
    with manifest_path.open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                records.append(json.loads(line))
    return records


def repo_relative(path: Path, root: Path) -> str:
    """Return a path beneath the repository root in POSIX form."""
    return path.resolve().relative_to(root.resolve()).as_posix()


def _find_forbidden_key(value: object) -> str | None:
    if isinstance(value, Mapping):
        for key, nested_value in value.items():
            if key in _FORBIDDEN_KEYS:
                return str(key)
            if found := _find_forbidden_key(nested_value):
                return found
    elif isinstance(value, (list, tuple)):
        for nested_value in value:
            if found := _find_forbidden_key(nested_value):
                return found
    return None
