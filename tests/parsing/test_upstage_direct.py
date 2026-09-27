"""Tests for the optional Upstage original-HWP input experiment."""

import hashlib
import io
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from kodocs.costs import BudgetExceededError, BudgetGuard, CostLedger
from kodocs.parsing import run as run_module
from kodocs.parsing.run import ParseInput, load_direct_inputs, run
from kodocs.parsing.upstage_adapter import (
    ESTIMATE_PAGE_FACTOR_DIRECT,
    PARSER_NAME_DIRECT,
    REQUEST_OPTIONS,
    DryRunPlan,
    UpstageClient,
    UpstageDirectAdapter,
    cache_path,
    plan,
)

SECRET = "up_direct_test_secret"


def _source(root: Path, relative: str, content: bytes) -> tuple[Path, str]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest()


def _target(
    root: Path,
    *,
    rule_no: str,
    source_kind: str,
    page_count: int | None,
    role: str = "current",
    suffix: str | None = None,
) -> dict[str, object]:
    extension = suffix or (".hwpx" if source_kind == "hwpx" else ".hwp")
    path, digest = _source(
        root,
        f"data/raw/alio/{rule_no}/한글 이름 {rule_no}{extension}",
        f"source-{rule_no}-{source_kind}".encode(),
    )
    return {
        "law_rule_id": f"law-{rule_no}",
        "law_serial": f"serial-{rule_no}",
        "rule_no": rule_no,
        "source_kind": source_kind,
        "source_path": path.relative_to(root).as_posix(),
        "source_sha256": digest,
        "page_count": page_count,
        "role": role,
    }


def _write_targets(root: Path, targets: list[dict[str, object]]) -> None:
    path = root / "data/manifest/parse_targets.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"targets": targets}), encoding="utf-8")


def _input(
    root: Path,
    *,
    kind: str = "hwp5",
    name: str = "한글 원본.hwp",
    page_count: int = 2,
) -> ParseInput:
    path, digest = _source(root, f"data/raw/alio/1/{name}", b"original-hwp")
    return ParseInput(
        target_key=f"law:serial:{digest}",
        input_path=path,
        input_sha256=digest,
        input_kind=kind,
        page_count=page_count,
        expected_sha256=digest,
    )


def _response(*, pages: int = 2) -> dict[str, object]:
    return {
        "apiVersion": "1.2",
        "model": "document-parse-test",
        "elements": [
            {
                "id": 1,
                "category": "paragraph",
                "page": 1,
                "content": {"text": "한글 본문"},
            }
        ],
        "usage": {"pages": pages, "standard": pages, "enhanced": 0},
    }


def _adapter(
    root: Path,
    client_factory: object,
    *,
    budget: float = 45.0,
) -> UpstageDirectAdapter:
    ledger = CostLedger(
        root / "data/manifest/costs.jsonl",
        clock=lambda: datetime(2026, 9, 27, tzinfo=UTC),
    )
    return UpstageDirectAdapter(
        root,
        client_factory=client_factory,  # type: ignore[arg-type]
        ledger=ledger,
        guard=BudgetGuard(ledger, budget),
        run_id="direct-run",
    )


def test_load_direct_inputs_prioritizes_golden_hwpx_then_skips_large_hwp5(
    tmp_path: Path,
) -> None:
    hwpx_plain = _target(tmp_path, rule_no="30", source_kind="hwpx", page_count=3)
    hwpx_golden = _target(tmp_path, rule_no="40", source_kind="hwpx", page_count=2)
    hwpx_no_pages = _target(tmp_path, rule_no="50", source_kind="hwpx", page_count=None)
    hwp_too_large = _target(tmp_path, rule_no="10", source_kind="hwp5", page_count=7)
    # Deliberately use a misleading suffix: source_kind, not the suffix, wins.
    hwp_fits = _target(
        tmp_path,
        rule_no="20",
        source_kind="hwp5",
        page_count=4,
        suffix=".bin",
    )
    hwp_after_count = _target(tmp_path, rule_no="25", source_kind="hwp5", page_count=1)
    history = _target(
        tmp_path,
        rule_no="5",
        source_kind="hwp5",
        page_count=1,
        role="history",
    )
    _write_targets(
        tmp_path,
        [
            hwpx_plain,
            hwp_after_count,
            hwpx_no_pages,
            history,
            hwp_too_large,
            hwpx_golden,
            hwp_fits,
        ],
    )
    golden_path = tmp_path / "data/manifest/table_golden.json"
    golden_path.write_text(
        json.dumps({"tables": [{"source_sha256": hwpx_golden["source_sha256"]}]}),
        encoding="utf-8",
    )

    selected = load_direct_inputs(tmp_path, max_pages=10, hwp5_count=1)

    assert [item.input_sha256 for item in selected] == [
        hwpx_golden["source_sha256"],
        hwpx_plain["source_sha256"],
        hwp_fits["source_sha256"],
    ]
    assert [item.input_kind for item in selected] == ["hwpx", "hwpx", "hwp5"]
    assert [item.page_count for item in selected] == [2, 3, 4]
    assert selected[-1].input_path.suffix == ".bin"


def test_direct_manifest_hash_mismatch_is_recorded_without_api_call(
    tmp_path: Path,
) -> None:
    target = _target(tmp_path, rule_no="1", source_kind="hwp5", page_count=1)
    target["source_sha256"] = "f" * 64
    _write_targets(tmp_path, [target])
    item = load_direct_inputs(tmp_path)[0]
    calls = 0

    def forbidden_client() -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("hash mismatch constructed an HTTP client")

    summary = run(
        tmp_path,
        _adapter(tmp_path, forbidden_client),
        [item],
        out=io.StringIO(),
    )

    record = json.loads(
        (tmp_path / "data/manifest/parse_runs.jsonl").read_text(encoding="utf-8")
    )
    assert item.input_sha256 != item.expected_sha256
    assert calls == 0
    assert summary.failed_count == 1
    assert record["error_type"] == "input_sha256_mismatch"


def test_direct_uses_same_options_original_filename_and_separate_cache(
    tmp_path: Path,
) -> None:
    item = _input(tmp_path)
    seen = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen += 1
        body = request.read()
        assert 'filename="한글 원본.hwp"'.encode() in body
        for name, value in REQUEST_OPTIONS.items():
            assert f'name="{name}"'.encode() in body
            assert f"\r\n\r\n{value}\r\n".encode() in body
        return httpx.Response(200, json=_response(), request=request)

    adapter = _adapter(
        tmp_path,
        lambda: UpstageClient(
            SECRET,
            transport=httpx.MockTransport(handler),
            min_interval_s=0,
        ),
    )
    first = adapter.parse(item, root=tmp_path)
    second = adapter.parse(item, root=tmp_path)

    assert seen == 1
    # 캐시 적중은 HTTP 요청이 없어 attempts만 다르다.
    assert [a["status"] for a in first.settings["attempts"]] == [200]
    assert second.settings["attempts"] == []
    assert replace(first, settings={**first.settings, "attempts": []}) == second
    assert first.parser == PARSER_NAME_DIRECT
    assert first.settings["input"] == "original"
    assert first.settings["request_options"] == REQUEST_OPTIONS
    assert first.settings["estimate_page_factor"] == 1.5
    assert cache_path(tmp_path, "hwp5", item.input_sha256).is_file()
    assert not cache_path(tmp_path, "pdf", item.input_sha256).exists()


def test_direct_budget_guard_uses_ceiled_one_point_five_estimate(
    tmp_path: Path,
) -> None:
    item = _input(tmp_path, page_count=3)
    calls = 0

    def forbidden_client() -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("budget failure constructed an HTTP client")

    # Three pages would cost $0.033 including VAT, but ceil(3 * 1.5)=5 costs $0.055.
    with pytest.raises(BudgetExceededError):
        _adapter(tmp_path, forbidden_client, budget=0.04).parse(item, root=tmp_path)

    assert calls == 0
    assert ESTIMATE_PAGE_FACTOR_DIRECT == 1.5


def test_direct_usage_pages_at_limit_warns_and_is_recorded(
    tmp_path: Path,
) -> None:
    item = _input(tmp_path, page_count=80)
    err = io.StringIO()

    class FakeClient:
        last_attempts: tuple[dict[str, object], ...] = ()

        def parse(self, path: Path) -> tuple[dict[str, object], float]:
            return _response(pages=100), 2.0

    ledger = CostLedger(
        tmp_path / "data/manifest/costs.jsonl",
        clock=lambda: datetime(2026, 9, 27, tzinfo=UTC),
    )
    adapter = UpstageDirectAdapter(
        tmp_path,
        client_factory=FakeClient,
        ledger=ledger,
        guard=BudgetGuard(ledger, 45.0),
        run_id="direct-run",
        err=err,
    )

    summary = run(tmp_path, adapter, [item], out=io.StringIO())

    output = json.loads(
        (
            tmp_path / f"data/interim/parsed/upstage-direct/{item.input_sha256}.json"
        ).read_text(encoding="utf-8")
    )
    record = json.loads(
        (tmp_path / "data/manifest/parse_runs.jsonl").read_text(encoding="utf-8")
    )
    ledger_record = json.loads(
        (tmp_path / "data/manifest/costs.jsonl").read_text(encoding="utf-8")
    )
    assert summary.total_page_count == 100
    assert output["page_count"] == 100
    assert output["settings"]["possibly_truncated"] is True
    assert record["possibly_truncated"] is True
    assert ledger_record["quantity"] == 100
    assert item.input_sha256[:12] in err.getvalue()
    assert "100" in err.getvalue()


def test_direct_plan_uses_factor_per_uncached_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kodocs.config import Settings

    first = _input(tmp_path, name="첫째.hwp", page_count=1)
    second = _input(tmp_path, name="둘째.hwp", page_count=3)
    monkeypatch.setattr(
        "kodocs.parsing.upstage_adapter.load_settings",
        lambda: Settings(api_keys={}, monthly_budget_usd=45.0),
    )

    result = plan(
        tmp_path,
        [first, second],
        estimate_page_factor=ESTIMATE_PAGE_FACTOR_DIRECT,
    )

    assert result.page_count == 7  # ceil(1.5) + ceil(4.5)
    assert result.estimated_usd == pytest.approx(0.077 * 1.05)  # 청구 여유 5%


def test_direct_cli_forwards_selection_limits_and_dry_run_needs_no_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    item = _input(tmp_path, kind="hwpx", name="직접.hwpx", page_count=4)
    selected_with: list[tuple[int, int]] = []
    planned_with: list[float] = []

    def fake_load(root: Path, *, max_pages: int, hwp5_count: int) -> list[ParseInput]:
        selected_with.append((max_pages, hwp5_count))
        return [item]

    def fake_plan(
        root: Path,
        inputs: list[ParseInput],
        *,
        estimate_page_factor: float,
    ) -> DryRunPlan:
        planned_with.append(estimate_page_factor)
        return DryRunPlan(1, 6, 0.066, 0.0, 45.0, 44.934)

    monkeypatch.setattr(run_module, "load_direct_inputs", fake_load)
    monkeypatch.setattr(run_module, "plan", fake_plan)
    monkeypatch.setattr(
        run_module,
        "load_settings",
        lambda: pytest.fail("direct dry-run loaded an API key"),
    )

    exit_code = run_module.main(
        [
            "--root",
            str(tmp_path),
            "--parser",
            "upstage-direct",
            "--dry-run",
            "--max-pages",
            "123",
            "--hwp5-count",
            "7",
        ]
    )

    assert exit_code == 0
    assert selected_with == [(123, 7)]
    assert planned_with == [1.5]
    output = capsys.readouterr().out
    assert "직접 입력 선정 1건 / HWPX 1건 / HWP5 0건" in output
    assert "캐시 없음 1건 / 6쪽 / 예상 $0.07" in output
