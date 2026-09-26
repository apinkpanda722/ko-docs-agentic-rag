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

from kodocs.config import MissingAPIKeyError, load_settings
from kodocs.costs import (
    DEFAULT_LEDGER_PATH,
    BudgetExceededError,
    BudgetGuard,
    CostLedger,
)
from kodocs.ingest.manifest import (
    append_record,
    latest_records,
    load_records,
    now_iso,
    repo_relative,
    sha256_file,
)
from kodocs.parsing.docling_adapter import DoclingAdapter
from kodocs.parsing.pypdf_adapter import PARSER_NAME as PYPDF_PARSER_NAME
from kodocs.parsing.pypdf_adapter import parse_pdf
from kodocs.parsing.schema import (
    ParsedDocument,
    content_stats,
    from_json,
    output_path,
    settings_hash,
    to_json,
)
from kodocs.parsing.upstage_adapter import (
    DryRunPlan,
    UpstageAdapter,
    UpstageClient,
    plan,
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
        "api_version",
        "usage_enhanced",
        "init_s",
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

    # Optional: adapters with a one-time model load may define
    # ``prepare() -> None``. The runner calls it once, before the first parse,
    # and records its duration as ``init_s`` instead of in any ``elapsed_s``.


@dataclass(frozen=True)
class RunSummary:
    """Counts and timing from one parser batch run."""

    ok_count: int
    skipped_count: int
    failed_count: int
    empty_count: int
    total_page_count: int
    elapsed_s: float
    seconds_per_page: float  # excludes init_s
    init_s: float | None = None  # one-time adapter preparation (model loading)


class ParseRunError(RuntimeError):
    """The shared target list could not be used for a parser run."""


class _PypdfAdapter:
    name = PYPDF_PARSER_NAME

    def __init__(self) -> None:
        self.parser_version = version("pypdf")

    def settings(self) -> Mapping[str, object]:
        return {"extraction_mode": "plain"}

    def parse(self, item: ParseInput, *, root: Path) -> ParsedDocument:
        return parse_pdf(item.input_path, root=root)


def _upstage_cli_only() -> Adapter:
    raise ParseRunError(
        "upstage 어댑터는 비용 확인을 거친 CLI에서만 생성할 수 있습니다."
    )


ADAPTERS: dict[str, Callable[[], Adapter]] = {
    "docling": DoclingAdapter,
    "pypdf": _PypdfAdapter,
    "upstage": _upstage_cli_only,
}


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
    run_id: str | None = None,
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
    run_id = run_id or str(uuid.uuid4())
    started = time.perf_counter()
    ok_count = 0
    skipped_count = 0
    failed_count = 0
    empty_count = 0
    total_page_count = 0
    total = len(inputs)
    prepared = False
    init_s: float | None = None

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
                init_s=init_s,
                document_settings=None,
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
                init_s=init_s,
                document_settings=existing.settings,
            )
            _append_run(manifest_path, record)
            latest_by_key[key] = record
            report_progress(index)
            continue

        if not prepared:
            prepared = True
            init_s = _prepare(adapter)
            # Model loading must not count toward this document's elapsed_s.
            item_started = time.perf_counter()

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
                init_s=init_s,
                document_settings=None,
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
            init_s=init_s,
            document_settings=document.settings,
        )
        _append_run(manifest_path, record)
        latest_by_key[key] = record
        report_progress(index)

    elapsed_s = time.perf_counter() - started
    parse_elapsed_s = elapsed_s - (init_s or 0.0)
    seconds_per_page = parse_elapsed_s / total_page_count if total_page_count else 0.0
    summary = RunSummary(
        ok_count=ok_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        empty_count=empty_count,
        total_page_count=total_page_count,
        elapsed_s=elapsed_s,
        seconds_per_page=seconds_per_page,
        init_s=init_s,
    )
    init_text = "none" if init_s is None else f"{init_s:.1f}s"
    print(
        f"[{adapter.name}] summary ok={ok_count} skipped={skipped_count} "
        f"failed={failed_count} empty={empty_count} pages={total_page_count} "
        f"elapsed={elapsed_s:.1f}s init={init_text} "
        f"sec/page={seconds_per_page:.3f} (init 제외)",
        file=out,
    )
    return summary


def main(
    argv: list[str] | None = None,
    *,
    input_fn: Callable[[str], str] = input,
) -> int:
    """CLI entry point for user-initiated local parser runs."""
    parser = argparse.ArgumentParser(
        description="공통 M2 대상 목록을 선택한 파서로 실행합니다."
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--parser", choices=sorted(ADAPTERS), required=True)
    parser.add_argument("--limit", type=_positive_int)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="업스테이지 캐시 미적중 대상의 예상 비용만 출력합니다.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="업스테이지 유료 실행 확인을 생략합니다.",
    )
    args = parser.parse_args(argv)

    try:
        inputs = load_inputs(args.root, limit=args.limit)
        if args.parser == "upstage":
            dry_run_plan = plan(args.root, inputs)
            _print_upstage_plan(dry_run_plan)
            if dry_run_plan.exceeds_budget:
                print(
                    "예산 초과: 예상 비용이 이번 달 남은 예산을 넘어 "
                    "실행할 수 없습니다."
                )
                return 1
            if args.dry_run:
                return 0
            if (
                not args.yes
                and input_fn("계속하려면 y 입력: ").strip().casefold() != "y"
            ):
                return 0

            settings = load_settings()
            try:
                api_key = settings.require_key("UPSTAGE_API_KEY")
            except MissingAPIKeyError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            run_id = str(uuid.uuid4())
            adapter = _build_upstage_adapter(
                args.root,
                api_key=api_key,
                monthly_budget_usd=settings.monthly_budget_usd,
                run_id=run_id,
            )
            summary = run(
                args.root,
                adapter,
                inputs,
                force=args.force,
                run_id=run_id,
            )
            return 1 if summary.failed_count else 0

        if args.dry_run or args.yes:
            parser.error(
                "--dry-run과 --yes는 --parser upstage에서만 사용할 수 있습니다."
            )
        summary = run(
            args.root,
            ADAPTERS[args.parser](),
            inputs,
            force=args.force,
        )
    except (BudgetExceededError, ParseRunError, OSError, ValueError) as exc:
        print(f"파싱 실행 실패: {exc}", file=sys.stderr)
        return 1
    return 1 if summary.failed_count else 0


def _build_upstage_adapter(
    root: Path,
    *,
    api_key: str,
    monthly_budget_usd: float,
    run_id: str,
) -> UpstageAdapter:
    ledger = CostLedger(root / DEFAULT_LEDGER_PATH)
    return UpstageAdapter(
        root,
        client_factory=lambda: UpstageClient(api_key),
        ledger=ledger,
        guard=BudgetGuard(ledger, monthly_budget_usd),
        run_id=run_id,
    )


def _print_upstage_plan(dry_run_plan: DryRunPlan) -> None:
    print(
        f"캐시 없음 {dry_run_plan.uncached_count}건 / "
        f"{dry_run_plan.page_count}쪽 / "
        f"예상 ${dry_run_plan.estimated_usd:.2f} (세금 10% 포함) / "
        f"이번 달 누적 ${dry_run_plan.month_total_usd:.2f} / "
        f"상한 ${dry_run_plan.monthly_budget_usd:.2f} / "
        f"실행 후 남은 예산 ${dry_run_plan.remaining_after_usd:.2f}"
    )


def _prepare(adapter: Adapter) -> float | None:
    """Run the adapter's optional one-time preparation and return its duration."""
    prepare = getattr(adapter, "prepare", None)
    if not callable(prepare):
        return None
    started = time.perf_counter()
    try:
        prepare()
    except BudgetExceededError:
        raise
    # Without a prepared parser no input can succeed, so the run stops here.
    except Exception as exc:
        raise ParseRunError(
            f"파서 초기화 실패: {type(exc).__name__}: {_first_line(str(exc))}"
        ) from exc
    return time.perf_counter() - started


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
    init_s: float | None,
    document_settings: Mapping[str, object] | None,
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
        "api_version": (
            document_settings.get("apiVersion")
            if document_settings is not None
            else None
        ),
        "usage_enhanced": (
            document_settings.get("usage_enhanced")
            if document_settings is not None
            else None
        ),
        "init_s": init_s,
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
