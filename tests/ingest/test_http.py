"""Tests for the rate-limited ingestion HTTP client."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
import pytest

from kodocs.ingest.http import DEFAULT_USER_AGENT, RateLimitedClient


@dataclass
class FakeTime:
    """A deterministic clock whose sleep advances time immediately."""

    current: float = 100.0
    sleeps: list[float] = field(default_factory=list)

    def clock(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += seconds


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    fake_time: FakeTime,
    **kwargs: object,
) -> RateLimitedClient:
    return RateLimitedClient(
        transport=httpx.MockTransport(handler),
        clock=fake_time.clock,
        sleep=fake_time.sleep,
        **kwargs,
    )


def test_requests_are_spaced_from_the_previous_request_start() -> None:
    fake_time = FakeTime()
    request_times: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_times.append(fake_time.clock())
        return httpx.Response(200, request=request)

    with make_client(handler, fake_time, min_interval_s=1.5) as client:
        client.get("https://example.test/first")
        client.get("https://example.test/second")

    assert request_times == [100.0, 101.5]
    assert fake_time.sleeps == [1.5]


def test_server_error_is_retried_until_success() -> None:
    fake_time = FakeTime()
    statuses = iter([503, 502, 200])
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(next(statuses), request=request)

    with make_client(
        handler,
        fake_time,
        min_interval_s=0,
        max_retries=2,
        backoff_s=2,
    ) as client:
        response = client.get("https://example.test/resource")

    assert response.status_code == 200
    assert attempts == 3
    assert fake_time.sleeps == [2, 4]


def test_client_error_fails_immediately_without_retry() -> None:
    fake_time = FakeTime()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(404, request=request)

    with (
        make_client(handler, fake_time, max_retries=3) as client,
        pytest.raises(httpx.HTTPStatusError, match="HTTP 404"),
    ):
        client.get("https://example.test/missing")

    assert attempts == 1
    assert fake_time.sleeps == []


def test_retry_exhaustion_reports_last_status_with_sanitized_url() -> None:
    fake_time = FakeTime()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, request=request)

    url = "https://example.test/resource?OC=top-secret&target=pi"
    with (
        make_client(
            handler,
            fake_time,
            min_interval_s=0,
            max_retries=2,
            backoff_s=0,
        ) as client,
        pytest.raises(httpx.HTTPStatusError) as exc_info,
    ):
        client.get(url)

    message = str(exc_info.value)
    assert attempts == 3
    assert "HTTP 503" in message
    assert "target=pi" in message
    assert "OC=" not in message
    assert "top-secret" not in message


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
def test_transport_errors_are_retried_and_report_the_last_cause(
    error_type: type[httpx.RequestError],
) -> None:
    fake_time = FakeTime()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise error_type("transport failed", request=request)

    with (
        make_client(
            handler,
            fake_time,
            min_interval_s=0,
            max_retries=1,
            backoff_s=0,
        ) as client,
        pytest.raises(httpx.RequestError, match=error_type.__name__),
    ):
        client.get("https://example.test/resource")

    assert attempts == 2


def test_default_and_per_call_headers_are_sent_and_can_be_overridden() -> None:
    fake_time = FakeTime()
    received_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received_headers.append(request.headers)
        return httpx.Response(200, request=request)

    with make_client(
        handler,
        fake_time,
        headers={"Referer": "https://default.test/", "X-Source": "manifest"},
    ) as client:
        client.get(
            "https://example.test/resource",
            headers={"Referer": "https://request.test/"},
        )

    assert received_headers[0]["User-Agent"] == DEFAULT_USER_AGENT
    assert received_headers[0]["Referer"] == "https://request.test/"
    assert received_headers[0]["X-Source"] == "manifest"


def test_post_json_sends_the_payload() -> None:
    fake_time = FakeTime()
    received_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received_payloads.append(dict(json.loads(request.content)))
        return httpx.Response(200, request=request)

    with make_client(handler, fake_time) as client:
        client.post_json("https://example.test/resource", {"query": "한글"})

    assert received_payloads == [{"query": "한글"}]
