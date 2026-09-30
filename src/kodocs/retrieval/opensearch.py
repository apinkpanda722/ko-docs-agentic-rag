"""Thin synchronous HTTP client for the local OpenSearch service."""

from __future__ import annotations

import json as jsonlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Self

import httpx

_ERROR_BODY_LIMIT = 200


class OpenSearchError(RuntimeError):
    """An OpenSearch HTTP or response-shape failure with a bounded body."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body[:_ERROR_BODY_LIMIT]
        self.body_summary = self.body
        super().__init__(f"OpenSearch returned HTTP {status}: {self.body}")


@dataclass(frozen=True)
class BulkItemError:
    """One failed action from an otherwise successful bulk HTTP request."""

    action: str
    doc_id: str | None
    status: int | None
    error: object


@dataclass(frozen=True)
class BulkResult:
    """Counts and item failures returned by an OpenSearch bulk request."""

    took_ms: int
    item_count: int
    errors: tuple[BulkItemError, ...]

    @property
    def error_count(self) -> int:
        """Return the number of failed bulk actions."""
        return len(self.errors)

    @property
    def succeeded_count(self) -> int:
        """Return the number of successful bulk actions."""
        return self.item_count - self.error_count


class OpenSearchClient:
    """Make explicit JSON and NDJSON requests without a search abstraction."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:9200",
        *,
        client: httpx.Client | None = None,
    ) -> None:
        normalized = base_url.rstrip("/")
        if not normalized:
            raise ValueError("base_url must not be empty")
        self.base_url = normalized
        self._client = client if client is not None else httpx.Client(timeout=30.0)
        self._owns_client = client is None

    def request(
        self,
        method: str,
        path: str,
        json: object | None = None,
    ) -> dict[str, Any]:
        """Send one JSON request and reject HTTP errors with a bounded body."""
        response = self._client.request(method, self._url(path), json=json)
        self._raise_for_status(response)
        return self._json_object(response)

    def bulk(
        self,
        index: str,
        docs: Iterable[tuple[str, dict]],
    ) -> BulkResult:
        """Index documents as NDJSON and retain every item-level failure."""
        lines: list[str] = []
        for doc_id, document in docs:
            action = {"index": {"_index": index, "_id": doc_id}}
            lines.append(
                jsonlib.dumps(action, ensure_ascii=False, separators=(",", ":"))
            )
            lines.append(
                jsonlib.dumps(document, ensure_ascii=False, separators=(",", ":"))
            )
        if not lines:
            return BulkResult(took_ms=0, item_count=0, errors=())

        payload = ("\n".join(lines) + "\n").encode("utf-8")
        response = self._client.request(
            "POST",
            self._url("/_bulk"),
            content=payload,
            headers={"Content-Type": "application/x-ndjson"},
        )
        self._raise_for_status(response)
        result = self._json_object(response)
        items = result.get("items")
        if not isinstance(items, list):
            raise OpenSearchError(
                response.status_code, self._response_summary(response)
            )

        errors: list[BulkItemError] = []
        for item in items:
            parsed = self._bulk_item_error(item)
            if parsed is not None:
                errors.append(parsed)
        took = result.get("took", 0)
        took_ms = took if isinstance(took, int) and not isinstance(took, bool) else 0
        return BulkResult(
            took_ms=took_ms,
            item_count=len(items),
            errors=tuple(errors),
        )

    def close(self) -> None:
        """Close the internally-created HTTP client, if any."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _url(self, path: str) -> str:
        normalized_path = path if path.startswith("/") else f"/{path}"
        return f"{self.base_url}{normalized_path}"

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_error:
            raise OpenSearchError(
                response.status_code,
                OpenSearchClient._response_summary(response),
            )

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        try:
            value = response.json()
        except ValueError as exc:
            raise OpenSearchError(
                response.status_code,
                OpenSearchClient._response_summary(response),
            ) from exc
        if not isinstance(value, dict):
            raise OpenSearchError(
                response.status_code,
                OpenSearchClient._response_summary(response),
            )
        return value

    @staticmethod
    def _response_summary(response: httpx.Response) -> str:
        return response.text[:_ERROR_BODY_LIMIT]

    @staticmethod
    def _bulk_item_error(item: object) -> BulkItemError | None:
        if not isinstance(item, Mapping) or len(item) != 1:
            return BulkItemError(
                action="unknown",
                doc_id=None,
                status=None,
                error="invalid bulk item response",
            )
        action, details = next(iter(item.items()))
        if not isinstance(details, Mapping):
            return BulkItemError(
                action=str(action),
                doc_id=None,
                status=None,
                error="invalid bulk item details",
            )

        status_value = details.get("status")
        status = (
            status_value
            if isinstance(status_value, int) and not isinstance(status_value, bool)
            else None
        )
        error = details.get("error")
        if error is None and (status is None or status < 400):
            return None
        doc_id_value = details.get("_id")
        return BulkItemError(
            action=str(action),
            doc_id=doc_id_value if isinstance(doc_id_value, str) else None,
            status=status,
            error=error if error is not None else "bulk action failed",
        )
