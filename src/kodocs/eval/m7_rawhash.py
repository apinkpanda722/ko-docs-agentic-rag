"""Authenticate uncommitted M7 JSONL bytes with append-only manifest history."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path


class RawHashMismatch(RuntimeError):
    """Raw output differs from its latest registered fingerprint."""


def fingerprint(path: Path) -> dict:
    data = Path(path).read_bytes()
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "rows": sum(bool(line.strip()) for line in data.decode("utf-8").splitlines()),
    }


def record_fingerprint(manifest_path: Path, key: str, path: Path, event: str) -> dict:
    manifest_path, path = Path(manifest_path), Path(path)
    manifest = json.loads(manifest_path.read_bytes()) if manifest_path.exists() else {}
    record = {
        "key": key,
        "path": path.as_posix(),
        **fingerprint(path),
        "event": event,
        "at": datetime.now(UTC).isoformat(),
    }
    manifest.setdefault("raw", []).append(record)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return record


def verify_fingerprint(manifest_path: Path, key: str, path: Path) -> dict:
    try:
        manifest = json.loads(Path(manifest_path).read_bytes())
        records = [r for r in manifest.get("raw", []) if r["key"] == key]
        latest = records[-1]
        actual = fingerprint(path)
        if any(latest[k] != actual[k] for k in ("sha256", "rows")):
            raise RawHashMismatch(f"{key}: raw SHA-256/rows mismatch")
        return actual
    except (OSError, ValueError, KeyError, IndexError) as exc:
        raise RawHashMismatch(f"{key}: raw fingerprint missing or invalid") from exc
