"""Tests for the shared parser batch runner."""

import hashlib
import io
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from kodocs.costs import BudgetExceededError
from kodocs.parsing.run import ParseInput, ParseRunError, load_inputs, run
from kodocs.parsing.schema import (
    SCHEMA_VERSION,
    Block,
    ParsedDocument,
    TableCell,
    build_table,
    settings_hash,
)


class FakeAdapter:
    """Small configurable adapter used without opening real PDFs."""

    name = "fake"

    def __init__(
        self,
        *,
        settings: Mapping[str, object] | None = None,
        errors: Mapping[str, Exception] | None = None,
        empty: frozenset[str] = frozenset(),
        invalid_table: frozenset[str] = frozenset(),
    ) -> None:
        self._settings = dict(settings or {"mode": "plain"})
        self.errors = dict(errors or {})
        self.empty = empty
        self.invalid_table = invalid_table
        self.calls: list[str] = []

    def settings(self) -> Mapping[str, object]:
        return self._settings

    def parse(self, item: ParseInput, *, root: Path) -> ParsedDocument:
        del root
        self.calls.append(item.target_key)
        if error := self.errors.get(item.target_key):
            raise error

        blocks = (
            ()
            if item.target_key in self.empty
            else (Block(page=1, kind="text", text=f"한글 내용 {item.target_key}"),)
        )
        tables = ()
        if item.target_key in self.invalid_table:
            invalid, invalid_text = build_table(
                1,
                1,
                (
                    TableCell(0, 0, 1, 2, "겹침"),
                    TableCell(0, 1, 1, 1, "셀"),
                ),
            )
            blocks += (Block(page=1, kind="table", text=invalid_text),)
            tables = (invalid,)
        return ParsedDocument(
            schema_version=SCHEMA_VERSION,
            parser=self.name,
            parser_version="fake-1.0",
            settings=self._settings,
            settings_hash=settings_hash(self._settings),
            input_path=item.input_path.as_posix(),
            input_sha256=item.input_sha256,
            input_kind="pdf",
            page_count=item.page_count,
            blocks=blocks,
            tables=tables,
            elapsed_s=0.01,
        )


class FlushTrackingIO(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flush_count = 0

    def flush(self) -> None:
        self.flush_count += 1
        super().flush()


def _input(
    root: Path,
    name: str,
    *,
    target_key: str | None = None,
    page_count: int | None = 1,
) -> ParseInput:
    path = root / "data/raw/pdf" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"pdf bytes for {name}".encode())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return ParseInput(
        target_key=target_key or name,
        input_path=path,
        input_sha256=digest,
        input_kind="pdf",
        page_count=page_count,
    )


def _records(root: Path) -> list[dict[str, object]]:
    path = root / "data/manifest/parse_runs.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write_targets(root: Path, targets: list[dict[str, object]]) -> None:
    path = root / "data/manifest/parse_targets.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"targets": targets}), encoding="utf-8")


def test_load_inputs_hashes_pdf_builds_target_key_and_honors_limit(
    tmp_path: Path,
) -> None:
    first = _input(tmp_path, "first.pdf")
    second = _input(tmp_path, "second.pdf")
    _write_targets(
        tmp_path,
        [
            {
                "law_rule_id": "rule-1",
                "law_serial": "serial-1",
                "source_sha256": "a" * 64,
                "pdf_path": first.input_path.relative_to(tmp_path).as_posix(),
                "page_count": 3,
            },
            {
                "law_rule_id": "rule-2",
                "law_serial": "serial-2",
                "source_sha256": "b" * 64,
                "pdf_path": second.input_path.relative_to(tmp_path).as_posix(),
                "page_count": None,
            },
        ],
    )

    loaded = load_inputs(tmp_path, limit=1)

    assert loaded == [
        ParseInput(
            target_key=f"rule-1:serial-1:{'a' * 64}",
            input_path=first.input_path,
            input_sha256=first.input_sha256,
            input_kind="pdf",
            page_count=3,
        )
    ]


def test_load_inputs_keeps_missing_pdf_for_per_input_failure(tmp_path: Path) -> None:
    _write_targets(
        tmp_path,
        [
            {
                "law_rule_id": "rule-1",
                "law_serial": "serial-1",
                "source_sha256": "a" * 64,
                "page_count": 3,
            }
        ],
    )

    loaded = load_inputs(tmp_path)

    assert len(loaded) == 1
    assert loaded[0].target_key == f"rule-1:serial-1:{'a' * 64}"
    assert loaded[0].input_sha256 == ""
    assert not loaded[0].input_path.is_file()


@pytest.mark.parametrize(
    "payload",
    [{"targets": []}, {}],
    ids=["empty-targets", "missing-targets"],
)
def test_load_inputs_rejects_zero_targets_with_regeneration_guidance(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    path = tmp_path / "data/manifest/parse_targets.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ParseRunError, match="make parse-targets"):
        load_inputs(tmp_path)


def test_run_records_invalid_table_as_ok_empty_and_failure_then_continues(
    tmp_path: Path,
) -> None:
    invalid = _input(tmp_path, "invalid.pdf", target_key="invalid", page_count=2)
    failed = _input(tmp_path, "failed.pdf", target_key="failed", page_count=4)
    empty = _input(tmp_path, "empty.pdf", target_key="empty", page_count=1)
    adapter = FakeAdapter(
        errors={"failed": RuntimeError("first line\nsecret second line")},
        empty=frozenset({"empty"}),
        invalid_table=frozenset({"invalid"}),
    )
    out = FlushTrackingIO()

    summary = run(
        tmp_path,
        adapter,
        [invalid, failed, empty],
        progress_every=2,
        out=out,
    )

    assert adapter.calls == ["invalid", "failed", "empty"]
    assert (summary.ok_count, summary.failed_count, summary.empty_count) == (1, 1, 1)
    records = _records(tmp_path)
    assert [record["status"] for record in records] == ["ok", "failed", "empty"]
    assert records[0]["invalid_tables"] == 1
    assert records[1]["error_type"] == "RuntimeError"
    assert records[1]["error"] == "first line"
    assert records[2]["hangul_distinct"] == 0
    manifest_text = (tmp_path / "data/manifest/parse_runs.jsonl").read_text(
        encoding="utf-8"
    )
    assert "한글 내용" not in manifest_text
    progress = [
        line for line in out.getvalue().splitlines() if line.startswith("[fake]")
    ]
    assert progress[0].startswith("[fake] 2/3 ok=1 skipped=0 failed=1 empty=0 elapsed=")
    assert progress[1].startswith("[fake] 3/3 ok=1 skipped=0 failed=1 empty=1 elapsed=")
    assert out.flush_count == 2


def test_run_uses_parser_hash_for_skip_reparse_force_and_duplicate(
    tmp_path: Path,
) -> None:
    item = _input(tmp_path, "same.pdf", target_key="first", page_count=2)
    duplicate = replace(item, target_key="duplicate")
    first_adapter = FakeAdapter(settings={"mode": "plain"})

    first = run(tmp_path, first_adapter, [item, duplicate], out=io.StringIO())
    same_adapter = FakeAdapter(settings={"mode": "plain"})
    second = run(tmp_path, same_adapter, [item], out=io.StringIO())
    changed_adapter = FakeAdapter(settings={"mode": "layout"})
    third = run(tmp_path, changed_adapter, [item], out=io.StringIO())
    forced_adapter = FakeAdapter(settings={"mode": "layout"})
    fourth = run(tmp_path, forced_adapter, [item], force=True, out=io.StringIO())

    assert first_adapter.calls == ["first"]
    assert (first.ok_count, first.skipped_count) == (1, 1)
    assert same_adapter.calls == []
    assert second.skipped_count == 1
    assert changed_adapter.calls == ["first"]
    assert third.ok_count == 1
    assert forced_adapter.calls == ["first"]
    assert fourth.ok_count == 1
    assert [record["status"] for record in _records(tmp_path)] == [
        "ok",
        "skipped",
        "skipped",
        "ok",
        "ok",
    ]


def test_run_records_missing_and_changed_inputs_without_calling_adapter(
    tmp_path: Path,
) -> None:
    missing = ParseInput(
        target_key="missing",
        input_path=tmp_path / "missing.pdf",
        input_sha256="",
        input_kind="pdf",
        page_count=None,
    )
    changed = _input(tmp_path, "changed.pdf", target_key="changed")
    changed.input_path.write_bytes(b"changed after discovery")
    good = _input(tmp_path, "good.pdf", target_key="good")
    adapter = FakeAdapter()

    summary = run(tmp_path, adapter, [missing, changed, good], out=io.StringIO())

    assert summary.failed_count == 2
    assert adapter.calls == ["good"]
    records = _records(tmp_path)
    assert [record["error_type"] for record in records] == [
        "missing_input",
        "input_sha256_mismatch",
        None,
    ]


def test_run_rejects_conversion_manifest_hash_mismatch(tmp_path: Path) -> None:
    item = _input(tmp_path, "manifest.pdf", target_key="manifest")
    manifest = tmp_path / "data/manifest/conversions.jsonl"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "output_path": item.input_path.relative_to(tmp_path).as_posix(),
                "output_sha256": "f" * 64,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    adapter = FakeAdapter()

    summary = run(tmp_path, adapter, [item], out=io.StringIO())

    assert summary.failed_count == 1
    assert adapter.calls == []
    assert _records(tmp_path)[0]["error_type"] == "input_sha256_mismatch"


def test_budget_exceeded_stops_batch_without_failed_record(tmp_path: Path) -> None:
    first = _input(tmp_path, "budget.pdf", target_key="budget")
    second = _input(tmp_path, "never.pdf", target_key="never")
    adapter = FakeAdapter(errors={"budget": BudgetExceededError("monthly cap")})

    with pytest.raises(BudgetExceededError, match="monthly cap"):
        run(tmp_path, adapter, [first, second], out=io.StringIO())

    assert adapter.calls == ["budget"]
    assert not (tmp_path / "data/manifest/parse_runs.jsonl").exists()


class PreparingAdapter(FakeAdapter):
    """Adapter whose one-time initialization is slow."""

    def __init__(self, clock: list[float], **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.clock = clock
        self.prepare_calls = 0

    def prepare(self) -> None:
        self.prepare_calls += 1
        self.clock[0] += 100.0

    def parse(self, item: ParseInput, *, root: Path) -> ParsedDocument:
        self.clock[0] += 1.0
        return super().parse(item, root=root)


def test_run_prepares_once_and_records_init_time_separately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kodocs.parsing.run as run_module

    clock = [0.0]
    monkeypatch.setattr(run_module.time, "perf_counter", lambda: clock[0])
    adapter = PreparingAdapter(clock)
    inputs = [
        _input(tmp_path, "a.pdf", page_count=2),
        _input(tmp_path, "b.pdf", page_count=2),
    ]

    summary = run(tmp_path, adapter, inputs, out=io.StringIO())

    records = _records(tmp_path)
    assert adapter.prepare_calls == 1
    assert summary.init_s == 100.0
    assert [record["elapsed_s"] for record in records] == [1.0, 1.0]
    assert all(record["init_s"] == 100.0 for record in records)
    assert summary.seconds_per_page == pytest.approx(0.5)


def test_run_skips_prepare_when_every_input_is_already_parsed(tmp_path: Path) -> None:
    inputs = [_input(tmp_path, "a.pdf")]
    run(tmp_path, FakeAdapter(), inputs, out=io.StringIO())
    adapter = PreparingAdapter([0.0])

    summary = run(tmp_path, adapter, inputs, out=io.StringIO())

    assert adapter.prepare_calls == 0
    assert summary.init_s is None
    assert _records(tmp_path)[-1]["init_s"] is None


def test_run_stops_when_adapter_initialization_fails(tmp_path: Path) -> None:
    class BrokenPrepare(FakeAdapter):
        def prepare(self) -> None:
            raise RuntimeError("model download missing")

    adapter = BrokenPrepare()

    with pytest.raises(ParseRunError, match="RuntimeError"):
        run(tmp_path, adapter, [_input(tmp_path, "a.pdf")], out=io.StringIO())
    assert adapter.calls == []


def test_run_copies_per_document_skip_and_table_drop_counts_into_records(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    class CountingAdapter(FakeAdapter):
        def parse(self, item: ParseInput, *, root: Path) -> ParsedDocument:
            document = super().parse(item, root=root)
            return replace(
                document,
                settings={
                    **document.settings,
                    "skipped_items": {"picture": 1},
                    "table_dropped_warnings": 2,
                },
            )

    inputs = [_input(tmp_path, "a.pdf")]
    run(tmp_path, CountingAdapter(), inputs, out=io.StringIO())
    run(tmp_path, CountingAdapter(), inputs, out=io.StringIO())
    run(tmp_path, FakeAdapter(), [_input(tmp_path, "b.pdf")], out=io.StringIO())

    parsed, skipped, other = _records(tmp_path)
    assert (parsed["status"], skipped["status"]) == ("ok", "skipped")
    for record in (parsed, skipped):
        assert record["skipped_items"] == {"picture": 1}
        assert record["table_dropped_warnings"] == 2
    assert other["skipped_items"] is None
    assert other["table_dropped_warnings"] is None
