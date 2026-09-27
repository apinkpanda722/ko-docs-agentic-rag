"""Billed Upstage Document Parse adapter with immutable raw-response caching."""

from __future__ import annotations

import json
import math
import re
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

import httpx

from kodocs.config import load_settings
from kodocs.costs import (
    DEFAULT_LEDGER_PATH,
    PRICES,
    BudgetGuard,
    CostLedger,
    budget_estimate_usd,
)
from kodocs.ingest.manifest import now_iso, repo_relative
from kodocs.parsing.schema import (
    SCHEMA_VERSION,
    BillingUncertainError,
    Block,
    BlockKind,
    FatalParseError,
    ParsedDocument,
    ParsedTable,
    ParseError,
    TableCell,
    build_table,
    settings_hash,
)
from kodocs.parsing.tables import cells_to_grid, join_cell_text, place_html_rows

if TYPE_CHECKING:
    from kodocs.parsing.run import ParseInput

PARSER_NAME = "upstage"
PARSER_NAME_DIRECT = "upstage-direct"
SERVICE = "upstage.document-parse.standard"
ESTIMATE_PAGE_FACTOR_DIRECT = 1.5
# Bump when blocks/tables built from the same cached response change, so stored
# outputs are rebuilt from the cache (no API call). 2: nested tables flattened.
ADAPTER_REVISION = 2
REQUEST_OPTIONS: Mapping[str, str] = {
    "model": "document-parse",
    "mode": "standard",
    "ocr": "auto",
    "output_formats": '["text", "html", "markdown"]',
    "merge_multipage_tables": "false",
}
# coordinates·chart_recognition are deliberately not sent. The API reference
# (https://console.upstage.ai/docs/for-agents/api-reference) documents both as
# default true, but the pricing table
# (https://console.upstage.ai/docs/models/compare/document-intelligence) lists
# per-page prices by mode only and says nothing about these options' effect on
# price. Leaving them at the server default avoids asserting an unverified value.
# (checked 2026-09-26)
MAX_PAGES_PER_REQUEST = 100
MAX_FILE_BYTES = 50 * 1024 * 1024

_ENDPOINT = "https://api.upstage.ai/v1/document-digitization"
_RETRY_AFTER_HEADER = "X-Upstage-RateLimit-Retry-After-Requests"
# Statuses that would repeat for every document: stop the run, don't retry.
_FATAL_STATUSES = frozenset({401, 402, 403})
# 4xx bodies that look like an account-level usage/credit/spending cap.
_QUOTA_BODY_RE = re.compile(
    r"quota|credit|billing|payment|insufficient|spending|usage\s*limit"
    r"|limit\s*(?:has\s*been\s*)?(?:exceeded|reached)",
    re.IGNORECASE,
)
_BILLING_UNCERTAIN_MESSAGE = "과금 여부 불확실: 업스테이지 콘솔 Usage 확인 후 재실행"
_CATEGORY_KINDS: Mapping[str, BlockKind] = {
    "paragraph": "text",
    "list": "list_item",
    "header": "header",
    "footer": "footer",
    "caption": "caption",
    "table": "table",
}


def cache_path(root: Path, input_kind: str, input_sha256: str) -> Path:
    """Return the content-addressed raw Upstage response cache path."""
    return root / "data/raw/upstage" / input_kind / f"{input_sha256}.json"


class UpstageClient:
    """Sequential synchronous client with Upstage-specific bounded retries."""

    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        min_interval_s: float = 1.0,
        max_retries: int = 5,
        backoff_s: float = 2.0,
        # Post-send timeouts are not retried (billing uncertain), so allow slow
        # sync parses of long documents (target max 85 pages). No cost impact.
        timeout_s: float = 600.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if min_interval_s < 0:
            raise ValueError("min_interval_s must be non-negative")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if backoff_s < 0:
            raise ValueError("backoff_s must be non-negative")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if not api_key:
            raise ValueError("api_key must not be empty")

        self._api_key = api_key
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
            transport=transport,
        )
        self._min_interval_s = min_interval_s
        self._max_retries = max_retries
        self._backoff_s = backoff_s
        self._clock = clock
        self._sleep = sleep
        self._last_request_started: float | None = None
        # HTTP attempts of the latest parse(): [{"status", "error", "ts"}].
        self.last_attempts: list[dict[str, object]] = []

    def parse(self, path: Path) -> tuple[dict[str, object], float]:
        """Post one document and return a validated, secret-free JSON response.

        Every HTTP attempt is kept in ``last_attempts``; a raised ``ParseError``
        carries the same list as ``attempts`` for the run record.
        """
        self.last_attempts = []
        try:
            return self._parse(path)
        except ParseError as exc:
            exc.attempts = list(self.last_attempts)  # type: ignore[attr-defined]
            raise

    def _parse(self, path: Path) -> tuple[dict[str, object], float]:
        started = self._clock()
        response: httpx.Response | None = None

        for attempt in range(self._max_retries + 1):
            self._wait_for_request_slot()
            try:
                with path.open("rb") as document:
                    response = self._client.post(
                        _ENDPOINT,
                        data=dict(REQUEST_OPTIONS),
                        files={"document": (path.name, document)},
                    )
            # The connection never opened, so the request was not sent and
            # cannot have been billed: safe to retry.
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                self._record_attempt(None, exc)
                if attempt < self._max_retries:
                    self._sleep(self._backoff_s * 2**attempt)
                    continue
                raise ParseError(
                    "Upstage request failed after "
                    f"{attempt + 1} attempts: {type(exc).__name__}"
                ) from exc
            # The request was (at least partly) sent. The server may have parsed
            # and billed it before the response was lost; resending could bill
            # twice, and the ledger only records successes. Do not retry.
            except httpx.TransportError as exc:
                self._record_attempt(None, exc)
                raise BillingUncertainError(
                    f"{_BILLING_UNCERTAIN_MESSAGE} ({type(exc).__name__})"
                ) from exc

            status = response.status_code
            self._record_attempt(status, None)
            if status in _FATAL_STATUSES:
                raise FatalParseError(
                    f"Upstage HTTP {status}: 인증·결제·권한 문제로 실행을 멈춥니다: "
                    f"{self._safe_body(response)}"
                )
            if status == 429:
                if attempt < self._max_retries:
                    self._sleep(self._retry_delay(response, attempt))
                    continue
                raise FatalParseError(
                    f"Upstage HTTP 429: 재시도 {self._max_retries}회 후에도 속도 제한·한도가 "
                    f"풀리지 않아 실행을 멈춥니다: {self._safe_body(response)}"
                )
            if 500 <= status < 600 and attempt < self._max_retries:
                self._sleep(self._backoff_s * 2**attempt)
                continue
            if status >= 400:
                body = self._safe_body(response)
                if 400 <= status < 500 and _QUOTA_BODY_RE.search(response.text):
                    raise FatalParseError(
                        f"Upstage HTTP {status}: 사용 한도 초과로 보여 실행을 멈춥니다: "
                        f"{body}"
                    )
                raise ParseError(f"Upstage HTTP {status}: {body}")
            break
        else:  # pragma: no cover - every loop path returns, breaks, or raises
            raise AssertionError("retry loop exited unexpectedly")

        if response is None:  # pragma: no cover - defensive type narrowing
            raise AssertionError("request loop produced no response")
        try:
            raw_payload = response.json()
        except ValueError as exc:
            raise ParseError(
                f"Upstage HTTP 200 response is not JSON: {self._safe_body(response)}"
            ) from exc
        payload = _secret_free_payload(raw_payload, self._api_key)
        validated = _validated_response(payload, body=self._safe_body(response))
        return validated, self._clock() - started

    def _record_attempt(self, status: int | None, exc: Exception | None) -> None:
        self.last_attempts.append(
            {
                "status": status,
                "error": type(exc).__name__ if exc is not None else None,
                "ts": now_iso(),
            }
        )

    def _wait_for_request_slot(self) -> None:
        now = self._clock()
        if self._last_request_started is not None:
            remaining = self._min_interval_s - (now - self._last_request_started)
            if remaining > 0:
                self._sleep(remaining)
                now = self._clock()
        self._last_request_started = now

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        raw_retry_at = response.headers.get(_RETRY_AFTER_HEADER)
        if raw_retry_at is not None:
            try:
                return max(0.0, float(raw_retry_at) - self._clock())
            except ValueError:
                pass
        return self._backoff_s * 2**attempt

    def _safe_body(self, response: httpx.Response) -> str:
        text = response.text.replace(self._api_key, "[REDACTED]")
        text = re.sub(
            "authorization",
            "[REDACTED_HEADER]",
            text,
            flags=re.IGNORECASE,
        )
        return _first_line(text)[:200]


class UpstageAdapter:
    """Cache and account for each paid request before schema normalization."""

    name = PARSER_NAME

    def __init__(
        self,
        root: Path,
        *,
        client_factory: Callable[[], UpstageClient],
        ledger: CostLedger,
        guard: BudgetGuard,
        run_id: str,
        err: TextIO | None = None,
    ) -> None:
        self._root = root
        self._client_factory = client_factory
        self._client: UpstageClient | None = None
        self._ledger = ledger
        self._guard = guard
        self._run_id = run_id
        self._err = err

    def settings(self) -> Mapping[str, object]:
        """Return only output-determining input and request settings."""
        return {
            "input": "pdf",
            "request_options": dict(REQUEST_OPTIONS),
            "adapter_revision": ADAPTER_REVISION,
        }

    def parse(self, item: ParseInput, *, root: Path) -> ParsedDocument:
        """Use an existing cache or make one guarded, billed API call."""
        destination = cache_path(
            self._root,
            item.input_kind,
            item.input_sha256,
        )
        current_settings = dict(self.settings())
        if destination.exists():
            cached = _read_cache(destination)
            if cached.get("request_options") != dict(REQUEST_OPTIONS):
                raise ParseError(
                    "캐시 옵션 불일치: 현재 REQUEST_OPTIONS와 다르므로 "
                    "재과금 호출을 중단합니다."
                )
            response = _validated_response(cached.get("response"), body="cached JSON")
            elapsed_s = _non_negative_number(cached.get("elapsed_s"), "elapsed_s")
            self._warn_response(response, item)
            return self._to_parsed(
                response,
                item=item,
                root=root,
                settings=current_settings,
                elapsed_s=elapsed_s,
                attempts=[],  # cache hit: no HTTP request in this run
            )

        estimated_pages = self._estimated_pages(item)
        if item.input_path.stat().st_size > MAX_FILE_BYTES:
            raise ParseError(
                "업스테이지 호출 전 파일 크기 검사가 실패했습니다: "
                f"최대 {MAX_FILE_BYTES} bytes"
            )
        # Budget check only: includes the provider billing margin (see costs.PRICES).
        self._guard.check(budget_estimate_usd(SERVICE, estimated_pages))

        if self._client is None:
            self._client = self._client_factory()
        response, elapsed_s = self._client.parse(item.input_path)
        usage = response["usage"]
        assert isinstance(usage, Mapping)  # established by _validated_response
        pages = usage["pages"]
        assert isinstance(pages, int)  # established by _validated_response
        model = response["model"]
        assert isinstance(model, str)  # established by _validated_response

        # Billing already happened. Record it before attempting the fallible cache write.
        self._ledger.record(
            service=SERVICE,
            model=model,
            quantity=pages,
            ref=item.input_sha256,
            run_id=self._run_id,
        )
        cache_payload = {
            "request_options": dict(REQUEST_OPTIONS),
            "fetched_at": now_iso(),
            "elapsed_s": elapsed_s,
            "response": response,
        }
        _write_cache_atomic(destination, cache_payload)
        self._warn_response(response, item)
        return self._to_parsed(
            response,
            item=item,
            root=root,
            settings=current_settings,
            elapsed_s=elapsed_s,
            attempts=self._client.last_attempts,
        )

    def _estimated_pages(self, item: ParseInput) -> int:
        page_count = item.page_count
        if page_count is None or page_count > MAX_PAGES_PER_REQUEST:
            raise ParseError(
                "업스테이지 호출 전 쪽수 검사가 실패했습니다: "
                f"page_count={page_count!r}, 최대 {MAX_PAGES_PER_REQUEST}쪽"
            )
        return page_count

    def _to_parsed(
        self,
        response: Mapping[str, object],
        *,
        item: ParseInput,
        root: Path,
        settings: Mapping[str, object],
        elapsed_s: float,
        attempts: Sequence[Mapping[str, object]],
    ) -> ParsedDocument:
        return response_to_parsed(
            response,
            item=item,
            root=root,
            settings=settings,
            elapsed_s=elapsed_s,
            parser_name=self.name,
            extra_settings={"attempts": list(attempts)},
        )

    def _warn_response(self, response: Mapping[str, object], item: ParseInput) -> None:
        del item
        self._warn_enhanced(response)

    def _warn_enhanced(self, response: Mapping[str, object]) -> None:
        usage = response.get("usage")
        enhanced = usage.get("enhanced") if isinstance(usage, Mapping) else None
        if _has_enhanced_usage(enhanced):
            print(
                "경고: mode=standard 요청에서 usage.enhanced가 비어 있지 않습니다: "
                f"{enhanced!r}",
                file=self._err or sys.stderr,
            )


class UpstageDirectAdapter(UpstageAdapter):
    """Upstage experiment that changes only the input from PDF to HWP/HWPX."""

    name = PARSER_NAME_DIRECT

    def settings(self) -> Mapping[str, object]:
        return {
            "input": "original",
            "request_options": dict(REQUEST_OPTIONS),
            "estimate_page_factor": ESTIMATE_PAGE_FACTOR_DIRECT,
            "adapter_revision": ADAPTER_REVISION,
        }

    def _estimated_pages(self, item: ParseInput) -> int:
        if item.page_count is None:
            raise ParseError(
                "업스테이지 직접 입력 호출 전 쪽수 검사가 실패했습니다: page_count=None"
            )
        return math.ceil(item.page_count * ESTIMATE_PAGE_FACTOR_DIRECT)

    def _to_parsed(
        self,
        response: Mapping[str, object],
        *,
        item: ParseInput,
        root: Path,
        settings: Mapping[str, object],
        elapsed_s: float,
        attempts: Sequence[Mapping[str, object]],
    ) -> ParsedDocument:
        usage = response["usage"]
        assert isinstance(usage, Mapping)
        pages = usage["pages"]
        assert isinstance(pages, int)
        return response_to_parsed(
            response,
            item=item,
            root=root,
            settings=settings,
            elapsed_s=elapsed_s,
            parser_name=self.name,
            page_count=pages,
            extra_settings={
                "possibly_truncated": pages >= MAX_PAGES_PER_REQUEST,
                "attempts": list(attempts),
            },
        )

    def _warn_response(self, response: Mapping[str, object], item: ParseInput) -> None:
        super()._warn_response(response, item)
        usage = response["usage"]
        assert isinstance(usage, Mapping)
        pages = usage["pages"]
        assert isinstance(pages, int)
        if pages >= MAX_PAGES_PER_REQUEST:
            print(
                f"경고: {item.input_sha256[:12]} usage.pages={pages}; "
                "앞 100쪽만 처리됐을 수 있습니다(possibly_truncated).",
                file=self._err or sys.stderr,
            )


def response_to_parsed(
    resp: Mapping[object, object],
    *,
    item: ParseInput,
    root: Path,
    settings: Mapping[str, object],
    elapsed_s: float,
    parser_name: str = PARSER_NAME,
    page_count: int | None = None,
    extra_settings: Mapping[str, object] | None = None,
) -> ParsedDocument:
    """Normalize one validated Upstage response to the common parser schema."""
    response = _validated_response(resp, body="response mapping")
    elements = response.get("elements", [])
    if not isinstance(elements, list):
        raise ParseError("Upstage response elements must be a list")

    blocks: list[Block] = []
    tables: list[ParsedTable] = []
    for element in sorted(elements, key=_element_sort_key):
        if not isinstance(element, Mapping):
            raise ParseError("Upstage response element must be an object")
        category = str(element.get("category", "")).casefold()
        kind = _category_kind(category)
        page = _page_number(element.get("page"))
        content = element.get("content")
        content_mapping = content if isinstance(content, Mapping) else {}
        text = _text(content_mapping.get("text"))

        if kind == "table":
            table, text = _build_upstage_table(
                _text(content_mapping.get("html")),
                fallback_text=text,
                page=page,
                block_position=len(blocks),
            )
            tables.append(table)
        blocks.append(Block(page=page, kind=kind, text=text))

    stable_settings = dict(settings)
    document_settings = dict(stable_settings)
    if extra_settings is not None:
        document_settings.update(extra_settings)
    # The response key is "api" (e.g. "2.0"), checked against cached responses.
    document_settings["api"] = _text(response.get("api"))
    usage = response["usage"]
    assert isinstance(usage, Mapping)
    enhanced = usage.get("enhanced")
    if _has_enhanced_usage(enhanced):
        document_settings["usage_enhanced"] = enhanced

    return ParsedDocument(
        schema_version=SCHEMA_VERSION,
        parser=parser_name,
        parser_version=str(response["model"]),
        settings=document_settings,
        # Response metadata is recorded but does not change output-determining settings.
        settings_hash=settings_hash(stable_settings),
        input_path=repo_relative(item.input_path, root),
        input_sha256=item.input_sha256,
        input_kind=item.input_kind,  # type: ignore[arg-type]
        page_count=item.page_count if page_count is None else page_count,
        blocks=tuple(blocks),
        tables=tuple(tables),
        elapsed_s=elapsed_s,
    )


@dataclass(frozen=True)
class DryRunPlan:
    """One no-network estimate for the still-uncached paid requests."""

    uncached_count: int
    page_count: int
    estimated_usd: float
    month_total_usd: float
    monthly_budget_usd: float
    remaining_after_usd: float
    billing_margin: float = 1.0

    @property
    def exceeds_budget(self) -> bool:
        return self.remaining_after_usd < 0


def plan(
    root: Path,
    inputs: Sequence[ParseInput],
    *,
    estimate_page_factor: float = 1.0,
) -> DryRunPlan:
    """Estimate tax-inclusive cost using only target metadata, cache, and ledger."""
    if not math.isfinite(estimate_page_factor) or estimate_page_factor <= 0:
        raise ValueError("estimate_page_factor must be a positive finite number")
    uncached = [
        item
        for item in inputs
        if not cache_path(root, item.input_kind, item.input_sha256).exists()
    ]
    page_count = sum(
        math.ceil(item.page_count * estimate_page_factor)
        for item in uncached
        if item.page_count is not None and item.page_count > 0
    )
    estimated = budget_estimate_usd(SERVICE, page_count)
    settings = load_settings()
    ledger = CostLedger(root / DEFAULT_LEDGER_PATH)
    month_total = ledger.month_total_usd()
    return DryRunPlan(
        uncached_count=len(uncached),
        page_count=page_count,
        estimated_usd=estimated,
        month_total_usd=month_total,
        monthly_budget_usd=settings.monthly_budget_usd,
        remaining_after_usd=(settings.monthly_budget_usd - month_total - estimated),
        billing_margin=PRICES[SERVICE].billing_margin,
    )


def _validated_response(value: object, *, body: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ParseError(f"Upstage response JSON must be an object: {body[:200]}")
    response = dict(value)
    usage = response.get("usage")
    pages = usage.get("pages") if isinstance(usage, Mapping) else None
    if isinstance(pages, bool) or not isinstance(pages, int) or pages < 0:
        raise ParseError(
            f"Upstage HTTP 200 response has no valid usage.pages: {body[:200]}"
        )
    model = response.get("model")
    if not isinstance(model, str) or not model:
        raise ParseError(f"Upstage HTTP 200 response has no valid model: {body[:200]}")
    return response


def _secret_free_payload(value: object, secret: str) -> object:
    if isinstance(value, Mapping):
        safe: dict[str, object] = {}
        for key, nested in value.items():
            safe_key = str(key)
            if safe_key.casefold() == "authorization":
                continue
            safe[safe_key] = _secret_free_payload(nested, secret)
        return safe
    if isinstance(value, list):
        return [_secret_free_payload(item, secret) for item in value]
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    return value


def _read_cache(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ParseError(
            f"업스테이지 캐시를 읽을 수 없어 재과금 호출을 중단합니다: "
            f"{type(exc).__name__}: {_first_line(str(exc))}"
        ) from exc
    if not isinstance(payload, dict):
        raise ParseError("업스테이지 캐시가 JSON 객체가 아니어서 호출을 중단합니다.")
    return payload


def _write_cache_atomic(path: Path, payload: Mapping[str, object]) -> None:
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
            json.dump(payload, destination, ensure_ascii=False, sort_keys=True)
            destination.write("\n")
            temporary_path = Path(destination.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _category_kind(category: str) -> BlockKind:
    if category.startswith(("heading", "title")):
        return "heading"
    return _CATEGORY_KINDS.get(category, "other")


def _element_sort_key(element: object) -> tuple[int, object]:
    if not isinstance(element, Mapping):
        return (2, "")
    element_id = element.get("id")
    if isinstance(element_id, int) and not isinstance(element_id, bool):
        return (0, element_id)
    if isinstance(element_id, str) and element_id.isdecimal():
        return (0, int(element_id))
    return (1, str(element_id))


def _page_number(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ParseError(f"Upstage element page must be an integer: {value!r}")
    return value


def _build_upstage_table(
    html: str,
    *,
    fallback_text: str,
    page: int,
    block_position: int,
) -> tuple[ParsedTable, str]:
    cells: list[TableCell] = []
    nested = False
    try:
        rows, nested = _html_table(html)
        cells = place_html_rows(rows)
        # Validate with the shared grid implementation before serialization.
        cells_to_grid(cells)
        _reject_row_overflow(rows, cells)
        table, text = build_table(page, block_position, cells)
        return replace(table, nested=nested), text
    except Exception as exc:  # noqa: BLE001 - isolate one malformed paid table
        reason = _first_line(f"table_parse: {type(exc).__name__}: {exc}")
        if cells:
            table, text = build_table(page, block_position, cells)
            return replace(table, invalid_reason=reason, nested=nested), text
        empty_table, _empty_text = build_table(page, block_position, ())
        return replace(empty_table, invalid_reason=reason), fallback_text


def _reject_row_overflow(
    rows: Sequence[Sequence[tuple[str, int, int]]],
    cells: Sequence[TableCell],
) -> None:
    """Reject malformed later rows that overflow the table's first-row width."""
    if not rows:
        return
    expected_width = sum(col_span for _text_value, _row_span, col_span in rows[0])
    if any(cell.col + cell.col_span > expected_width for cell in cells):
        raise ValueError("Table cells overlap or exceed the established row width")


class _TableHTMLParser(HTMLParser):
    """Parse one HTML table; inner tables inside a cell are flattened into it.

    An inner table's cell texts join the containing outer cell in document
    order (``join_cell_text``), the same rule as the HWPX golden drafts.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[tuple[str, int, int]]] = []
        self.nested = False
        self._depth = 0  # 1 = outer table, >1 = inside a flattened inner table
        self._table_count = 0
        self._row: list[tuple[str, int, int]] | None = None
        self._cell_parts: list[str] | None = None
        self._cell_span: tuple[int, int] | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        tag = tag.casefold()
        if tag == "table":
            if self._depth == 0:
                self._depth = 1
                self._table_count += 1
            elif self._cell_parts is not None:
                self._depth += 1
                self.nested = True
                self._cell_parts.append("")
            else:
                raise ValueError("nested <table> outside a cell is not supported")
        elif self._depth > 1:
            if tag in {"td", "th"} and self._cell_parts is not None:
                self._cell_parts.append("")
            elif tag == "br" and self._cell_parts is not None:
                self._cell_parts[-1] += " "
        elif tag == "tr" and self._depth == 1:
            if self._row is not None:
                raise ValueError("nested or unclosed <tr>")
            self._row = []
        elif tag in {"td", "th"} and self._depth == 1:
            if self._row is None or self._cell_parts is not None:
                raise ValueError(f"misplaced <{tag}>")
            attributes = {name.casefold(): value for name, value in attrs}
            row_span = _span(attributes.get("rowspan"), "rowspan")
            col_span = _span(attributes.get("colspan"), "colspan")
            self._cell_parts = [""]
            self._cell_span = (row_span, col_span)
        elif tag == "br" and self._cell_parts is not None:
            self._cell_parts[-1] += " "

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() == "br":
            self.handle_starttag(tag, attrs)
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self._depth > 1:
            if tag == "table":
                self._depth -= 1
                if self._cell_parts is not None:
                    self._cell_parts.append("")
            return
        if tag in {"td", "th"} and self._depth == 1:
            if self._row is None or self._cell_parts is None or self._cell_span is None:
                raise ValueError(f"unexpected </{tag}>")
            self._row.append(
                (
                    join_cell_text(self._cell_parts),
                    self._cell_span[0],
                    self._cell_span[1],
                )
            )
            self._cell_parts = None
            self._cell_span = None
        elif tag == "tr" and self._depth == 1:
            if self._row is None or self._cell_parts is not None:
                raise ValueError("unexpected or incomplete </tr>")
            self.rows.append(self._row)
            self._row = None
        elif tag == "table":
            if (
                self._depth != 1
                or self._row is not None
                or self._cell_parts is not None
            ):
                raise ValueError("unexpected or incomplete </table>")
            self._depth = 0

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts[-1] += data

    def finish(self) -> list[list[tuple[str, int, int]]]:
        if self._table_count == 0:
            raise ValueError("<table> not found")
        if self._table_count != 1:
            raise ValueError("exactly one <table> is required")
        if self._depth or self._row is not None or self._cell_parts is not None:
            raise ValueError("unclosed table markup")
        return self.rows


def _html_table(html: str) -> tuple[list[list[tuple[str, int, int]]], bool]:
    """Return outer-table rows and whether inner tables were flattened."""
    parser = _TableHTMLParser()
    parser.feed(html)
    parser.close()
    return parser.finish(), parser.nested


def _span(value: str | None, name: str) -> int:
    if value is None:
        return 1
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer: {value!r}") from exc
    if parsed < 1:
        raise ValueError(f"{name} must be at least 1")
    return parsed


def _has_enhanced_usage(value: object) -> bool:
    return value not in (None, 0, "", [], {})


def _non_negative_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ParseError(f"업스테이지 캐시의 {name} 값이 올바르지 않습니다.")
    return float(value)


def _text(value: object) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def _first_line(message: str) -> str:
    lines = message.splitlines()
    return (lines[0] if lines else "")[:200]
