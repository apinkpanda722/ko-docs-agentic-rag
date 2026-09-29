"""Synchronous Anthropic Messages calls with bounded concurrency and billing."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic

from kodocs.costs import BudgetGuard, CostLedger, estimate_usd
from kodocs.costs import budget_estimate_usd as guarded_estimate_usd
from kodocs.llm.anthropic_batch import (
    BatchRequest,
    BatchResult,
    CostEstimate,
    _non_negative_int_attr,
    _optional_string_attr,
    _optional_usage,
    _required_string_attr,
)
from kodocs.llm.settings import GenerationConfig

SYNC_SERVICE_PREFIX = "anthropic.claude-sonnet-5"
MAX_CONCURRENCY = 4
REQUEST_TIMEOUT_S = 60.0
MAX_RETRIES = 2
_RETRYABLE = (
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.APIConnectionError,
)


class AnthropicMessagesClient:
    """Create messages one by one (at most ``max_concurrency`` at a time)."""

    def __init__(
        self,
        *,
        client_factory: Callable[..., object],
        config: GenerationConfig,
        ledger: CostLedger,
        guard: BudgetGuard,
        run_id: str,
        max_concurrency: int = MAX_CONCURRENCY,
        timeout_s: float = REQUEST_TIMEOUT_S,
        max_retries: int = MAX_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._ledger = ledger
        self._guard = guard
        self.run_id = run_id
        self._max_concurrency = max_concurrency
        self._max_retries = max_retries
        self._sleep = sleep
        # SDK retries are off: only 429, 5xx and connection errors are retried here.
        self._sdk = client_factory(max_retries=0, timeout=timeout_s)

    def estimate(self, requests: Sequence[BatchRequest]) -> CostEstimate:
        """Estimate input cost and the token-limited worst case at sync prices."""
        return estimate_requests(requests, self._config)

    def create_many(self, requests: Sequence[BatchRequest]) -> list[BatchResult]:
        """Run every request after one budget check; results keep request order."""
        self._guard.check(self.estimate(requests).budget_estimate_usd)
        results: dict[str, BatchResult] = {}
        with ThreadPoolExecutor(max_workers=self._max_concurrency) as pool:
            futures = {pool.submit(self._create_one, r): r for r in requests}
            for future in as_completed(futures):
                result = future.result()
                self._record_usage(result)
                results[result.custom_id] = result
        return [results[request.custom_id] for request in requests]

    def _create_one(self, request: BatchRequest) -> BatchResult:
        attempt = 0
        while True:
            try:
                message = self._sdk.messages.create(  # type: ignore[attr-defined]
                    **self._config.request_params(),
                    system=request.system,
                    messages=[{"role": "user", "content": request.user}],
                )
            except _RETRYABLE as exc:
                if attempt >= self._max_retries:
                    return _errored(request, type(exc).__name__)
                self._sleep(min(2.0**attempt, 20.0))
                attempt += 1
                continue
            except anthropic.APIError as exc:
                return _errored(request, type(exc).__name__)
            return _succeeded(request, message)

    def _record_usage(self, result: BatchResult) -> None:
        if result.result_type != "succeeded" or result.usage is None:
            return
        cached = result.usage.get("cache_creation_input_tokens", 0) + result.usage.get(
            "cache_read_input_tokens", 0
        )
        if cached:
            raise ValueError("cache token pricing for sync calls is not configured")
        for suffix, key in (("input", "input_tokens"), ("output", "output_tokens")):
            quantity = result.usage[key]
            if quantity > 0:
                self._ledger.record(
                    service=f"{SYNC_SERVICE_PREFIX}.{suffix}",
                    model=result.model or self._config.model,
                    quantity=quantity,
                    ref=result.custom_id,
                    run_id=self.run_id,
                )


def estimate_requests(
    requests: Sequence[BatchRequest], config: GenerationConfig
) -> CostEstimate:
    input_tokens = sum(
        math.ceil((len(r.system) + len(r.user)) / config.chars_per_token_estimate)
        for r in requests
    )
    output_tokens = len(requests) * config.max_tokens
    input_cost = estimate_usd(f"{SYNC_SERVICE_PREFIX}.input", input_tokens)
    return CostEstimate(
        estimated_input_tokens=input_tokens,
        maximum_output_tokens=output_tokens,
        expected_cost_usd=input_cost,
        maximum_cost_usd=input_cost
        + estimate_usd(f"{SYNC_SERVICE_PREFIX}.output", output_tokens),
        budget_estimate_usd=guarded_estimate_usd(
            f"{SYNC_SERVICE_PREFIX}.input", input_tokens
        )
        + guarded_estimate_usd(f"{SYNC_SERVICE_PREFIX}.output", output_tokens),
    )


def _succeeded(request: BatchRequest, message: object) -> BatchResult:
    text = "".join(
        str(block.text)
        for block in message.content  # type: ignore[attr-defined]
        if getattr(block, "type", None) == "text"
    )
    usage = message.usage  # type: ignore[attr-defined]
    return BatchResult(
        custom_id=request.custom_id,
        result_type="succeeded",
        text=text,
        model=_required_string_attr(message, "model"),
        stop_reason=_optional_string_attr(message, "stop_reason"),
        usage={
            "input_tokens": _non_negative_int_attr(usage, "input_tokens"),
            "output_tokens": _non_negative_int_attr(usage, "output_tokens"),
            "cache_creation_input_tokens": _optional_usage(
                usage, "cache_creation_input_tokens"
            ),
            "cache_read_input_tokens": _optional_usage(
                usage, "cache_read_input_tokens"
            ),
        },
        error=None,
    )


def _errored(request: BatchRequest, error: str) -> BatchResult:
    return BatchResult(
        custom_id=request.custom_id,
        result_type="errored",
        text=None,
        model=None,
        stop_reason=None,
        usage=None,
        error=error,
    )
