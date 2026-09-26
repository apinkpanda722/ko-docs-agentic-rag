"""Tests for the billed Upstage Document Parse adapter and CLI guardrails."""

import hashlib
import io
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from kodocs.config import Settings
from kodocs.costs import BudgetExceededError, BudgetGuard, CostLedger
from kodocs.parsing import run as run_module
from kodocs.parsing.run import ParseInput, run
from kodocs.parsing.schema import Block, ParseError, content_stats, settings_hash
from kodocs.parsing.tables import cells_to_grid
from kodocs.parsing.upstage_adapter import (
    MAX_FILE_BYTES,
    MAX_PAGES_PER_REQUEST,
    PARSER_NAME,
    REQUEST_OPTIONS,
    SERVICE,
    DryRunPlan,
    UpstageAdapter,
    UpstageClient,
    cache_path,
    plan,
    response_to_parsed,
)

SECRET = "up_test_secret_value"


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _input(
    root: Path,
    name: str = "input.pdf",
    *,
    page_count: int | None = 2,
) -> ParseInput:
    path = root / "data/raw/pdf" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"fake PDF {name}".encode())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return ParseInput(
        target_key=name,
        input_path=path,
        input_sha256=digest,
        input_kind="pdf",
        page_count=page_count,
    )


def _response(
    *,
    pages: int = 2,
    enhanced: object = 0,
    elements: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "apiVersion": "1.2",
        "model": "document-parse-260630",
        "elements": elements
        if elements is not None
        else [
            {
                "id": 1,
                "category": "paragraph",
                "page": 1,
                "content": {
                    "text": "한글 본문",
                    "html": "<p>한글 본문</p>",
                    "markdown": "한글 본문",
                },
            }
        ],
        "content": {"text": "한글 본문"},
        "usage": {"pages": pages, "standard": pages, "enhanced": enhanced},
    }


def _ledger(root: Path) -> CostLedger:
    return CostLedger(
        root / "data/manifest/costs.jsonl",
        clock=lambda: datetime(2026, 9, 26, tzinfo=UTC),
    )


def _adapter(
    root: Path,
    client_factory: Callable[[], object],
    *,
    budget: float = 45.0,
) -> UpstageAdapter:
    ledger = _ledger(root)
    return UpstageAdapter(
        root,
        client_factory=client_factory,  # type: ignore[arg-type]
        ledger=ledger,
        guard=BudgetGuard(ledger, budget),
        run_id="run-upstage",
    )


def test_constants_cache_path_and_fixed_settings(tmp_path: Path) -> None:
    adapter = _adapter(
        tmp_path,
        lambda: SimpleNamespace(parse=lambda path: (_response(), 0.1)),
    )

    assert PARSER_NAME == "upstage"
    assert SERVICE == "upstage.document-parse.standard"
    assert MAX_PAGES_PER_REQUEST == 100
    assert MAX_FILE_BYTES == 50 * 1024 * 1024
    assert REQUEST_OPTIONS == {
        "model": "document-parse",
        "mode": "standard",
        "ocr": "auto",
        "output_formats": '["text", "html", "markdown"]',
        "coordinates": "true",
        "chart_recognition": "true",
        "merge_multipage_tables": "false",
    }
    assert adapter.settings() == {
        "input": "pdf",
        "request_options": REQUEST_OPTIONS,
    }
    assert cache_path(tmp_path, "hwp5", "a" * 64) == (
        tmp_path / f"data/raw/upstage/hwp5/{'a' * 64}.json"
    )


def test_client_posts_multipart_with_standard_mode_and_all_fixed_fields(
    tmp_path: Path,
) -> None:
    item = _input(tmp_path)
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        body = request.content
        assert request.method == "POST"
        assert request.url == "https://api.upstage.ai/v1/document-digitization"
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        assert b'name="document"; filename="input.pdf"' in body
        assert item.input_path.read_bytes() in body
        for name, value in REQUEST_OPTIONS.items():
            assert f'name="{name}"'.encode() in body
            assert f"\r\n\r\n{value}\r\n".encode() in body
        return httpx.Response(200, json=_response(), request=request)

    client = UpstageClient(
        SECRET,
        transport=httpx.MockTransport(handler),
        min_interval_s=0,
    )

    response, elapsed_s = client.parse(item.input_path)

    assert response["model"] == "document-parse-260630"
    assert elapsed_s >= 0
    assert len(seen_requests) == 1


@pytest.mark.parametrize(
    ("retry_header", "expected_sleep"),
    [("1005", 5.0), (None, 2.0)],
    ids=["absolute-retry-time", "exponential-backoff"],
)
def test_client_retries_429_with_header_or_backoff(
    tmp_path: Path,
    retry_header: str | None,
    expected_sleep: float,
) -> None:
    item = _input(tmp_path)
    clock = FakeClock()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            headers = (
                {"X-Upstage-RateLimit-Retry-After-Requests": retry_header}
                if retry_header is not None
                else {}
            )
            return httpx.Response(429, headers=headers, text="slow", request=request)
        return httpx.Response(200, json=_response(), request=request)

    client = UpstageClient(
        SECRET,
        transport=httpx.MockTransport(handler),
        min_interval_s=1,
        backoff_s=2,
        clock=clock,
        sleep=clock.sleep,
    )

    client.parse(item.input_path)

    assert calls == 2
    assert clock.sleeps == [expected_sleep]


def test_client_retries_server_and_network_errors_then_stops_boundedly(
    tmp_path: Path,
) -> None:
    item = _input(tmp_path)
    clock = FakeClock()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(503, text="unavailable", request=request)

    client = UpstageClient(
        SECRET,
        transport=httpx.MockTransport(handler),
        min_interval_s=0,
        max_retries=2,
        backoff_s=2,
        clock=clock,
        sleep=clock.sleep,
    )

    with pytest.raises(ParseError, match=r"503.*unavailable") as caught:
        client.parse(item.input_path)

    assert calls == 3
    assert clock.sleeps == [2, 4]
    assert SECRET not in str(caught.value)


def test_client_does_not_retry_other_4xx_and_redacts_secret(tmp_path: Path) -> None:
    item = _input(tmp_path)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            400,
            text=f"bad request Authorization: Bearer {SECRET}",
            request=request,
        )

    client = UpstageClient(
        SECRET,
        transport=httpx.MockTransport(handler),
        min_interval_s=0,
    )

    with pytest.raises(ParseError, match="400") as caught:
        client.parse(item.input_path)

    assert calls == 1
    assert SECRET not in str(caught.value)
    assert "Authorization" not in str(caught.value)


@pytest.mark.parametrize(
    "response_factory",
    [
        lambda request: httpx.Response(200, text="not-json", request=request),
        lambda request: httpx.Response(
            200,
            json={"model": "document-parse-260630", "usage": {}},
            request=request,
        ),
    ],
    ids=["non-json", "missing-usage-pages"],
)
def test_client_rejects_unbillable_success_responses(
    tmp_path: Path,
    response_factory: Callable[[httpx.Request], httpx.Response],
) -> None:
    item = _input(tmp_path)
    client = UpstageClient(
        SECRET,
        transport=httpx.MockTransport(response_factory),
        min_interval_s=0,
    )

    with pytest.raises(ParseError, match="usage.pages|JSON"):
        client.parse(item.input_path)


def test_cache_hit_precedes_preflight_and_never_calls_or_records(
    tmp_path: Path,
) -> None:
    item = _input(tmp_path, page_count=None)
    cache = cache_path(tmp_path, item.input_kind, item.input_sha256)
    cache.parent.mkdir(parents=True)
    cache.write_text(
        json.dumps(
            {
                "request_options": dict(REQUEST_OPTIONS),
                "fetched_at": "2026-09-26T00:00:00+09:00",
                "elapsed_s": 1.25,
                "response": _response(),
            }
        ),
        encoding="utf-8",
    )
    calls = 0

    def forbidden_client() -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("cache hit constructed an HTTP client")

    adapter = _adapter(tmp_path, forbidden_client, budget=0)

    parsed = adapter.parse(item, root=tmp_path)

    assert calls == 0
    assert parsed.elapsed_s == 1.25
    assert not (tmp_path / "data/manifest/costs.jsonl").exists()


def test_cache_option_mismatch_never_calls(tmp_path: Path) -> None:
    item = _input(tmp_path)
    cache = cache_path(tmp_path, item.input_kind, item.input_sha256)
    cache.parent.mkdir(parents=True)
    cache.write_text(
        json.dumps(
            {
                "request_options": {**REQUEST_OPTIONS, "mode": "auto"},
                "fetched_at": "2026-09-26T00:00:00+09:00",
                "elapsed_s": 1.25,
                "response": _response(),
            }
        ),
        encoding="utf-8",
    )
    calls = 0

    def forbidden_client() -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("mismatched cache constructed an HTTP client")

    with pytest.raises(ParseError, match="캐시 옵션 불일치"):
        _adapter(tmp_path, forbidden_client).parse(item, root=tmp_path)

    assert calls == 0


@pytest.mark.parametrize("page_count", [None, 101])
def test_preflight_rejects_missing_or_excess_pages_without_call(
    tmp_path: Path,
    page_count: int | None,
) -> None:
    item = _input(tmp_path, page_count=page_count)
    calls = 0

    def forbidden_client() -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("invalid input constructed an HTTP client")

    with pytest.raises(ParseError, match="쪽수"):
        _adapter(tmp_path, forbidden_client).parse(item, root=tmp_path)

    assert calls == 0


def test_preflight_rejects_oversized_file_without_call(tmp_path: Path) -> None:
    item = _input(tmp_path, page_count=1)
    with item.input_path.open("r+b") as oversized:
        oversized.truncate(MAX_FILE_BYTES + 1)
    calls = 0

    def forbidden_client() -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("oversized input constructed an HTTP client")

    with pytest.raises(ParseError, match="파일 크기"):
        _adapter(tmp_path, forbidden_client).parse(item, root=tmp_path)

    assert calls == 0


def test_budget_exceeded_stops_shared_runner_before_client_and_next_input(
    tmp_path: Path,
) -> None:
    first = _input(tmp_path, "first.pdf", page_count=1)
    second = _input(tmp_path, "second.pdf", page_count=1)
    calls = 0

    def forbidden_client() -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("budget failure constructed an HTTP client")

    adapter = _adapter(tmp_path, forbidden_client, budget=0)

    with pytest.raises(BudgetExceededError):
        run(tmp_path, adapter, [first, second], out=io.StringIO())

    assert calls == 0
    assert not (tmp_path / "data/manifest/parse_runs.jsonl").exists()


def test_ledger_is_recorded_before_cache_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kodocs.parsing import upstage_adapter

    item = _input(tmp_path)

    class FakeClient:
        def parse(self, path: Path) -> tuple[dict[str, object], float]:
            return _response(), 0.4

    def fail_cache_write(path: Path, payload: Mapping[str, object]) -> None:
        ledger_path = tmp_path / "data/manifest/costs.jsonl"
        assert ledger_path.is_file()
        raise OSError("disk full")

    monkeypatch.setattr(upstage_adapter, "_write_cache_atomic", fail_cache_write)

    with pytest.raises(OSError, match="disk full"):
        _adapter(tmp_path, FakeClient).parse(item, root=tmp_path)

    records = [
        json.loads(line)
        for line in (tmp_path / "data/manifest/costs.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(records) == 1
    assert records[0]["quantity"] == 2
    assert records[0]["ref"] == item.input_sha256
    assert records[0]["run_id"] == "run-upstage"


def test_success_records_once_then_cache_prevents_rebilling_and_hides_key(
    tmp_path: Path,
) -> None:
    item = _input(tmp_path)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_response(), request=request)

    def client_factory() -> UpstageClient:
        return UpstageClient(
            SECRET,
            transport=httpx.MockTransport(handler),
            min_interval_s=0,
        )

    adapter = _adapter(tmp_path, client_factory)
    first = adapter.parse(item, root=tmp_path)
    second = adapter.parse(item, root=tmp_path)

    assert calls == 1
    assert first == second
    ledger_text = (tmp_path / "data/manifest/costs.jsonl").read_text(encoding="utf-8")
    cache_text = cache_path(tmp_path, item.input_kind, item.input_sha256).read_text(
        encoding="utf-8"
    )
    assert len(ledger_text.splitlines()) == 1
    assert SECRET not in ledger_text
    assert SECRET not in cache_text
    assert "Authorization" not in cache_text


def test_response_maps_blocks_and_keeps_response_metadata_out_of_settings_hash(
    tmp_path: Path,
) -> None:
    item = _input(tmp_path)
    settings = {"input": "pdf", "request_options": REQUEST_OPTIONS}
    response = _response(
        elements=[
            {"id": 8, "category": "mystery", "page": 2, "content": {"text": "기타"}},
            {"id": 1, "category": "heading1", "page": 1, "content": {"text": "제목"}},
            {"id": 2, "category": "title", "page": 1, "content": {"text": "부제"}},
            {"id": 3, "category": "paragraph", "page": 1, "content": {"text": "본문"}},
            {"id": 4, "category": "list", "page": 1, "content": {"text": "항목"}},
            {"id": 5, "category": "header", "page": 2, "content": {"text": "머리"}},
            {"id": 6, "category": "footer", "page": 2, "content": {"text": "꼬리"}},
            {"id": 7, "category": "caption", "page": 2, "content": {"text": "캡션"}},
        ]
    )

    parsed = response_to_parsed(
        response,
        item=item,
        root=tmp_path,
        settings=settings,
        elapsed_s=0.75,
    )

    assert parsed.blocks == (
        Block(1, "heading", "제목"),
        Block(1, "heading", "부제"),
        Block(1, "text", "본문"),
        Block(1, "list_item", "항목"),
        Block(2, "header", "머리"),
        Block(2, "footer", "꼬리"),
        Block(2, "caption", "캡션"),
        Block(2, "other", "기타"),
    )
    assert parsed.parser_version == "document-parse-260630"
    assert parsed.settings["apiVersion"] == "1.2"
    assert parsed.settings_hash == settings_hash(settings)
    assert parsed.elapsed_s == 0.75


def test_overlapping_table_is_invalid_and_next_table_is_normal(
    tmp_path: Path,
) -> None:
    item = _input(tmp_path)
    overlapping_html = """
        <table>
          <tr><td rowspan="2">병합</td><td>위</td></tr>
          <tr><td colspan="2">겹치는 행</td></tr>
        </table>
    """
    normal_html = """
        <table>
          <tr><th>열1</th><th>열2</th></tr>
          <tr><td>값1<br>계속</td><td>값2</td></tr>
        </table>
    """
    response = _response(
        elements=[
            {
                "id": 1,
                "category": "table",
                "page": 1,
                "content": {"text": "겹친 표", "html": overlapping_html},
            },
            {
                "id": 2,
                "category": "table",
                "page": 2,
                "content": {"text": "정상 표", "html": normal_html},
            },
        ]
    )

    parsed = response_to_parsed(
        response,
        item=item,
        root=tmp_path,
        settings={"input": "pdf", "request_options": REQUEST_OPTIONS},
        elapsed_s=0.1,
    )

    assert len(parsed.tables) == 2
    assert parsed.tables[0].invalid_reason is not None
    assert parsed.tables[1].invalid_reason is None
    assert cells_to_grid(parsed.tables[1].cells) == [
        ["열1", "열2"],
        ["값1 계속", "값2"],
    ]
    assert content_stats(parsed)["invalid_tables"] == 1


def test_broken_html_table_has_no_cells_and_uses_element_text(tmp_path: Path) -> None:
    item = _input(tmp_path)
    response = _response(
        elements=[
            {
                "id": 1,
                "category": "table",
                "page": 1,
                "content": {
                    "text": "표 대체 텍스트",
                    "html": "<table><tr><td>닫히지 않음",
                },
            }
        ]
    )

    parsed = response_to_parsed(
        response,
        item=item,
        root=tmp_path,
        settings={"input": "pdf", "request_options": REQUEST_OPTIONS},
        elapsed_s=0.1,
    )

    assert parsed.tables[0].invalid_reason is not None
    assert parsed.tables[0].cells == ()
    assert parsed.blocks[0] == Block(1, "table", "표 대체 텍스트")


def test_enhanced_usage_warns_and_is_saved_in_settings_and_run_record(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    item = _input(tmp_path)

    class FakeClient:
        def parse(self, path: Path) -> tuple[dict[str, object], float]:
            return _response(enhanced=1), 0.2

    summary = run(
        tmp_path,
        _adapter(tmp_path, FakeClient),
        [item],
        out=io.StringIO(),
    )

    output = json.loads(
        (tmp_path / f"data/interim/parsed/upstage/{item.input_sha256}.json").read_text(
            encoding="utf-8"
        )
    )
    record = json.loads(
        (tmp_path / "data/manifest/parse_runs.jsonl").read_text(encoding="utf-8")
    )
    assert summary.ok_count == 1
    assert output["settings"]["usage_enhanced"] == 1
    assert record["usage_enhanced"] == 1
    assert "enhanced" in capsys.readouterr().err


def test_plan_excludes_every_existing_cache_and_totals_tax_inclusive_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _input(tmp_path, "cached.pdf", page_count=5)
    second = _input(tmp_path, "new.pdf", page_count=10)
    existing = cache_path(tmp_path, first.input_kind, first.input_sha256)
    existing.parent.mkdir(parents=True)
    existing.write_text("even a broken cache prevents a paid call", encoding="utf-8")
    monkeypatch.setattr(
        "kodocs.parsing.upstage_adapter.load_settings",
        lambda: Settings(api_keys={}, monthly_budget_usd=45.0),
    )

    result = plan(tmp_path, [first, second])

    assert result == DryRunPlan(
        uncached_count=1,
        page_count=10,
        estimated_usd=pytest.approx(0.11),
        month_total_usd=0.0,
        monthly_budget_usd=45.0,
        remaining_after_usd=pytest.approx(44.89),
    )


def test_cli_dry_run_needs_no_key_and_over_budget_returns_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    item = _input(tmp_path)
    monkeypatch.setattr(run_module, "load_inputs", lambda root, limit=None: [item])
    monkeypatch.setattr(
        run_module,
        "plan",
        lambda root, inputs: DryRunPlan(1, 2, 1.0, 44.5, 45.0, -0.5),
        raising=False,
    )
    monkeypatch.setattr(
        run_module,
        "_build_upstage_adapter",
        lambda *args, **kwargs: pytest.fail("dry-run built a network adapter"),
        raising=False,
    )

    exit_code = run_module.main(
        ["--root", str(tmp_path), "--parser", "upstage", "--dry-run"]
    )

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "캐시 없음 1건 / 2쪽 / 예상 $1.00" in output
    assert "예산 초과" in output


def test_cli_confirmation_n_makes_zero_calls_and_does_not_load_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _input(tmp_path)
    prompts: list[str] = []
    monkeypatch.setattr(run_module, "load_inputs", lambda root, limit=None: [item])
    monkeypatch.setattr(
        run_module,
        "plan",
        lambda root, inputs: DryRunPlan(1, 2, 0.02, 0.0, 45.0, 44.98),
        raising=False,
    )
    monkeypatch.setattr(
        run_module,
        "load_settings",
        lambda: pytest.fail("declined run loaded the API key"),
        raising=False,
    )

    exit_code = run_module.main(
        ["--root", str(tmp_path), "--parser", "upstage"],
        input_fn=lambda prompt: prompts.append(prompt) or "n",
    )

    assert exit_code == 0
    assert prompts == ["계속하려면 y 입력: "]


def test_cli_missing_key_after_confirmation_returns_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    item = _input(tmp_path)
    monkeypatch.setattr(run_module, "load_inputs", lambda root, limit=None: [item])
    monkeypatch.setattr(
        run_module,
        "plan",
        lambda root, inputs: DryRunPlan(1, 2, 0.02, 0.0, 45.0, 44.98),
        raising=False,
    )
    monkeypatch.setattr(
        run_module,
        "load_settings",
        lambda: Settings(api_keys={}, monthly_budget_usd=45.0),
        raising=False,
    )

    exit_code = run_module.main(
        ["--root", str(tmp_path), "--parser", "upstage", "--yes"]
    )

    assert exit_code == 2
    assert "UPSTAGE_API_KEY" in capsys.readouterr().err
