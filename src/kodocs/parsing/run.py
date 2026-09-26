"""Shared, resumable batch runner for parser adapters."""

import argparse
import json
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Protocol, TextIO

from kodocs.costs import BudgetExceededError
from kodocs.ingest.manifest import (
    append_record,
    latest_records,
    load_records,
    now_iso,
    repo_relative,
    sha256_file,
)
from kodocs.parsing.pypdf_adapter import PARSER_NAME, parse_pdf
from kodocs.parsing.schema import (
    ParsedDocument,
    content_stats,
    from_json,
    output_path,
    settings_hash,
    to_json,
)

PARSE_RUN_REQUIRED_FIELDS = frozenset(
    {
        "run_id",
        "parser",
        "parser_version",
        "settings_hash",
        "input_sha256",
        "input_kind",
        "target_key",
        "status",
        "elapsed_s",
        "page_count",
        "text_chars",
        "hangul_distinct",
        "blocks",
        "tables",
        "invalid_tables",
        "error_type",
        "error",
        "ts",
    }
)


@dataclass(frozen=True)
class ParseInput:
    """One parser input resolved from the shared target manifest."""

    target_key: str
    input_path: Path
    input_sha256: str
    input_kind: str
    page_count: int | None


class Adapter(Protocol):
    """Common interface implemented by parser-specific adapters."""

    name: str

    def settings(self) -> Mapping[str, object]: ...

    def parse(self, item: ParseInput, *, root: Path) -> ParsedDocument: ...


@dataclass(frozen=True)
class RunSummary:
    """Counts and timing from one parser batch run."""

    ok_count: int
    skipped_count: int
    failed_count: int
    empty_count: int
    total_page_count: int
    elapsed_s: float
    seconds_per_page: float


class ParseRunError(RuntimeError):
    """The shared target list could not be used for a parser run."""


class _PypdfAdapter:
    name = PARSER_NAME

    def __init__(self) -> None:
        self.parser_version = version("pypdf")

    def settings(self) -> Mapping[str, object]:
        return {"extraction_mode": "plain"}

    def parse(self, item: ParseInput, *, root: Path) -> ParsedDocument:
        return parse_pdf(item.input_path, root=root)


ADAPTERS: dict[str, Callable[[], Adapter]] = {"pypdf": _PypdfAdapter}


def load_inputs(root: Path, *, limit: int | None = None) -> list[ParseInput]:
    """Load targets and hash each available PDF from disk."""
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")

    target_path = root / "data/manifest/parse_targets.json"
    try:
        payload = json.loads(target_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ParseRunError(
            "data/manifest/parse_targets.json이 없습니다. "
            "먼저 make parse-targets를 실행하세요."
        ) from exc
    except json.JSONDecodeError as exc:
        raise ParseRunError(
            "data/manifest/parse_targets.json을 읽을 수 없습니다: "
            f"JSONDecodeError: {exc.msg}"
        ) from exc

    targets = payload.get("targets") if isinstance(payload, dict) else None
    if not isinstance(targets, list) or not targets:
        raise ParseRunError(
            "파싱 대상이 0건입니다. 먼저 make parse-targets를 실행하세요."
        )

    selected = targets if limit is None else targets[:limit]
    inputs: list[ParseInput] = []
    for index, target in enumerate(selected, start=1):
        if not isinstance(target, dict):
            raise ParseRunError(
                f"parse_targets.json targets[{index - 1}]가 객체가 아닙니다."
            )
        law_rule_id = _text(target.get("law_rule_id"))
        law_serial = _text(target.get("law_serial"))
        source_sha256 = _text(target.get("source_sha256"))
        target_key = f"{law_rule_id}:{law_serial}:{source_sha256}"

        pdf_path = target.get("pdf_path")
        if isinstance(pdf_path, str) and pdf_path:
            input_path = root / pdf_path
        else:
            input_path = (
                root
                / "data/raw/pdf/.missing"
                / f"{index}-{source_sha256 or 'unknown'}.pdf"
            )

        input_sha256 = ""
        try:
            if input_path.is_file():
                input_sha256 = sha256_file(input_path)
        except OSError:
            # run() records unreadable files per input and continues the batch.
            pass

        page_count = target.get("page_count")
        inputs.append(
            ParseInput(
                target_key=target_key,
                input_path=input_path,
                input_sha256=input_sha256,
                input_kind="pdf",
                page_count=(
                    page_count
                    if isinstance(page_count, int) and not isinstance(page_count, bool)
                    else None
                ),
            )
        )
    return inputs


def run(
    root: Path,
    adapter: Adapter,
    inputs: Sequence[ParseInput],
    *,
    force: bool = False,
    progress_every: int = 10,
    out: TextIO = sys.stdout,
) -> RunSummary:
    """Run one adapter across inputs, recording every non-budget outcome."""
    if progress_every < 1:
        raise ValueError("progress_every must be at least 1")
    if not inputs:
        raise ParseRunError(
            "파싱 대상이 0건입니다. 먼저 make parse-targets를 실행하세요."
        )

    current_settings = dict(adapter.settings())
    current_settings_hash = settings_hash(current_settings)
    manifest_path = root / "data/manifest/parse_runs.jsonl"
    latest_by_key = {
        (record.get("parser"), record.get("input_sha256")): record
        for record in latest_records(
            load_records(manifest_path),
            lambda record: (record.get("parser"), record.get("input_sha256")),
        )
    }
    conversion_hashes = _conversion_hashes(root)
    run_id = str(uuid.uuid4())
    started = time.perf_counter()
    ok_count = 0
    skipped_count = 0
    failed_count = 0
    empty_count = 0
    total_page_count = 0
    total = len(inputs)

    def report_progress(index: int) -> None:
        if index % progress_every == 0 or index == total:
            elapsed = time.perf_counter() - started
            print(
                f"[{adapter.name}] {index}/{total} ok={ok_count} "
                f"skipped={skipped_count} failed={failed_count} "
                f"empty={empty_count} elapsed={elapsed:.1f}s",
                file=out,
                flush=True,
            )

    for index, item in enumerate(inputs, start=1):
        item_started = time.perf_counter()
        key = (adapter.name, item.input_sha256)
        prior = latest_by_key.get(key)
        parser_version = _adapter_version(adapter, prior)

        validation_error = _validate_input(root, item, conversion_hashes)
        if validation_error is not None:
            error_type, error = validation_error
            failed_count += 1
            record = _record(
                run_id=run_id,
                parser=adapter.name,
                parser_version=parser_version,
                settings_hash_value=current_settings_hash,
                item=item,
                status="failed",
                elapsed_s=time.perf_counter() - item_started,
                page_count=item.page_count,
                stats=None,
                error_type=error_type,
                error=error,
            )
            _append_run(manifest_path, record)
            latest_by_key[key] = record
            report_progress(index)
            continue

        destination = output_path(root, adapter.name, item.input_sha256)
        existing = (
            None if force else _current_output(destination, current_settings_hash)
        )
        if existing is not None:
            stats = content_stats(existing)
            skipped_count += 1
            page_count = existing.page_count
            if page_count is not None:
                total_page_count += page_count
            record = _record(
                run_id=run_id,
                parser=adapter.name,
                parser_version=existing.parser_version,
                settings_hash_value=current_settings_hash,
                item=item,
                status="skipped",
                elapsed_s=time.perf_counter() - item_started,
                page_count=page_count,
                stats=stats,
                error_type=None,
                error=None,
            )
            _append_run(manifest_path, record)
            latest_by_key[key] = record
            report_progress(index)
            continue

        try:
            document = adapter.parse(item, root=root)
            if document.settings_hash != current_settings_hash:
                raise ValueError(
                    "adapter output settings_hash does not match adapter settings"
                )
            stats = content_stats(document)
            _write_atomic(destination, to_json(document))
        except BudgetExceededError:
            raise
        # Parser libraries raise heterogeneous exceptions; FP-030 requires one
        # bad input to be recorded without ending the batch.
        except Exception as exc:  # noqa: BLE001
            failed_count += 1
            record = _record(
                run_id=run_id,
                parser=adapter.name,
                parser_version=parser_version,
                settings_hash_value=current_settings_hash,
                item=item,
                status="failed",
                elapsed_s=time.perf_counter() - item_started,
                page_count=item.page_count,
                stats=None,
                error_type=type(exc).__name__,
                error=_first_line(str(exc)),
            )
            _append_run(manifest_path, record)
            latest_by_key[key] = record
            report_progress(index)
            continue

        status = "empty" if stats["hangul_distinct"] == 0 else "ok"
        if status == "empty":
            empty_count += 1
        else:
            ok_count += 1
        page_count = document.page_count
        if page_count is not None:
            total_page_count += page_count
        record = _record(
            run_id=run_id,
            parser=adapter.name,
            parser_version=document.parser_version,
            settings_hash_value=current_settings_hash,
            item=item,
            status=status,
            elapsed_s=time.perf_counter() - item_started,
            page_count=page_count,
            stats=stats,
            error_type=None,
            error=None,
        )
        _append_run(manifest_path, record)
        latest_by_key[key] = record
        report_progress(index)

    elapsed_s = time.perf_counter() - started
    seconds_per_page = elapsed_s / total_page_count if total_page_count else 0.0
    summary = RunSummary(
        ok_count=ok_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        empty_count=empty_count,
        total_page_count=total_page_count,
        elapsed_s=elapsed_s,
        seconds_per_page=seconds_per_page,
    )
    print(
        f"[{adapter.name}] summary ok={ok_count} skipped={skipped_count} "
        f"failed={failed_count} empty={empty_count} pages={total_page_count} "
        f"elapsed={elapsed_s:.1f}s sec/page={seconds_per_page:.3f}",
        file=out,
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for user-initiated local parser runs."""
    parser = argparse.ArgumentParser(
        description="공통 M2 대상 목록을 선택한 파서로 실행합니다."
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--parser", choices=sorted(ADAPTERS), required=True)
    parser.add_argument("--limit", type=_positive_int)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    try:
        inputs = load_inputs(args.root, limit=args.limit)
        summary = run(
            args.root,
            ADAPTERS[args.parser](),
            inputs,
            force=args.force,
        )
    except (ParseRunError, OSError, ValueError) as exc:
        print(f"파싱 실행 실패: {exc}", file=sys.stderr)
        return 1
    return 1 if summary.failed_count else 0


def _validate_input(
    root: Path,
    item: ParseInput,
    conversion_hashes: Mapping[str, str],
) -> tuple[str, str] | None:
    if not item.input_path.is_file():
        return "missing_input", "PDF 입력 파일이 없습니다."
    try:
        actual_sha256 = sha256_file(item.input_path)
    except OSError as exc:
        return type(exc).__name__, _first_line(str(exc))
    if actual_sha256 != item.input_sha256:
        return (
            "input_sha256_mismatch",
            "PDF 입력 파일의 SHA-256이 대상 로드 시 계산값과 다릅니다.",
        )
    try:
        relative_path = repo_relative(item.input_path, root)
    except ValueError:
        relative_path = ""
    expected_sha256 = conversion_hashes.get(relative_path)
    if expected_sha256 is not None and actual_sha256 != expected_sha256:
        return (
            "input_sha256_mismatch",
            "PDF 입력 파일의 SHA-256이 변환 manifest 값과 다릅니다.",
        )
    return None


def _conversion_hashes(root: Path) -> dict[str, str]:
    records = latest_records(
        load_records(root / "data/manifest/conversions.jsonl"),
        lambda record: record.get("output_path"),
    )
    return {
        output_path_value: output_sha256
        for record in records
        if isinstance((output_path_value := record.get("output_path")), str)
        and isinstance((output_sha256 := record.get("output_sha256")), str)
    }


def _current_output(path: Path, expected_settings_hash: str) -> ParsedDocument | None:
    if not path.is_file():
        return None
    try:
        document = from_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return document if document.settings_hash == expected_settings_hash else None


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as destination:
            destination.write(f"{text}\n")
            temporary_path = Path(destination.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _record(
    *,
    run_id: str,
    parser: str,
    parser_version: str,
    settings_hash_value: str,
    item: ParseInput,
    status: str,
    elapsed_s: float,
    page_count: int | None,
    stats: Mapping[str, int] | None,
    error_type: str | None,
    error: str | None,
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "parser": parser,
        "parser_version": parser_version,
        "settings_hash": settings_hash_value,
        "input_sha256": item.input_sha256,
        "input_kind": item.input_kind,
        "target_key": item.target_key,
        "status": status,
        "elapsed_s": elapsed_s,
        "page_count": page_count,
        "text_chars": stats["text_chars"] if stats is not None else 0,
        "hangul_distinct": stats["hangul_distinct"] if stats is not None else 0,
        "blocks": stats["blocks"] if stats is not None else 0,
        "tables": stats["tables"] if stats is not None else 0,
        "invalid_tables": stats["invalid_tables"] if stats is not None else 0,
        "error_type": error_type,
        "error": _first_line(error) if error is not None else None,
        "ts": now_iso(),
    }


def _append_run(path: Path, record: Mapping[str, object]) -> None:
    append_record(path, record, required=PARSE_RUN_REQUIRED_FIELDS)


def _adapter_version(adapter: Adapter, prior: Mapping[str, object] | None) -> str:
    value = getattr(adapter, "parser_version", None)
    if isinstance(value, str) and value:
        return value
    if prior is not None:
        prior_value = prior.get("parser_version")
        if isinstance(prior_value, str):
            return prior_value
    return "unknown"


def _first_line(message: str) -> str:
    lines = message.splitlines()
    return (lines[0] if lines else "")[:200]


def _text(value: object) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("정수를 입력해야 합니다") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("1 이상의 정수를 입력해야 합니다")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
