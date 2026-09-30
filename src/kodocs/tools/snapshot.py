"""Explicit generator and validator for immutable versioned tool snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from kodocs.tools.contract import (
    TOOL_DESCRIPTIONS,
    TOOL_INPUT_MODELS,
    TOOL_OUTPUT_MODELS,
    TOOL_VERSIONS,
)

_SNAPSHOT_NAME = re.compile(r"(?P<name>[a-z_]+)\.v(?P<version>[1-9][0-9]*)\.json")
_DEFAULT_DIRECTORY = Path("tests/contract/snapshots")


class ContractSnapshotError(RuntimeError):
    """The live tool contract and immutable snapshots are inconsistent."""


def build_contract_snapshot(name: str) -> dict[str, object]:
    """Build one JSON-compatible snapshot without writing to disk."""
    if name not in TOOL_VERSIONS:
        raise ValueError(f"unknown tool: {name}")
    return {
        "name": name,
        "version": TOOL_VERSIONS[name],
        "description": TOOL_DESCRIPTIONS[name],
        "input_schema": TOOL_INPUT_MODELS[name].model_json_schema(mode="validation"),
        "output_schema": TOOL_OUTPUT_MODELS[name].model_json_schema(
            mode="serialization"
        ),
    }


def canonical_snapshot_bytes(name: str) -> bytes:
    """Serialize the current snapshot in its checked-in canonical form."""
    return _canonical_json_bytes(build_contract_snapshot(name))


def validate_snapshots(
    directory: Path,
    *,
    current_snapshots: Mapping[str, Mapping[str, object]] | None = None,
    versions: Mapping[str, int] | None = None,
) -> None:
    """Require a current snapshot and a version bump for every schema change."""
    registered = dict(TOOL_VERSIONS if versions is None else versions)
    current = (
        {name: build_contract_snapshot(name) for name in registered}
        if current_snapshots is None
        else dict(current_snapshots)
    )
    files = _snapshot_files(directory)
    for name in sorted(registered):
        version_files = files.get(name, {})
        if not version_files:
            raise ContractSnapshotError(f"{name}: snapshot file is missing")
        latest = max(version_files)
        registered_version = registered[name]
        if registered_version > latest:
            raise ContractSnapshotError(
                f"{name}: v{registered_version} snapshot file is missing"
            )
        if registered_version < latest:
            raise ContractSnapshotError(
                f"{name}: registered version {registered_version} is older than snapshot v{latest}"
            )
        expected = _canonical_json_bytes(current[name])
        actual = version_files[latest].read_bytes()
        if actual != expected:
            raise ContractSnapshotError(
                f"{name}: contract changed without a version bump"
            )
    unexpected = set(files) - set(registered)
    if unexpected:
        names = ", ".join(sorted(unexpected))
        raise ContractSnapshotError(f"unregistered tool snapshots: {names}")


def validate_snapshot_index(directory: Path) -> None:
    """Verify every historical snapshot against the checked-in hash index."""
    index_path = directory / "index.json"
    try:
        value = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractSnapshotError("snapshot index is missing or invalid") from exc
    if not isinstance(value, Mapping) or not isinstance(value.get("files"), Mapping):
        raise ContractSnapshotError("snapshot index must contain a files object")
    recorded = value["files"]
    paths = sorted(
        path for path in directory.glob("*.json") if path.name != "index.json"
    )
    expected_names = {path.name for path in paths}
    if set(recorded) != expected_names:
        raise ContractSnapshotError("snapshot index file list does not match snapshots")
    for path in paths:
        expected_hash = recorded[path.name]
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected_hash != actual_hash:
            raise ContractSnapshotError(
                f"historical snapshot hash mismatch: {path.name}"
            )


def write_current_snapshots(directory: Path) -> None:
    """Create missing current snapshots and refuse to replace changed history."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, version in sorted(TOOL_VERSIONS.items()):
        path = directory / f"{name}.v{version}.json"
        content = canonical_snapshot_bytes(name)
        if path.exists() and path.read_bytes() != content:
            raise ContractSnapshotError(
                f"{path.name} already exists with different content; bump the version"
            )
        if not path.exists():
            path.write_bytes(content)
    files = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.glob("*.v*.json"))
    }
    (directory / "index.json").write_bytes(_canonical_json_bytes({"files": files}))


def _snapshot_files(directory: Path) -> dict[str, dict[int, Path]]:
    result: dict[str, dict[int, Path]] = {}
    for path in sorted(directory.glob("*.json")):
        match = _SNAPSHOT_NAME.fullmatch(path.name)
        if match is None:
            continue
        result.setdefault(match.group("name"), {})[int(match.group("version"))] = path
    return result


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; importing or testing this module never writes files."""
    parser = argparse.ArgumentParser(
        description="도구 계약 스냅샷을 명시적으로 생성합니다."
    )
    parser.add_argument("--snapshot-dir", type=Path, default=_DEFAULT_DIRECTORY)
    args = parser.parse_args(argv)
    write_current_snapshots(args.snapshot_dir)
    validate_snapshots(args.snapshot_dir)
    validate_snapshot_index(args.snapshot_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
