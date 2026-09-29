"""Contracts for the synchronous Anthropic Messages wrapper."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from kodocs.costs import BudgetExceededError, BudgetGuard, CostLedger
from kodocs.llm.anthropic_batch import BatchRequest
from kodocs.llm.anthropic_messages import SYNC_SERVICE_PREFIX, AnthropicMessagesClient
from kodocs.llm.settings import GenerationConfig


def _config() -> GenerationConfig:
    return GenerationConfig(
        model="claude-sonnet-5",
        max_tokens=100,
        thinking="disabled",
        effort=None,
        temperature=None,
        max_attempts=3,
        pilot_limit=1,
        chars_per_token_estimate=0.8,
    )


def _message(text: str) -> object:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        model="claude-sonnet-5",
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=20,
            output_tokens=10,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


def _status_error(cls: type, status: int) -> Exception:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("err", response=httpx.Response(status, request=request), body=None)


class FakeSDK:
    def __init__(self, script: dict[str, list[object]] | None = None) -> None:
        self.script = script or {}
        self.calls: list[dict[str, object]] = []
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()
        self.messages = SimpleNamespace(create=self._create)
        self.factory_kwargs: dict[str, object] = {}

    def _create(self, **params: object) -> object:
        with self.lock:
            self.calls.append(params)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(0.02)
            user = params["messages"][0]["content"]  # type: ignore[index]
            steps = self.script.get(str(user))
            if steps:
                step = steps.pop(0)
                if isinstance(step, Exception):
                    raise step
                return step
            return _message(f"답 {user}")
        finally:
            with self.lock:
                self.active -= 1


def _client(
    tmp_path: Path, sdk: FakeSDK, *, budget: float = 10.0
) -> tuple[AnthropicMessagesClient, CostLedger]:
    ledger = CostLedger(tmp_path / "costs.jsonl")

    def factory(**kwargs: object) -> object:
        sdk.factory_kwargs = kwargs
        return sdk

    client = AnthropicMessagesClient(
        client_factory=factory,
        config=_config(),
        ledger=ledger,
        guard=BudgetGuard(ledger, budget),
        run_id="sync-test",
        sleep=lambda _seconds: None,
    )
    return client, ledger


def _requests(n: int) -> list[BatchRequest]:
    return [BatchRequest(f"q-{i:02d}-a1", "시스템", f"u{i}", f"k{i}") for i in range(n)]


def test_concurrency_is_capped_and_results_keep_request_order(tmp_path: Path) -> None:
    sdk = FakeSDK()
    client, _ = _client(tmp_path, sdk)

    results = client.create_many(_requests(12))

    assert sdk.peak <= 4
    assert [r.custom_id for r in results] == [f"q-{i:02d}-a1" for i in range(12)]
    assert all(r.result_type == "succeeded" for r in results)
    assert sdk.factory_kwargs["max_retries"] == 0
    assert sdk.factory_kwargs["timeout"] > 0


def test_usage_is_billed_at_synchronous_prices(tmp_path: Path) -> None:
    sdk = FakeSDK()
    client, ledger = _client(tmp_path, sdk)

    client.create_many(_requests(2))

    rows = [json.loads(x) for x in ledger.path.read_text().splitlines()]
    assert sorted({r["service"] for r in rows}) == [
        f"{SYNC_SERVICE_PREFIX}.input",
        f"{SYNC_SERVICE_PREFIX}.output",
    ]
    assert {r["run_id"] for r in rows} == {"sync-test"}
    assert sum(r["quantity"] for r in rows) == 2 * (20 + 10)


@pytest.mark.parametrize(
    "error",
    [
        _status_error(anthropic.RateLimitError, 429),
        _status_error(anthropic.InternalServerError, 500),
        anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com")
        ),
    ],
)
def test_transient_errors_are_retried(tmp_path: Path, error: Exception) -> None:
    sdk = FakeSDK({"u0": [error, _message("ok")]})
    client, _ = _client(tmp_path, sdk)

    [result] = client.create_many(_requests(1))

    assert result.result_type == "succeeded"
    assert result.text == "ok"
    assert len(sdk.calls) == 2


def test_other_errors_fail_the_request_without_retry(tmp_path: Path) -> None:
    sdk = FakeSDK({"u0": [_status_error(anthropic.BadRequestError, 400)]})
    client, ledger = _client(tmp_path, sdk)

    [result] = client.create_many(_requests(1))

    assert result.result_type == "errored"
    assert len(sdk.calls) == 1
    assert not ledger.path.exists()


def test_retries_stop_after_the_limit(tmp_path: Path) -> None:
    errors = [_status_error(anthropic.RateLimitError, 429) for _ in range(5)]
    sdk = FakeSDK({"u0": errors})
    client, _ = _client(tmp_path, sdk)

    [result] = client.create_many(_requests(1))

    assert result.result_type == "errored"
    assert len(sdk.calls) == 3


def test_budget_guard_blocks_before_any_call(tmp_path: Path) -> None:
    sdk = FakeSDK()
    client, _ = _client(tmp_path, sdk, budget=0.0)

    with pytest.raises(BudgetExceededError):
        client.create_many(_requests(1))
    assert sdk.calls == []
