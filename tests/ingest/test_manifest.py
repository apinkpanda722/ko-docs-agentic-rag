"""Tests for provenance manifest helpers."""

import json
import re
from pathlib import Path

import pytest

from kodocs.ingest.manifest import (
    LICENSE_NOTE_NO_KOGL,
    append_record,
    load_records,
    now_iso,
    repo_relative,
    sanitize_url,
    sha256_file,
)


def raw_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "source": "공개 출처",
        "source_url": "https://example.test/file?target=pi&type=JSON",
        "local_path": "data/raw/source/file.hwp",
        "sha256": "a" * 64,
        "size_bytes": 123,
        "collected_at": "2026-09-26T10:20:30+09:00",
        "license_note": LICENSE_NOTE_NO_KOGL,
    }
    record.update(overrides)
    return record


def test_sanitize_url_removes_secret_params_case_insensitively_in_place() -> None:
    url = (
        "https://example.test/search?target=pi&OC=first&type=JSON&oc=second&blank="
        "#result"
    )

    assert sanitize_url(url) == (
        "https://example.test/search?target=pi&type=JSON&blank=#result"
    )


def test_append_record_rejects_missing_required_fields(tmp_path: Path) -> None:
    record = raw_record()
    del record["sha256"]

    with pytest.raises(ValueError, match="sha256"):
        append_record(tmp_path / "manifest.jsonl", record)


@pytest.mark.parametrize("secret_name", ["OC", "oc", "Oc"])
def test_append_record_rejects_secret_source_url(
    tmp_path: Path,
    secret_name: str,
) -> None:
    record = raw_record(
        source_url=f"https://example.test/file?target=pi&{secret_name}=secret"
    )

    with pytest.raises(ValueError, match="secret"):
        append_record(tmp_path / "manifest.jsonl", record)


def test_append_record_rejects_absolute_local_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="relative"):
        append_record(
            tmp_path / "manifest.jsonl",
            raw_record(local_path="/tmp/download.hwp"),
        )


@pytest.mark.parametrize("forbidden_key", ["담당자명", "전화번호"])
def test_append_record_rejects_forbidden_keys_at_any_depth(
    tmp_path: Path,
    forbidden_key: str,
) -> None:
    record = raw_record(metadata={"items": [{forbidden_key: "비공개"}]})

    with pytest.raises(ValueError, match=forbidden_key):
        append_record(tmp_path / "manifest.jsonl", record)


def test_append_and_load_records_round_trip_without_escaping_korean(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "nested" / "manifest.jsonl"
    record = raw_record()

    append_record(manifest_path, record)

    serialized = manifest_path.read_text(encoding="utf-8")
    assert "공개 출처" in serialized
    assert "\\u" not in serialized
    assert load_records(manifest_path) == [record]


def test_load_records_returns_empty_list_for_missing_file(tmp_path: Path) -> None:
    assert load_records(tmp_path / "missing.jsonl") == []


def test_append_record_accepts_custom_required_fields(tmp_path: Path) -> None:
    manifest_path = tmp_path / "matches.jsonl"
    record = {"left_version": "v1", "right_version": "v2"}

    append_record(
        manifest_path,
        record,
        required={"left_version", "right_version"},
    )

    assert load_records(manifest_path) == [record]


def test_sha256_file_hashes_content(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"kodocs")

    assert sha256_file(source) == (
        "4462863a0f24314fb96c4053ebccde27233cbc2841a70ee88eff33241b9149ad"
    )


def test_now_iso_is_kst_at_second_precision() -> None:
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+09:00", now_iso())


def test_repo_relative_returns_posix_path_and_rejects_outside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    file_path = root / "data" / "raw" / "file.hwp"
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(b"source")

    assert repo_relative(file_path, root) == "data/raw/file.hwp"
    with pytest.raises(ValueError):
        repo_relative(tmp_path / "outside.hwp", root)


def test_manifest_json_keys_are_sorted(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"
    append_record(manifest_path, raw_record())

    parsed_pairs = json.loads(
        manifest_path.read_text(encoding="utf-8"),
        object_pairs_hook=list,
    )
    assert [key for key, _ in parsed_pairs] == sorted(key for key, _ in parsed_pairs)
