"""Crash-safe Anthropic Message Batches submission, collection, and billing."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from kodocs.costs import BudgetGuard, CostLedger, estimate_usd
from kodocs.costs import budget_estimate_usd as guarded_estimate_usd
from kodocs.ingest.manifest import append_record, latest_records, load_records
from kodocs.llm.settings import GenerationConfig

BATCHES_MANIFEST = Path("data/manifest/anthropic_batches.jsonl")
SERVICE_PREFIX = "anthropic.claude-sonnet-5.batch"
CUSTOM_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_MAX_REQUESTS = 100_000
_COMMON_FIELDS = (
    "submission_id",
    "state",
    "ts",
    "n_requests",
    "custom_ids",
    "request_set_hash",
    "generation",
    "settings_hash",
)
_RESULT_TYPES = frozenset({"succeeded", "errored", "canceled", "expired"})
_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class BatchRequest:
    custom_id: str
    system: str
    user: str
    cache_key: str


@dataclass(frozen=True)
class BatchResult:
    custom_id: str
    result_type: Literal[
        "succeeded",
        "errored",
        "canceled",
        "expired",
        "missing",
    ]
    text: str | None
    model: str | None
    stop_reason: str | None
    usage: Mapping[str, int] | None
    error: str | None


@dataclass(frozen=True)
class CostEstimate:
    estimated_input_tokens: int
    maximum_output_tokens: int
    expected_cost_usd: float
    maximum_cost_usd: float
    budget_estimate_usd: float

    @property
    def input_tokens(self) -> int:
        return self.estimated_input_tokens

    @property
    def max_output_tokens(self) -> int:
        return self.maximum_output_tokens

    @property
    def estimated_cost_usd(self) -> float:
        return self.expected_cost_usd

    @property
    def expected_usd(self) -> float:
        return self.expected_cost_usd

    @property
    def max_cost_usd(self) -> float:
        return self.maximum_cost_usd

    @property
    def maximum_usd(self) -> float:
        return self.maximum_cost_usd

    @property
    def budget_usd(self) -> float:
        return self.budget_estimate_usd


@dataclass(frozen=True)
class BatchStatus:
    batch_id: str
    processing_status: Literal["in_progress", "canceling", "ended"]
    request_counts: Mapping[str, int]
    created_at: datetime
    expires_at: datetime
    ended_at: datetime | None
    elapsed_s: float
    warnings: tuple[str, ...]

    @property
    def elapsed_seconds(self) -> float:
        return self.elapsed_s

    @property
    def counts(self) -> Mapping[str, int]:
        return self.request_counts


class SubmitUncertainError(RuntimeError):
    """Raised when create may have succeeded and must never be repeated."""


class SubmitRejectedError(RuntimeError):
    """Raised when a client-side/API rejection proves no batch was created."""


class AnthropicBatchClient:
    """Wrap Message Batches without exposing credentials or retrying creation."""

    def __init__(
        self,
        *,
        client_factory: Callable[..., object],
        config: GenerationConfig,
        ledger: CostLedger,
        guard: BudgetGuard,
        manifest_path: Path = BATCHES_MANIFEST,
        clock: Callable[[], datetime] = _utc_now,
        new_id: Callable[[], object] = uuid.uuid4,
    ) -> None:
        self._client_factory = client_factory
        self._config = config
        self._ledger = ledger
        self._guard = guard
        self._manifest_path = Path(manifest_path)
        self._clock = clock
        self._new_id = new_id
        self._read_client: object | None = None
        self._collection_summaries: dict[
            str,
            tuple[dict[str, int], dict[str, int]],
        ] = {}

    def estimate(self, requests: Sequence[BatchRequest]) -> CostEstimate:
        """Estimate known input cost and the worst-case token-limited charge."""
        input_tokens = sum(
            math.ceil(
                (len(request.system) + len(request.user))
                / self._config.chars_per_token_estimate
            )
            for request in requests
        )
        maximum_output_tokens = len(requests) * self._config.max_tokens
        input_cost = estimate_usd(f"{SERVICE_PREFIX}.input", input_tokens)
        maximum_cost = input_cost + estimate_usd(
            f"{SERVICE_PREFIX}.output",
            maximum_output_tokens,
        )
        guarded_cost = guarded_estimate_usd(
            f"{SERVICE_PREFIX}.input",
            input_tokens,
        ) + guarded_estimate_usd(
            f"{SERVICE_PREFIX}.output",
            maximum_output_tokens,
        )
        return CostEstimate(
            estimated_input_tokens=input_tokens,
            maximum_output_tokens=maximum_output_tokens,
            expected_cost_usd=input_cost,
            maximum_cost_usd=maximum_cost,
            budget_estimate_usd=guarded_cost,
        )

    def submit(self, requests: Sequence[BatchRequest]) -> str:
        """Create one batch exactly once, persisting ambiguity before returning."""
        materialized = tuple(requests)
        self._validate_requests(materialized)
        unresolved = self.uncertain_submissions()
        if unresolved:
            identifiers = ", ".join(
                str(record["submission_id"]) for record in unresolved
            )
            raise SubmitUncertainError(
                "Uncertain Anthropic submission(s) block new create calls: "
                f"{identifiers}. Check the Anthropic console, then call adopt "
                "with its batch_id or abandon after confirming no batch exists."
            )

        estimate = self.estimate(materialized)
        self._guard.check(estimate.budget_estimate_usd)
        submission_id = self._submission_id()
        common = self._common_record(submission_id, materialized)
        self._append_state(common, state="submitting")

        try:
            # Creation is deliberately isolated on a max_retries=0 SDK client.
            # A resend after a lost response could create and bill a second batch.
            client = self._client_factory(max_retries=0)
            batch = client.messages.batches.create(
                requests=[self._sdk_request(request) for request in materialized]
            )
            batch_id = _required_string_attr(batch, "id")
            expires_at = _iso_attr(batch, "expires_at")
        except Exception as exc:  # noqa: BLE001 - classify every create failure
            status_code = getattr(exc, "status_code", None)
            if isinstance(exc, anthropic.APIStatusError) and (
                isinstance(status_code, int) and status_code < 500
            ):
                self._append_state(
                    common,
                    state="submit_rejected",
                    status_code=status_code,
                    error=type(exc).__name__,
                )
                raise SubmitRejectedError(
                    f"Anthropic batch create was rejected with HTTP {status_code}; "
                    "the request was not retried."
                ) from None

            self._append_state(
                common,
                state="submit_uncertain",
                error_type=type(exc).__name__,
                status_code=status_code if isinstance(status_code, int) else None,
            )
            raise SubmitUncertainError(
                "Anthropic batch create outcome is uncertain; the request was not "
                f"retried. Resolve submission {submission_id} from the console with "
                "adopt or abandon before submitting again."
            ) from None

        self._append_state(
            common,
            state="submitted",
            batch_id=batch_id,
            expires_at=expires_at,
        )
        return batch_id

    def status(self, batch_id: str) -> BatchStatus:
        """Retrieve status with expiry and result-retention warnings."""
        batch = self._reader().messages.batches.retrieve(batch_id)
        processing_status = _required_string_attr(batch, "processing_status")
        if processing_status not in {"in_progress", "canceling", "ended"}:
            raise ValueError(
                f"Unknown Anthropic processing_status: {processing_status}"
            )
        created_at = _datetime_attr(batch, "created_at")
        expires_at = _datetime_attr(batch, "expires_at")
        ended_at = _optional_datetime_attr(batch, "ended_at")
        now = _as_utc(self._clock(), name="clock result")
        elapsed_s = max(0.0, (now - created_at).total_seconds())
        warnings: list[str] = []
        if processing_status != "ended" and now - created_at >= timedelta(hours=24):
            warnings.append(
                "Batch has not ended after 24 hours; Anthropic batches expire at 24 hours."
            )
        if now - created_at >= timedelta(days=25) and not self._is_collected(batch_id):
            warnings.append(
                "Batch is 25 days old and uncollected; results are retained for 29 days."
            )
        counts = {
            name: _non_negative_int_attr(batch.request_counts, name)
            for name in ("processing", "succeeded", "errored", "canceled", "expired")
        }
        return BatchStatus(
            batch_id=_required_string_attr(batch, "id"),
            processing_status=processing_status,  # type: ignore[arg-type]
            request_counts=counts,
            created_at=created_at,
            expires_at=expires_at,
            ended_at=ended_at,
            elapsed_s=elapsed_s,
            warnings=tuple(warnings),
        )

    def collect(self, batch_id: str) -> list[BatchResult]:
        """Collect by custom_id and record only succeeded usage, idempotently."""
        status = self.status(batch_id)
        if status.processing_status != "ended":
            raise RuntimeError(
                f"Anthropic batch {batch_id} is not ended: {status.processing_status}"
            )
        submission = self._submission_for_batch(batch_id)
        custom_ids = _string_list(submission.get("custom_ids"), name="custom_ids")
        expected_ids = set(custom_ids)
        by_id: dict[str, BatchResult] = {}
        for item in self._reader().messages.batches.results(batch_id):
            custom_id = _required_string_attr(item, "custom_id")
            if custom_id not in expected_ids:
                raise ValueError(
                    f"Anthropic batch {batch_id} returned unknown custom_id: {custom_id}"
                )
            if custom_id in by_id:
                raise ValueError(
                    f"Anthropic batch {batch_id} returned duplicate custom_id: {custom_id}"
                )
            by_id[custom_id] = self._batch_result(item)

        results = [
            by_id.get(
                custom_id,
                BatchResult(
                    custom_id=custom_id,
                    result_type="missing",
                    text=None,
                    model=None,
                    stop_reason=None,
                    usage=None,
                    error="missing",
                ),
            )
            for custom_id in custom_ids
        ]
        self._record_usage(batch_id, results)
        self._collection_summaries[batch_id] = self._summarize(results)
        return results

    def mark_collected(self, batch_id: str) -> None:
        """Append the console-reconciliation totals after outputs are safely stored."""
        submission = self._submission_for_batch(batch_id)
        if submission.get("state") == "collected":
            return
        if batch_id not in self._collection_summaries:
            self.collect(batch_id)
        result_counts, usage_totals = self._collection_summaries[batch_id]
        common = self._copy_common(submission)
        self._append_state(
            common,
            state="collected",
            batch_id=batch_id,
            expires_at=submission.get("expires_at"),
            result_counts=result_counts,
            usage_totals=usage_totals,
        )

    def open_batches(self) -> list[Mapping[str, object]]:
        """Return submitted/adopted batches whose latest state is not collected."""
        return [
            record
            for record in self._latest_submissions()
            if record.get("state") in {"submitted", "adopted"}
        ]

    def uncertain_submissions(self) -> list[Mapping[str, object]]:
        """Return create attempts whose server-side outcome needs human resolution."""
        return [
            record
            for record in self._latest_submissions()
            if record.get("state") in {"submitting", "submit_uncertain"}
        ]

    def adopt(self, submission_id: str, batch_id: str) -> None:
        """Attach the console-confirmed batch ID to an uncertain submission."""
        if not batch_id:
            raise ValueError("batch_id must not be empty")
        record = self._uncertain_record(submission_id)
        self._append_state(
            self._copy_common(record),
            state="adopted",
            batch_id=batch_id,
        )

    def abandon(self, submission_id: str, reason: str) -> None:
        """Resolve an uncertainty after the user confirms no batch was created."""
        if not reason.strip():
            raise ValueError("reason must not be empty")
        record = self._uncertain_record(submission_id)
        self._append_state(
            self._copy_common(record),
            state="abandoned",
            reason=_safe_text(reason),
        )

    def _validate_requests(self, requests: Sequence[BatchRequest]) -> None:
        if not requests:
            raise ValueError("Anthropic batch requests must not be empty")
        if len(requests) > _MAX_REQUESTS:
            raise ValueError(
                f"Anthropic batch supports at most {_MAX_REQUESTS} requests"
            )
        custom_ids: list[str] = []
        for request in requests:
            if not CUSTOM_ID_RE.fullmatch(request.custom_id):
                raise ValueError(
                    "custom_id must match ^[a-zA-Z0-9_-]{1,64}$: "
                    f"{request.custom_id!r}"
                )
            if not all(
                isinstance(value, str)
                for value in (request.system, request.user, request.cache_key)
            ):
                raise ValueError("Batch request text and cache_key must be strings")
            custom_ids.append(request.custom_id)
        if len(set(custom_ids)) != len(custom_ids):
            raise ValueError("custom_id values must be unique within a batch")

    def _sdk_request(self, request: BatchRequest) -> Request:
        params = MessageCreateParamsNonStreaming(
            **self._config.request_params(),
            system=request.system,
            messages=[{"role": "user", "content": request.user}],
        )
        return Request(custom_id=request.custom_id, params=params)

    def _batch_result(self, item: object) -> BatchResult:
        result = item.result
        result_type = _required_string_attr(result, "type")
        if result_type not in _RESULT_TYPES:
            raise ValueError(f"Unknown Anthropic batch result type: {result_type}")
        custom_id = _required_string_attr(item, "custom_id")
        if result_type == "succeeded":
            message = result.message
            text = "".join(
                str(block.text)
                for block in message.content
                if getattr(block, "type", None) == "text"
            )
            usage = {
                "input_tokens": _non_negative_int_attr(
                    message.usage,
                    "input_tokens",
                ),
                "output_tokens": _non_negative_int_attr(
                    message.usage,
                    "output_tokens",
                ),
                "cache_creation_input_tokens": _optional_usage(
                    message.usage,
                    "cache_creation_input_tokens",
                ),
                "cache_read_input_tokens": _optional_usage(
                    message.usage,
                    "cache_read_input_tokens",
                ),
            }
            return BatchResult(
                custom_id=custom_id,
                result_type="succeeded",
                text=text,
                model=_required_string_attr(message, "model"),
                stop_reason=_optional_string_attr(message, "stop_reason"),
                usage=usage,
                error=None,
            )
        error = result_type
        if result_type == "errored":
            error_object = getattr(result, "error", None)
            documented_type = getattr(error_object, "type", None)
            error = (
                documented_type
                if isinstance(documented_type, str) and documented_type
                else _safe_text(str(error_object))
            )
        return BatchResult(
            custom_id=custom_id,
            result_type=result_type,  # type: ignore[arg-type]
            text=None,
            model=None,
            stop_reason=None,
            usage=None,
            error=error,
        )

    def _record_usage(self, batch_id: str, results: Sequence[BatchResult]) -> None:
        seen = self._recorded_ledger_keys()
        for result in results:
            if result.result_type != "succeeded":
                continue
            if result.usage is None or result.model is None:
                raise ValueError("Succeeded result is missing usage or model")
            services = (
                (f"{SERVICE_PREFIX}.input", result.usage["input_tokens"]),
                (f"{SERVICE_PREFIX}.output", result.usage["output_tokens"]),
                (
                    f"{SERVICE_PREFIX}.cache-write-5m",
                    result.usage["cache_creation_input_tokens"],
                ),
                (
                    f"{SERVICE_PREFIX}.cache-read",
                    result.usage["cache_read_input_tokens"],
                ),
            )
            for service, quantity in services:
                if quantity <= 0:
                    continue
                key = (batch_id, result.custom_id, service)
                if key in seen:
                    continue
                self._ledger.record(
                    service=service,
                    model=result.model,
                    quantity=quantity,
                    ref=result.custom_id,
                    run_id=batch_id,
                )
                seen.add(key)

    def _recorded_ledger_keys(self) -> set[tuple[str, str, str]]:
        keys: set[tuple[str, str, str]] = set()
        for record in load_records(self._ledger.path):
            run_id = record.get("run_id")
            ref = record.get("ref")
            service = record.get("service")
            if all(isinstance(value, str) for value in (run_id, ref, service)):
                keys.add((run_id, ref, service))  # type: ignore[arg-type]
        return keys

    def _summarize(
        self,
        results: Sequence[BatchResult],
    ) -> tuple[dict[str, int], dict[str, int]]:
        counts = Counter(result.result_type for result in results)
        result_counts = {
            result_type: counts[result_type]
            for result_type in (
                "succeeded",
                "errored",
                "canceled",
                "expired",
                "missing",
            )
        }
        usage_totals = {
            name: sum(
                result.usage[name]
                for result in results
                if result.result_type == "succeeded" and result.usage is not None
            )
            for name in _USAGE_FIELDS
        }
        return result_counts, usage_totals

    def _reader(self) -> object:
        if self._read_client is None:
            self._read_client = self._client_factory()
        return self._read_client

    def _common_record(
        self,
        submission_id: str,
        requests: Sequence[BatchRequest],
    ) -> dict[str, object]:
        request_payload = [asdict(request) for request in requests]
        serialized = json.dumps(
            request_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return {
            "submission_id": submission_id,
            "n_requests": len(requests),
            "custom_ids": [request.custom_id for request in requests],
            "request_set_hash": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            "generation": self._config.record(),
            "settings_hash": self._config.settings_hash(),
        }

    def _append_state(
        self,
        common: Mapping[str, object],
        *,
        state: str,
        **extra: object,
    ) -> None:
        record = {
            **common,
            "state": state,
            "ts": _as_utc(self._clock(), name="clock result").isoformat(),
            **extra,
        }
        append_record(self._manifest_path, record, required=_COMMON_FIELDS)

    def _latest_submissions(self) -> list[Mapping[str, object]]:
        return latest_records(
            load_records(self._manifest_path),
            key=lambda record: record["submission_id"],
        )

    def _submission_for_batch(self, batch_id: str) -> Mapping[str, object]:
        matches = [
            record
            for record in self._latest_submissions()
            if record.get("batch_id") == batch_id
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Expected one manifest submission for batch_id {batch_id!r}; "
                f"found {len(matches)}"
            )
        return matches[0]

    def _uncertain_record(self, submission_id: str) -> Mapping[str, object]:
        matches = [
            record
            for record in self.uncertain_submissions()
            if record.get("submission_id") == submission_id
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Uncertain submission not found or already resolved: {submission_id}"
            )
        return matches[0]

    def _copy_common(self, record: Mapping[str, object]) -> dict[str, object]:
        return {
            name: record[name] for name in _COMMON_FIELDS if name not in {"state", "ts"}
        }

    def _is_collected(self, batch_id: str) -> bool:
        return any(
            record.get("batch_id") == batch_id and record.get("state") == "collected"
            for record in self._latest_submissions()
        )

    def _submission_id(self) -> str:
        value = self._new_id()
        identifier = value.hex if isinstance(value, uuid.UUID) else str(value)
        if not identifier:
            raise ValueError("new_id must return a non-empty identifier")
        return identifier


def _required_string_attr(value: object, name: str) -> str:
    result = getattr(value, name, None)
    if not isinstance(result, str) or not result:
        raise ValueError(f"Anthropic response {name} must be a non-empty string")
    return result


def _optional_string_attr(value: object, name: str) -> str | None:
    result = getattr(value, name, None)
    if result is None:
        return None
    if not isinstance(result, str):
        raise TypeError(f"Anthropic response {name} must be a string or null")
    return result


def _non_negative_int_attr(value: object, name: str) -> int:
    result = getattr(value, name, None)
    if isinstance(result, bool) or not isinstance(result, int) or result < 0:
        raise ValueError(f"Anthropic response {name} must be a non-negative integer")
    return result


def _optional_usage(value: object, name: str) -> int:
    result = getattr(value, name, None)
    if result is None:
        return 0
    return _non_negative_int_attr(value, name)


def _datetime_attr(value: object, name: str) -> datetime:
    raw = getattr(value, name, None)
    if isinstance(raw, str):
        try:
            raw = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"Anthropic response {name} is not ISO 8601") from exc
    return _as_utc(raw, name=f"Anthropic response {name}")


def _optional_datetime_attr(value: object, name: str) -> datetime | None:
    if getattr(value, name, None) is None:
        return None
    return _datetime_attr(value, name)


def _iso_attr(value: object, name: str) -> str | None:
    if getattr(value, name, None) is None:
        return None
    return _datetime_attr(value, name).isoformat()


def _as_utc(value: object, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _string_list(value: object, *, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Manifest {name} must be a list of strings")
    return value


def _safe_text(value: str) -> str:
    safe = re.sub(r"sk-ant-[A-Za-z0-9_-]+", "[REDACTED]", value)
    safe = re.sub(
        r"(?i)(authorization|x-api-key)\s*[:=]\s*\S+",
        r"\1: [REDACTED]",
        safe,
    )
    return safe.splitlines()[0][:200]
