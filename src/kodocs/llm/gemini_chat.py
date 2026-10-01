"""Paid Gemini grading with application-owned retry, pacing and billing.

Inspected google-genai 2.26.0: types.py (schema, thinking, usage, response_id),
errors.py (APIError.code/details, ClientError/ServerError), _api_client.py
(HttpRetryOptions.attempts=1 disables retries). Quota details are opaque JSON
in this SDK: no quotaId/quotaMetric typed contract or model-specific support
matrix is present. Unrecognized 429 bodies therefore stop conservatively.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from kodocs.config import Settings
from kodocs.costs import (
    DEFAULT_LEDGER_PATH,
    BudgetGuard,
    CostLedger,
    ensure_price_valid,
    estimate_usd,
)
from kodocs.llm.grade_settings import GradeModelConfig


@dataclass(frozen=True)
class GradeRequest:
    item_id: str
    system_prompt: str
    user_content: str
    schema: type[BaseModel]


@dataclass(frozen=True)
class GradeUsage:
    input_tokens: int
    output_tokens: int
    candidates_token_count: int
    thoughts_token_count: int
    total_token_count: int | None


@dataclass(frozen=True)
class GradeAttempt:
    number: int
    status: str
    latency_ms: float
    error_type: str | None = None
    status_code: int | None = None


@dataclass(frozen=True)
class GradeCostEstimate:
    estimated_input_tokens: int
    maximum_output_tokens: int
    maximum_cost_usd: float

    @property
    def budget_estimate_usd(self) -> float:
        return self.maximum_cost_usd


@dataclass(frozen=True)
class GradeResult:
    parsed: BaseModel
    usage: GradeUsage
    cost_usd: float
    latency_ms: float
    attempts: tuple[GradeAttempt, ...]
    request_id: str | None
    model_record: dict[str, object]


class GradeError(RuntimeError):
    """Safe error metadata without provider text or secrets."""

    def __init__(self, message: str, *, attempts=(), status_code=None):
        super().__init__(message)
        self.attempts = tuple(attempts)
        self.status_code = status_code


class GradeParseError(GradeError):
    pass


class BillingUncertainError(GradeError):
    pass


class FatalLLMError(GradeError):
    pass


class GradeStopError(GradeError):
    """Resumable stop; the caller may fill in the remaining cached work count."""

    def __init__(self, message: str, *, remaining: int | None = None, **kwargs):
        super().__init__(message, **kwargs)
        self.remaining = remaining

    def __str__(self) -> str:
        return f"{super().__str__()}; remaining={self.remaining}"


class DailyQuotaExhausted(GradeStopError):
    def __init__(self, **kwargs):
        self.next_reset_kst = next_daily_reset()
        super().__init__(
            f"Daily quota exhausted; next reset KST={self.next_reset_kst.isoformat()}",
            **kwargs,
        )


class CreditExhausted(GradeStopError):
    pass


class UnclassifiedRateLimit(GradeStopError):
    pass


def next_daily_reset(now: datetime | None = None) -> datetime:
    """Next Pacific calendar midnight, converted to KST with DST."""
    pacific = (now or datetime.now(UTC)).astimezone(ZoneInfo("America/Los_Angeles"))
    midnight = (pacific + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return midnight.astimezone(ZoneInfo("Asia/Seoul"))


def create_client(config: GradeModelConfig, api_key: str, **http_options):
    """Disable implicit SDK retries, including retry of post-send timeouts."""
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=config.request_timeout_s * 1000,
            retry_options=types.HttpRetryOptions(attempts=1),
            **http_options,
        ),
    )


def sdk_evidence() -> dict[str, object]:
    """Local evidence; model support and live quota payloads require user probe."""
    return {
        "version": genai.__version__,
        "sources": [
            str(Path(genai.__file__).with_name(name))
            for name in ("types.py", "errors.py", "_api_client.py")
        ],
        "schema": "GenerateContentConfig.response_schema + application/json",
        "thinking": "ThinkingConfig.thinking_level; SDK enum minimum MINIMAL",
        "model_minimum_support": "확인 불가: SDK has no gemini-3.8-flash support matrix",
        "model_document_evidence": "확인 불가: no model-specific documentation bundled",
        "sampling": "GenerateContentConfig.temperature (float), seed (int)",
        "usage_total": "prompt + candidates + tool_use_prompt + thoughts",
        "identity": "response_id (response identifier), model_version; HTTP request ID 확인 불가",
        "errors": "APIError.code/details; 4xx ClientError; 5xx ServerError",
        "quota_structure": "확인 불가: details is opaque response JSON, no typed quota contract",
    }


def _rate_classification(details: object) -> str:
    # Inspect only quota identifiers/metrics and message, not URLs or arbitrary
    # field names. Mixed daily/minute violations must stop, never retry.
    payload = details
    if isinstance(payload, list) and len(payload) == 1:
        payload = payload[0]
    if isinstance(payload, dict):
        payload = payload.get("error", payload)
    if not isinstance(payload, dict):
        return "unclassified"
    clues = [str(payload.get("message", ""))]
    for detail in payload.get("details", []) or []:
        if not isinstance(detail, dict):
            continue
        for violation in detail.get("violations", []) or []:
            if isinstance(violation, dict):
                clues.extend(
                    str(violation.get(key, ""))
                    for key in ("quotaId", "quotaMetric", "description")
                )
    text = " ".join(clues).casefold()
    compact = re.sub(r"[^a-z0-9]", "", text)
    if re.search(
        r"(prepaid|credit|balance).*(exhaust|deplet|insufficient|zero)", text
    ) or re.search(
        r"(exhaust|deplet|insufficient|zero).*(prepaid|credit|balance)", text
    ):
        return "credit"
    if any(
        term in compact for term in ("perday", "dailyquota", "dailylimit")
    ) or re.search(r"\brpd\b", text):
        return "daily"
    if any(term in compact for term in ("perminute", "minutelimit")) or re.search(
        r"\b(?:rpm|tpm)\b", text
    ):
        return "minute"
    return "unclassified"


def _sanitize(value: object, secret: str) -> object:
    """Redact credential fields and URL query secrets, including nested bodies."""
    if isinstance(value, dict):
        return {
            str(key): _sanitize(item, secret)
            for key, item in value.items()
            if str(key).casefold().replace("-", "_")
            not in {
                "headers",
                "authorization",
                "api_key",
                "apikey",
                "key",
                "x_goog_api_key",
            }
        }
    if isinstance(value, list):
        return [_sanitize(item, secret) for item in value]
    if isinstance(value, str):
        safe = value.replace(secret, "[REDACTED]") if secret else value
        return re.sub(
            r"(?i)([?&](?:key|api_key|apikey|x-goog-api-key)=)[^&\s\"']+",
            r"\1[REDACTED]",
            safe,
        )
    return value


class GeminiGrader:
    """Synchronous grading client with no network activity until grade()."""

    def __init__(
        self,
        config: GradeModelConfig,
        settings: Settings,
        *,
        client=None,
        ledger_path: Path = DEFAULT_LEDGER_PATH,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rate_log_path: Path = Path("data/interim/m7/gemini_429.jsonl"),
    ):
        self.config = config
        self._settings = settings
        self._client = client
        self._secret = settings.api_keys.get("GEMINI_API_KEY", "")
        self._ledger = CostLedger(ledger_path)
        self._guard = BudgetGuard(self._ledger, settings.monthly_budget_usd)
        self._clock, self._sleep = clock, sleep
        self._last_start: float | None = None
        self._rate_log_path = rate_log_path
        self.remaining: int | None = None

    def estimate(self, request: GradeRequest) -> GradeCostEstimate:
        prefix = f"google.{self.config.model}"
        for suffix in ("input", "output"):
            ensure_price_valid(f"{prefix}.{suffix}")
        count = (
            len(request.system_prompt)
            + len(request.user_content)
            + len(json.dumps(request.schema.model_json_schema(), ensure_ascii=False))
        )
        return GradeCostEstimate(
            count,
            self.config.max_output_tokens,
            estimate_usd(f"{prefix}.input", count)
            + estimate_usd(f"{prefix}.output", self.config.max_output_tokens),
        )

    def check_budget(self, estimate: GradeCostEstimate) -> None:
        self._guard.check(estimate.maximum_cost_usd)

    def _record(
        self,
        request,
        usage,
        status,
        attempts,
        model_version=None,
        request_id=None,
        latency_ms=0.0,
    ) -> float:
        metadata = {
            "generation": self.config.record(),
            "settings_hash": self.config.settings_hash(),
            "status": status,
            "usage": asdict(usage),
            "attempts": [asdict(attempt) for attempt in attempts],
            "request_id": request_id,
            "model_version": model_version,
            "latency_ms": latency_ms,
        }
        return sum(
            self._ledger.record(
                service=f"google.{self.config.model}.{suffix}",
                model=self.config.model,
                quantity=count,
                ref=request.item_id,
                run_id="gemini-grade",
                tier=self.config.tier,
                metadata=metadata,
            ).amount_usd
            for suffix, count in (
                ("input", usage.input_tokens),
                ("output", usage.output_tokens),
            )
        )

    def grade(self, request: GradeRequest) -> GradeResult:
        estimate = self.estimate(request)
        self.check_budget(estimate)
        if self._client is None:
            self._client = create_client(
                self.config, self._settings.require_key("GEMINI_API_KEY")
            )
        params = {
            "system_instruction": request.system_prompt,
            "response_mime_type": "application/json",
            "response_schema": request.schema,
            "max_output_tokens": self.config.max_output_tokens,
            # Per-request override also prevents retries in an injected SDK client.
            "http_options": types.HttpOptions(
                timeout=self.config.request_timeout_s * 1000,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        }
        if self.config.thinking_level is not None:
            params["thinking_config"] = types.ThinkingConfig(
                thinking_level=self.config.thinking_level
            )
        for name in ("temperature", "seed"):
            value = getattr(self.config, name)
            if value is not None:
                params[name] = value
        started = self._clock()
        attempts = []
        for number in range(1, self.config.max_attempts + 1):
            if self._last_start is not None:
                wait = 60 / self.config.rpm_pacing - (self._clock() - self._last_start)
                if wait > 0:
                    self._sleep(wait)
            # Re-check after pacing, immediately before each send.
            self.check_budget(self.estimate(request))
            self._last_start = self._clock()
            try:
                response = self._client.models.generate_content(
                    model=self.config.model,
                    contents=request.user_content,
                    config=params,
                )
            except (errors.APIError, httpx.TransportError, ValueError) as exc:
                code = exc.code if isinstance(exc, errors.APIError) else None
                classification = None
                if code == 429:
                    classification = _rate_classification(exc.details)
                    self._rate_log_path.parent.mkdir(parents=True, exist_ok=True)
                    with self._rate_log_path.open("a", encoding="utf-8") as target:
                        target.write(
                            json.dumps(
                                {
                                    "ts": datetime.now(UTC).isoformat(),
                                    "status": code,
                                    "body": _sanitize(exc.details, self._secret),
                                    "classification": classification,
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                retryable = (
                    isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
                    or (code is not None and 500 <= code < 600)
                    or classification == "minute"
                )
                uncertain = isinstance(exc, httpx.TransportError) and not retryable
                status = (
                    "billing_uncertain"
                    if uncertain
                    else (
                        "retry"
                        if retryable and number < self.config.max_attempts
                        else "stopped"
                    )
                )
                attempts.append(
                    GradeAttempt(
                        number,
                        status,
                        (self._clock() - self._last_start) * 1000,
                        type(exc).__name__,
                        code,
                    )
                )
                reserved = GradeUsage(
                    estimate.estimated_input_tokens if uncertain else 0,
                    estimate.maximum_output_tokens if uncertain else 0,
                    0,
                    0,
                    None,
                )
                self._record(request, reserved, status, attempts)
                kwargs = {"attempts": attempts, "status_code": code}
                if uncertain:
                    raise BillingUncertainError(
                        "Gemini billing uncertain; conservative cost reserved", **kwargs
                    ) from None
                if classification == "daily":
                    raise DailyQuotaExhausted(
                        remaining=self.remaining, **kwargs
                    ) from None
                if classification == "credit":
                    raise CreditExhausted(
                        "Prepaid credit exhausted", remaining=self.remaining, **kwargs
                    ) from None
                if classification == "unclassified":
                    raise UnclassifiedRateLimit(
                        "Unclassified 429; stopped", remaining=self.remaining, **kwargs
                    ) from None
                if status == "retry":
                    self._sleep(min(2 ** (number - 1), 20))
                    continue
                raise FatalLLMError(
                    "Gemini request failed; not retried", **kwargs
                ) from None
            attempts.append(
                GradeAttempt(
                    number, "success", (self._clock() - self._last_start) * 1000
                )
            )
            latency = (self._clock() - started) * 1000
            raw = response.usage_metadata
            if (
                raw is None
                or raw.prompt_token_count is None
                or raw.candidates_token_count is None
            ):
                usage = GradeUsage(
                    estimate.estimated_input_tokens,
                    estimate.maximum_output_tokens,
                    0,
                    0,
                    None,
                )
                self._record(request, usage, "billing_uncertain", attempts)
                raise BillingUncertainError(
                    "Gemini usage missing; cost reserved", attempts=attempts
                )
            candidates, thoughts = (
                raw.candidates_token_count,
                raw.thoughts_token_count or 0,
            )
            # SDK documents separate thoughts. If a provider version reports
            # an already-inclusive candidate count, total prevents double addition.
            output = candidates + thoughts
            if raw.total_token_count == raw.prompt_token_count + candidates:
                output = candidates
            usage = GradeUsage(
                raw.prompt_token_count,
                output,
                candidates,
                thoughts,
                raw.total_token_count,
            )
            parse_error = False
            try:
                parsed = request.schema.model_validate_json(
                    response.text or "", strict=True
                )
                _validate_scores(parsed.model_dump())
            except (ValidationError, ValueError, TypeError):
                parse_error = True
            cost = self._record(
                request,
                usage,
                "parse_error" if parse_error else "succeeded",
                attempts,
                response.model_version,
                response.response_id,
                latency,
            )
            if parse_error:
                error = GradeParseError(
                    "Gemini structured score invalid", attempts=attempts
                )
                error.usage = usage
                error.model_version = response.model_version
                raise error
            record = self.config.record() | {
                "settings_hash": self.config.settings_hash(),
                "model_version": response.model_version,
            }
            return GradeResult(
                parsed,
                usage,
                cost,
                latency,
                tuple(attempts),
                response.response_id,
                record,
            )
        raise AssertionError("unreachable")


def _validate_scores(value: object) -> None:
    """Grade schemas contain integer score leaves and optional text reasons."""
    if isinstance(value, dict):
        for item in value.values():
            _validate_scores(item)
    elif isinstance(value, list):
        for item in value:
            _validate_scores(item)
    elif isinstance(value, (int, float)) and (
        isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3
    ):
        raise ValueError("Scores must be integers in [0, 3]")
