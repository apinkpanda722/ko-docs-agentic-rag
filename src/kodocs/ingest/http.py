"""Rate-limited HTTP access for ingestion sources."""

import time
from collections.abc import Callable, Mapping
from typing import Self

import httpx

from kodocs.ingest.manifest import sanitize_url

DEFAULT_USER_AGENT: str = "kodocs-ingest/0.1 (+research; local evaluation)"


class RateLimitedClient:
    """Synchronous HTTP client with request spacing and bounded retries."""

    def __init__(
        self,
        *,
        min_interval_s: float = 1.0,
        max_retries: int = 3,
        backoff_s: float = 2.0,
        timeout_s: float = 30.0,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
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

        default_headers = {"User-Agent": DEFAULT_USER_AGENT}
        if headers is not None:
            default_headers.update(headers)

        self._client = httpx.Client(
            headers=default_headers,
            timeout=timeout_s,
            transport=transport,
        )
        self._min_interval_s = min_interval_s
        self._max_retries = max_retries
        self._backoff_s = backoff_s
        self._clock = clock
        self._sleep = sleep
        self._last_request_started: float | None = None

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Send a GET request, retrying only transient failures."""
        return self._request("GET", url, params=params, headers=headers)

    def post_json(
        self,
        url: str,
        payload: Mapping[str, object],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Send a JSON POST request, retrying only transient failures."""
        return self._request("POST", url, json=payload, headers=headers)

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        url: str,
        **kwargs: object,
    ) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            self._wait_for_request_slot()
            try:
                response = self._client.request(method, url, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt < self._max_retries:
                    self._sleep(self._backoff_s * 2**attempt)
                    continue
                safe_request = _safe_request(exc.request)
                error_type = type(exc)
                raise error_type(
                    f"{error_type.__name__} after {attempt + 1} attempts for "
                    f"{safe_request.method} {safe_request.url}",
                    request=safe_request,
                ) from exc

            if 500 <= response.status_code < 600 and attempt < self._max_retries:
                self._sleep(self._backoff_s * 2**attempt)
                continue
            if response.status_code >= 400:
                safe_request = _safe_request(response.request)
                raise httpx.HTTPStatusError(
                    f"HTTP {response.status_code} for "
                    f"{safe_request.method} {safe_request.url}",
                    request=safe_request,
                    response=response,
                )
            return response

        raise AssertionError("retry loop exited unexpectedly")

    def _wait_for_request_slot(self) -> None:
        now = self._clock()
        if self._last_request_started is not None:
            remaining = self._min_interval_s - (now - self._last_request_started)
            if remaining > 0:
                self._sleep(remaining)
                now = self._clock()
        self._last_request_started = now


def _safe_request(request: httpx.Request) -> httpx.Request:
    return httpx.Request(request.method, sanitize_url(str(request.url)))
