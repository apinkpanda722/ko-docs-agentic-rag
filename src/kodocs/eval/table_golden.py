"""Index human-reviewed table goldens without copying their cell text."""

import argparse
import json
import sys
import tempfile
import warnings
from collections.abc import Mapping
from pathlib import Path

from kodocs.eval.table_drafts import parse_markdown_document
from kodocs.ingest.manifest import now_iso, sha256_file


class TableGoldenError(RuntimeError):
    """Reviewed table goldens are absent or malformed."""


def index_golden(root: Path) -> dict[str, object]:
    """Build a text-free index from reviewed ``data/golden/tables/*.md`` files."""
    directory = root / "data/golden/tables"
    files = (
        sorted(path for path in directory.glob("*.md") if path.name != "README.md")
        if directory.is_dir()
        else []
    )
    if not files:
        raise TableGoldenError(
            "표 정답이 0개입니다. make table-drafts 실행 → 사람이 후보를 검수 → "
            "확정 파일을 data/golden/tables/에 복사한 뒤 다시 실행하세요."
        )

    tables: list[dict[str, object]] = []
    for path in files:
        metadata, _ = parse_markdown_document(path.read_text(encoding="utf-8"))
        missing_review = [
            key for key in ("reviewed_by", "reviewed_on") if not metadata.get(key)
        ]
        if missing_review:
            warnings.warn(
                f"{path.name}: reviewed_by와 reviewed_on을 확인하세요 "
                f"(누락: {', '.join(missing_review)})",
                UserWarning,
                stacklevel=2,
            )
        shape = _shape(metadata.get("shape"), path)
        tables.append(
            {
                "id": path.stem,
                "rule_no": _required_string(metadata, "rule_no", path),
                "source_sha256": _required_string(metadata, "source_sha256", path),
                "pdf_sha256": _optional_string(metadata.get("pdf_sha256"), path),
                "section": _required_string(metadata, "section", path),
                "table_ordinal": _integer(metadata.get("table_ordinal"), path),
                "annex_label": _optional_string(metadata.get("annex_label"), path),
                "shape": shape,
                "golden_file_sha256": sha256_file(path),
                "reviewed_by": _optional_string(metadata.get("reviewed_by"), path),
                "reviewed_on": _optional_string(metadata.get("reviewed_on"), path),
            }
        )
    return {"generated_at": now_iso(), "tables": tables}


def main(argv: list[str] | None = None) -> int:
    """CLI entry point that commits only reviewed selection metadata and hashes."""
    parser = argparse.ArgumentParser(
        description="사람이 검수한 표 정답의 선정 정보와 해시를 기록합니다."
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    try:
        payload = index_golden(args.root)
        _write_json_atomic(
            args.root / "data/manifest/table_golden.json",
            payload,
        )
    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        TableGoldenError,
    ) as exc:
        print(f"표 정답 인덱스 실패: {exc}", file=sys.stderr)
        return 1
    print(f"표 정답 {len(payload['tables'])}개를 기록했습니다.")
    return 0


def _required_string(metadata: Mapping[str, object], key: str, path: Path) -> str:
    value = metadata.get(key)
    if not isinstance(value, str) or not value:
        raise TableGoldenError(f"{path.name}: {key}가 비어 있습니다.")
    return value


def _optional_string(value: object, path: Path) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TableGoldenError(f"{path.name}: 문자열 또는 null이어야 합니다.")
    return value or None


def _integer(value: object, path: Path) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TableGoldenError(
            f"{path.name}: table_ordinal은 0 이상의 정수여야 합니다."
        )
    return value


def _shape(value: object, path: Path) -> list[int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(
            not isinstance(item, int) or isinstance(item, bool) or item < 0
            for item in value
        )
    ):
        raise TableGoldenError(
            f"{path.name}: shape은 [rows, cols] 정수 목록이어야 합니다."
        )
    return list(value)


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as temporary:
        temporary.write(serialized)
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


if __name__ == "__main__":  # pragma: no cover - exercised through CLI AC
    raise SystemExit(main())
