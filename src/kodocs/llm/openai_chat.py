"""Managed OpenAI Responses API calls with explicit retry and billing policy."""

from __future__ import annotations

import json
import math
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import openai
from langchain_openai import ChatOpenAI

from kodocs.config import Settings
from kodocs.costs import (
    DEFAULT_LEDGER_PATH,
    BudgetGuard,
    CostLedger,
    budget_estimate_usd,
    estimate_usd,
)
from kodocs.llm.answer_settings import AnswerModelConfig


@dataclass(frozen=True)
class ChatUsage:
    """Billable usage; ``input_tokens`` excludes both cache categories."""

    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cache_write_tokens_present: bool

    @property
    def total_input_tokens(self) -> int:
        """Return the provider's total input-token count."""
        return self.input_tokens + self.cached_input_tokens + self.cache_write_tokens


@dataclass(frozen=True)
class ChatAttempt:
    """One application-controlled attempt and its elapsed wall time."""

    number: int
    status: str
    latency_ms: float
    error_type: str | None


@dataclass(frozen=True)
class ChatCostEstimate:
    """Conservative pre-call estimate using one token per input character."""

    estimated_input_tokens: int
    maximum_output_tokens: int
    maximum_cost_usd: float
    budget_estimate_usd: float


@dataclass(frozen=True)
class ChatResult:
    """One successful model response with usage, timing, and billing metadata."""

    message: object
    parsed: object | None
    usage: ChatUsage | None
    cost_usd: float
    latency_ms: float
    ttft_ms: float | None
    attempts: tuple[ChatAttempt, ...]
    request_id: str | None
    model: str
    settings_hash: str
    generation: Mapping[str, object]


class ManagedChatError(RuntimeError):
    """Base class for safe managed-client failures."""

    def __init__(self, message: str, attempts: Sequence[ChatAttempt] = ()) -> None:
        super().__init__(message)
        self.attempts = tuple(attempts)


class ChatParseError(ManagedChatError):
    """Raised when a billed structured response does not match its schema."""


class BillingUncertainError(ManagedChatError):
    """Raised for failures that may have happened after the request was sent."""


class FatalLLMError(ManagedChatError):
    """Raised when the whole evaluation run must stop."""


class InputTokenLimitError(ManagedChatError):
    """Raised when estimated or observed input exceeds the configured ceiling."""


class ManagedChat:
    """Own ChatOpenAI construction, retries, usage parsing, and cost recording."""

    def __init__(
        self,
        config: AnswerModelConfig,
        settings: Settings,
        *,
        http_client: object | None = None,
        ledger_path: Path = DEFAULT_LEDGER_PATH,
        run_id: str = "managed-chat",
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.config = config
        self._ledger = CostLedger(ledger_path)
        self._guard = BudgetGuard(self._ledger, settings.monthly_budget_usd)
        self._run_id = run_id
        self._sleep = sleep
        self._monotonic = monotonic
        self._model = ChatOpenAI(
            model=config.model,
            api_key=settings.require_key("OPENAI_API_KEY"),
            use_responses_api=True,
            reasoning={"effort": config.effort},
            max_completion_tokens=config.max_output_tokens,
            max_retries=0,
            timeout=config.request_timeout_s,
            include_response_headers=True,
            **({"http_client": http_client} if http_client is not None else {}),
        )

    @property
    def max_retries(self) -> int | None:
        """Expose the SDK retry setting for contract checks and traces."""
        return self._model.max_retries

    def estimate(
        self,
        messages: object,
        *,
        tools: Sequence[object] | None = None,
        response_schema: type[object] | None = None,
    ) -> ChatCostEstimate:
        """Return worst-case cost, rejecting input above the standard price tier."""
        input_tokens = _estimate_input_tokens(messages, tools, response_schema)
        self._check_input_limit(input_tokens, observed=False)
        prefix = f"openai.{self.config.model}"
        maximum_cost = estimate_usd(f"{prefix}.input", input_tokens) + estimate_usd(
            f"{prefix}.output", self.config.max_output_tokens
        )
        guarded_cost = budget_estimate_usd(
            f"{prefix}.input", input_tokens
        ) + budget_estimate_usd(f"{prefix}.output", self.config.max_output_tokens)
        return ChatCostEstimate(
            estimated_input_tokens=input_tokens,
            maximum_output_tokens=self.config.max_output_tokens,
            maximum_cost_usd=maximum_cost,
            budget_estimate_usd=guarded_cost,
        )

    def dry_run(
        self,
        messages: object,
        *,
        tools: Sequence[object] | None = None,
        response_schema: type[object] | None = None,
    ) -> ChatCostEstimate:
        """Alias for a no-network preflight estimate."""
        return self.estimate(
            messages,
            tools=tools,
            response_schema=response_schema,
        )

    def invoke(
        self,
        messages: object,
        *,
        tools: Sequence[object] | None = None,
        response_schema: type[object] | None = None,
        stream: bool = False,
    ) -> ChatResult:
        """Invoke the Responses API under the registered retry and billing rules."""
        estimate = self.estimate(
            messages,
            tools=tools,
            response_schema=response_schema,
        )
        self._guard.check(estimate.budget_estimate_usd)

        attempts: list[ChatAttempt] = []
        overall_started = self._monotonic()
        for number in range(1, self.config.max_attempts + 1):
            attempt_started = self._monotonic()
            try:
                message, parsed, parsing_error, ttft_ms = self._invoke_once(
                    messages,
                    tools=tools,
                    response_schema=response_schema,
                    stream=stream,
                    overall_started=overall_started,
                )
            except Exception as exc:
                elapsed_ms = _elapsed_ms(attempt_started, self._monotonic())
                action = _classify_error(exc, number, self.config.max_attempts)
                attempt = ChatAttempt(
                    number=number,
                    status=action,
                    latency_ms=elapsed_ms,
                    error_type=type(exc).__name__,
                )
                attempts.append(attempt)
                if action == "retry":
                    self._sleep(min(2.0 ** (number - 1), 20.0))
                    continue
                if action == "billing_uncertain":
                    raise BillingUncertainError(
                        "OpenAI request outcome is billing-uncertain; not retried",
                        attempts,
                    ) from exc
                if action == "parse_error":
                    raise ChatParseError(
                        "OpenAI structured response could not be parsed",
                        attempts,
                    ) from exc
                raise FatalLLMError(
                    "OpenAI request failed with a non-retryable error",
                    attempts,
                ) from exc

            attempts.append(
                ChatAttempt(
                    number=number,
                    status="success",
                    latency_ms=_elapsed_ms(attempt_started, self._monotonic()),
                    error_type=None,
                )
            )
            latency_ms = _elapsed_ms(overall_started, self._monotonic())
            usage = _usage_from_message(message)
            request_id = _request_id(message)
            status = (
                "input_limit_exceeded"
                if usage is not None
                and usage.total_input_tokens > self.config.max_input_tokens
                else "succeeded"
            )
            cost_usd = self._record_usage(
                usage,
                message=message,
                request_id=request_id,
                attempts=attempts,
                latency_ms=latency_ms,
                status=status,
            )
            if status == "input_limit_exceeded":
                raise InputTokenLimitError(
                    "Observed OpenAI input usage exceeds the configured "
                    f"{self.config.max_input_tokens} token limit",
                    attempts,
                )
            if parsing_error is not None:
                raise ChatParseError(
                    "OpenAI structured response could not be parsed",
                    attempts,
                ) from parsing_error
            return ChatResult(
                message=message,
                parsed=parsed,
                usage=usage,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                ttft_ms=ttft_ms,
                attempts=tuple(attempts),
                request_id=request_id,
                model=_response_model(message) or self.config.model,
                settings_hash=self.config.settings_hash(),
                generation=self.config.record(),
            )

        raise AssertionError("unreachable retry loop")

    def _invoke_once(
        self,
        messages: object,
        *,
        tools: Sequence[object] | None,
        response_schema: type[object] | None,
        stream: bool,
        overall_started: float,
    ) -> tuple[object, object | None, BaseException | None, float | None]:
        if stream:
            runnable: Any = self._model
            if tools:
                runnable = self._model.bind_tools(tools)
            kwargs = (
                {"response_format": response_schema}
                if response_schema is not None
                else {}
            )
            combined = None
            ttft_ms = None
            for chunk in runnable.stream(messages, **kwargs):
                if ttft_ms is None:
                    ttft_ms = _elapsed_ms(overall_started, self._monotonic())
                combined = chunk if combined is None else combined + chunk
            if combined is None:
                raise ValueError("OpenAI stream returned no chunks")
            parsed = getattr(combined, "additional_kwargs", {}).get("parsed")
            if response_schema is not None and parsed is None:
                raise ChatParseError("structured stream did not contain parsed output")
            return combined, parsed, None, ttft_ms

        if response_schema is not None:
            structured = self._model.with_structured_output(
                response_schema,
                method="json_schema",
                include_raw=True,
                strict=True,
                tools=list(tools) if tools else None,
            )
            response = structured.invoke(messages)
            if not isinstance(response, Mapping):
                raise TypeError("structured ChatOpenAI result must be a mapping")
            return (
                response.get("raw"),
                response.get("parsed"),
                response.get("parsing_error"),
                None,
            )

        runnable = self._model.bind_tools(tools) if tools else self._model
        return runnable.invoke(messages), None, None, None

    def _check_input_limit(self, tokens: int, *, observed: bool) -> None:
        if tokens > self.config.max_input_tokens:
            source = "Observed" if observed else "Estimated"
            raise InputTokenLimitError(
                f"{source} OpenAI input exceeds the configured "
                f"{self.config.max_input_tokens} token limit"
            )

    def _record_usage(
        self,
        usage: ChatUsage | None,
        *,
        message: object,
        request_id: str | None,
        attempts: Sequence[ChatAttempt],
        latency_ms: float,
        status: str,
    ) -> float:
        if usage is None:
            return 0.0
        quantities = (
            ("input", usage.input_tokens),
            ("cached_input", usage.cached_input_tokens),
            ("cache_write", usage.cache_write_tokens),
            ("output", usage.output_tokens),
        )
        metadata: dict[str, object] = {
            "attempts": [asdict(attempt) for attempt in attempts],
            "cache_write_tokens_present": usage.cache_write_tokens_present,
            "generation": self.config.record(),
            "latency_ms": latency_ms,
            "reasoning_tokens": usage.reasoning_tokens,
            "request_id": request_id,
            "settings_hash": self.config.settings_hash(),
            "status": status,
            "usage": asdict(usage),
        }
        ref = request_id or _response_id(message) or uuid.uuid4().hex
        model = _response_model(message) or self.config.model
        costs = []
        for suffix, quantity in quantities:
            entry = self._ledger.record(
                service=f"openai.{self.config.model}.{suffix}",
                model=model,
                quantity=quantity,
                ref=ref,
                run_id=self._run_id,
                metadata=metadata,
            )
            costs.append(entry.amount_usd)
        return math.fsum(costs)


def _estimate_input_tokens(
    messages: object,
    tools: Sequence[object] | None,
    response_schema: type[object] | None,
) -> int:
    count = _message_characters(messages)
    if tools:
        count += len(json.dumps(tools, ensure_ascii=False, default=str))
    if response_schema is not None:
        schema_method = getattr(response_schema, "model_json_schema", None)
        schema = schema_method() if callable(schema_method) else response_schema
        count += len(json.dumps(schema, ensure_ascii=False, default=str))
    return count


def _message_characters(value: object) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, Mapping):
        if "content" in value:
            return _message_characters(value["content"])
        return sum(_message_characters(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        if (
            len(value) == 2
            and isinstance(value[0], str)
            and value[0] in {"system", "developer", "user", "assistant", "tool"}
        ):
            return _message_characters(value[1])
        return sum(_message_characters(item) for item in value)
    content = getattr(value, "content", None)
    return _message_characters(content) if content is not None else len(str(value))


def _usage_from_message(message: object) -> ChatUsage | None:
    raw = getattr(message, "usage_metadata", None)
    if not isinstance(raw, Mapping):
        return None
    total_input = _non_negative_int(raw.get("input_tokens", 0), "input_tokens")
    output = _non_negative_int(raw.get("output_tokens", 0), "output_tokens")
    input_details = raw.get("input_token_details") or {}
    output_details = raw.get("output_token_details") or {}
    if not isinstance(input_details, Mapping) or not isinstance(
        output_details, Mapping
    ):
        raise TypeError("OpenAI usage token details must be mappings")
    cached = _non_negative_int(input_details.get("cache_read", 0), "cache_read")
    cache_write_present = "cache_creation" in input_details
    cache_write = _non_negative_int(
        input_details.get("cache_creation", 0), "cache_creation"
    )
    reasoning = _non_negative_int(output_details.get("reasoning", 0), "reasoning")
    regular_input = total_input - cached - cache_write
    if regular_input < 0:
        raise ValueError("OpenAI cache usage exceeds total input tokens")
    # The installed SDK defines reasoning_tokens as a breakdown of output_tokens,
    # and langchain-openai subtracts it only when splitting service tiers. Therefore
    # output_tokens already includes reasoning and is billed exactly once here.
    return ChatUsage(
        input_tokens=regular_input,
        cached_input_tokens=cached,
        cache_write_tokens=cache_write,
        output_tokens=output,
        reasoning_tokens=reasoning,
        cache_write_tokens_present=cache_write_present,
    )


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"OpenAI usage {name} must be a non-negative integer")
    return value


def _classify_error(exc: BaseException, attempt: int, maximum: int) -> str:
    if isinstance(exc, ChatParseError) or _is_pydantic_validation_error(exc):
        return "parse_error"
    status = _status_code(exc)
    if status in {401, 402, 403} or _is_quota_error(exc):
        return "fatal"
    if status == 429:
        return "retry" if attempt < maximum else "fatal"
    if status is not None and 500 <= status <= 599:
        return "retry" if attempt < maximum else "fatal"

    chain_names = {type(item).__name__ for item in _exception_chain(exc)}
    if chain_names.intersection({"ConnectError", "ConnectTimeout"}):
        return "retry" if attempt < maximum else "fatal"
    if chain_names.intersection(
        {
            "ReadTimeout",
            "WriteTimeout",
            "PoolTimeout",
            "ProtocolError",
            "RemoteProtocolError",
            "LocalProtocolError",
        }
    ):
        return "billing_uncertain"
    if isinstance(exc, openai.APIConnectionError | openai.APITimeoutError):
        return "billing_uncertain"
    return "fatal"


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _status_code(exc: BaseException) -> int | None:
    for item in _exception_chain(exc):
        status = getattr(item, "status_code", None)
        if isinstance(status, int):
            return status
        response = getattr(item, "response", None)
        response_status = getattr(response, "status_code", None)
        if isinstance(response_status, int):
            return response_status
    return None


def _is_quota_error(exc: BaseException) -> bool:
    quota_codes = {
        "billing_hard_limit_reached",
        "insufficient_quota",
        "usage_limit_reached",
    }
    for item in _exception_chain(exc):
        body = getattr(item, "body", None)
        if isinstance(body, Mapping):
            error = body.get("error", body)
            if isinstance(error, Mapping) and error.get("code") in quota_codes:
                return True
    return False


def _is_pydantic_validation_error(exc: BaseException) -> bool:
    return type(exc).__name__ == "ValidationError" and type(exc).__module__.startswith(
        "pydantic"
    )


def _request_id(message: object) -> str | None:
    metadata = getattr(message, "response_metadata", None)
    if not isinstance(metadata, Mapping):
        return None
    headers = metadata.get("headers")
    if isinstance(headers, Mapping):
        for name, value in headers.items():
            if str(name).casefold() == "x-request-id" and isinstance(value, str):
                return value
    return None


def _response_id(message: object) -> str | None:
    metadata = getattr(message, "response_metadata", None)
    if isinstance(metadata, Mapping) and isinstance(metadata.get("id"), str):
        return metadata["id"]
    identifier = getattr(message, "id", None)
    return identifier if isinstance(identifier, str) else None


def _response_model(message: object) -> str | None:
    metadata = getattr(message, "response_metadata", None)
    if not isinstance(metadata, Mapping):
        return None
    for name in ("model_name", "model"):
        value = metadata.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _elapsed_ms(started: float, ended: float) -> float:
    return max(0.0, (ended - started) * 1000)
