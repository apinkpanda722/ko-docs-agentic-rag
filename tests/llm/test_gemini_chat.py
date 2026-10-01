"""Offline Gemini contracts using installed SDK exceptions and mock transport."""

import json
from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from google.genai import errors, types
from pydantic import BaseModel, Field

from kodocs.config import Settings
from kodocs.costs import BudgetExceededError, PriceExpiredError, ensure_price_valid
from kodocs.llm.gemini_chat import (
    BillingUncertainError,
    CreditExhausted,
    DailyQuotaExhausted,
    FatalLLMError,
    GeminiGrader,
    GradeParseError,
    GradeRequest,
    UnclassifiedRateLimit,
    next_daily_reset,
)
from kodocs.llm.grade_settings import ConfigError, load_grade_config

SECRET = "gemini-test-secret"


class Score(BaseModel):
    score: int = Field(ge=0, le=3, strict=True)


REQUEST = GradeRequest("item", "Return JSON scores", "dummy", Score)


def quota_error(metric="", message="quota exceeded", status=429):
    return errors.ClientError(
        status,
        {
            "error": {
                "code": status,
                "status": "RESOURCE_EXHAUSTED",
                "message": message,
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": metric, "quotaMetric": metric}],
                    }
                ],
            }
        },
    )


def response(text='{"score":2}'):
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(content=types.Content(parts=[types.Part(text=text)]))
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=100,
            candidates_token_count=20,
            thoughts_token_count=30,
            total_token_count=150,
        ),
        response_id="response-test",
        model_version="gemini-test-version",
    )


def grader(tmp_path, events, *, budget=45, config=None):
    class Fake:
        def __init__(self):
            self.models = self
            self.calls = []
            self.events = iter(events)

        def generate_content(self, **kwargs):
            self.calls.append(kwargs)
            event = next(self.events)
            if isinstance(event, Exception):
                raise event
            return event

    fake = Fake()
    ticks = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        ticks[0] += seconds

    managed = GeminiGrader(
        config or load_grade_config(),
        Settings({"GEMINI_API_KEY": SECRET}, budget),
        client=fake,
        ledger_path=tmp_path / "costs.jsonl",
        rate_log_path=tmp_path / "429.jsonl",
        clock=lambda: ticks[0],
        sleep=sleep,
    )
    return managed, fake, sleeps


def test_config_defaults_and_validation():
    config = load_grade_config()
    assert config.model == "gemini-3.8-flash"
    assert config.tier == "paid"
    assert config.rpm_pacing == 10
    assert config.rpm is config.tpm is config.rpd is None
    assert config.thinking_level is config.temperature is config.seed is None
    assert config.record()["seed"] is None
    assert replace(config, seed=1).settings_hash() != config.settings_hash()
    with pytest.raises(ConfigError):
        replace(config, model="")
    for values in (
        {"rpm_pacing": 0},
        {"seed": True},
        {"temperature": float("nan")},
        {"thinking_level": "invented"},
        {"tier": "free"},
    ):
        with pytest.raises(ConfigError):
            replace(config, **values)


def test_expired_price_and_vat():
    ensure_price_valid("google.gemini-3.8-flash.input", on=date(2026, 12, 31))
    with pytest.raises(PriceExpiredError):
        ensure_price_valid("google.gemini-3.8-flash.output", on=date(2027, 1, 1))


def test_success_usage_ledger_and_null_omission(tmp_path):
    managed, fake, _ = grader(tmp_path, [response()])
    result = managed.grade(REQUEST)
    assert result.parsed == Score(score=2)
    assert result.usage.output_tokens == 50
    assert result.cost_usd == pytest.approx((100 * 0.75e-6 + 50 * 3.75e-6) * 1.1)
    assert result.request_id == "response-test"
    assert result.model_record["model_version"] == "gemini-test-version"
    params = fake.calls[0]["config"]
    assert "temperature" not in params and "seed" not in params
    assert "thinking_config" not in params
    assert params["response_schema"] is Score
    lines = [
        json.loads(line) for line in (tmp_path / "costs.jsonl").read_text().splitlines()
    ]
    assert [line["quantity"] for line in lines] == [100, 50]
    assert all(line["tier"] == "paid" for line in lines)
    assert all(
        line["settings_hash"] == managed.config.settings_hash() for line in lines
    )


@pytest.mark.parametrize(
    "metric",
    [
        "GenerateRequestsPerMinutePerProjectPerModel",
        "generate_content_input_tokens_per_minute",
    ],
)
def test_minute_quota_retries_and_paces(tmp_path, metric):
    managed, fake, sleeps = grader(
        tmp_path, [quota_error(metric), response(), response()]
    )
    result = managed.grade(REQUEST)
    managed.grade(REQUEST)
    assert len(result.attempts) == 2
    assert sleeps == [1, 5, 6]
    assert len(fake.calls) == 3


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (quota_error("GenerateRequestsPerDayPerProjectPerModel"), DailyQuotaExhausted),
        (quota_error(message="Prepaid credit balance exhausted"), CreditExhausted),
        (quota_error(), UnclassifiedRateLimit),
        (
            quota_error("PerMinute", message="daily quota exhausted"),
            DailyQuotaExhausted,
        ),
    ],
)
def test_stop_errors_never_retry_and_have_remaining(tmp_path, error, expected):
    managed, fake, sleeps = grader(tmp_path, [error])
    with pytest.raises(expected) as caught:
        managed.grade(REQUEST)
    caught.value.remaining = 12
    assert "12" in str(caught.value)
    if expected is DailyQuotaExhausted:
        assert "+09:00" in str(caught.value)
    assert len(fake.calls) == 1
    assert not sleeps


@pytest.mark.parametrize(("month", "hour"), [(1, 17), (7, 16)])
def test_reset_observes_pacific_dst(month, hour):
    reset = next_daily_reset(
        datetime(2026, month, 1, 12, tzinfo=ZoneInfo("Asia/Seoul"))
    )
    assert reset.hour == hour
    assert reset.tzinfo == ZoneInfo("Asia/Seoul")


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("secret"),
        httpx.WriteTimeout("secret"),
        httpx.RemoteProtocolError("secret"),
    ],
)
def test_post_send_failure_records_uncertainty(tmp_path, error):
    managed, fake, _ = grader(tmp_path, [error])
    with pytest.raises(BillingUncertainError):
        managed.grade(REQUEST)
    assert len(fake.calls) == 1
    lines = [
        json.loads(line) for line in (tmp_path / "costs.jsonl").read_text().splitlines()
    ]
    assert all(line["status"] == "billing_uncertain" for line in lines)
    assert sum(line["amount_usd"] for line in lines) > 0


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("failed"),
        httpx.ConnectTimeout("failed"),
        errors.ServerError(503, {"error": {"message": "failed"}}),
    ],
)
def test_safe_failure_retries(tmp_path, error):
    managed, fake, _ = grader(tmp_path, [error, response()])
    managed.grade(REQUEST)
    assert len(fake.calls) == 2


@pytest.mark.parametrize("status", [401, 402, 403])
def test_fatal_error_redacts_message(tmp_path, status):
    managed, fake, _ = grader(tmp_path, [quota_error(message=SECRET, status=status)])
    with pytest.raises(FatalLLMError) as caught:
        managed.grade(REQUEST)
    assert SECRET not in str(caught.value)
    assert caught.value.status_code == status
    assert len(fake.calls) == 1


@pytest.mark.parametrize("text", ["bad json", '{"score":4}', '{"score":true}'])
def test_parse_failure_billed_without_retry(tmp_path, text):
    managed, fake, _ = grader(tmp_path, [response(text)])
    with pytest.raises(GradeParseError):
        managed.grade(REQUEST)
    assert len(fake.calls) == 1
    assert (tmp_path / "costs.jsonl").exists()


def test_budget_rejects_before_call(tmp_path):
    managed, fake, _ = grader(tmp_path, [], budget=0)
    with pytest.raises(BudgetExceededError):
        managed.grade(REQUEST)
    assert not fake.calls


def test_429_log_sanitizes_secrets_headers_and_urls(tmp_path):
    error = quota_error(
        message=f"https://example.test?key={SECRET}&api_key=other-secret"
    )
    error.details["headers"] = {"authorization": "another-secret"}
    managed, _, _ = grader(tmp_path, [error])
    with pytest.raises(UnclassifiedRateLimit):
        managed.grade(REQUEST)
    log = (tmp_path / "429.jsonl").read_text()
    assert (
        SECRET not in log and "other-secret" not in log and "another-secret" not in log
    )


def test_actual_sdk_transport_disables_retries(tmp_path):
    from kodocs.llm.gemini_chat import create_client

    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": '{"score":2}'}]}}],
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 20,
                    "thoughtsTokenCount": 30,
                    "totalTokenCount": 150,
                },
                "responseId": "test",
                "modelVersion": "test-version",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        config = replace(
            load_grade_config(), thinking_level="minimal", temperature=0.0, seed=42
        )
        client = create_client(config, SECRET, httpx_client=transport)
        managed = GeminiGrader(
            config,
            Settings({"GEMINI_API_KEY": SECRET}, 45),
            client=client,
            ledger_path=tmp_path / "costs.jsonl",
        )
        result = managed.grade(REQUEST)
    assert result.parsed.score == 2
    assert calls[0]["generationConfig"]["thinkingConfig"]["thinking_level"] == "MINIMAL"
    assert calls[0]["generationConfig"]["seed"] == 42


def test_expired_price_blocks_estimate_and_call(tmp_path, monkeypatch):
    import kodocs.llm.gemini_chat as module

    def expired(service):
        ensure_price_valid(service, on=date(2027, 1, 1))

    monkeypatch.setattr(module, "ensure_price_valid", expired)
    managed, fake, _ = grader(tmp_path, [])
    with pytest.raises(PriceExpiredError):
        managed.estimate(REQUEST)
    with pytest.raises(PriceExpiredError):
        managed.grade(REQUEST)
    assert not fake.calls


def test_inclusive_candidate_usage_does_not_add_thinking_twice(tmp_path):
    event = response()
    event.usage_metadata.candidates_token_count = 50
    managed, _, _ = grader(tmp_path, [event])
    assert managed.grade(REQUEST).usage.output_tokens == 50


@pytest.mark.parametrize("kind", ["daily", "timeout"])
def test_sdk_never_retries_stops_or_read_timeout(tmp_path, kind):
    from kodocs.llm.gemini_chat import create_client

    calls = []

    def handler(request):
        calls.append(request)
        if kind == "timeout":
            raise httpx.ReadTimeout("safe", request=request)
        return httpx.Response(429, json=quota_error("GenerateRequestsPerDay").details)

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        config = load_grade_config()
        managed = GeminiGrader(
            config,
            Settings({"GEMINI_API_KEY": SECRET}, 45),
            client=create_client(config, SECRET, httpx_client=transport),
            ledger_path=tmp_path / "costs.jsonl",
            rate_log_path=tmp_path / "429.jsonl",
        )
        with pytest.raises(
            DailyQuotaExhausted if kind == "daily" else BillingUncertainError
        ):
            managed.grade(REQUEST)
    assert len(calls) == 1


def test_unconstrained_schema_still_enforces_score_range(tmp_path):
    class Unconstrained(BaseModel):
        accuracy: int

    managed, fake, _ = grader(tmp_path, [response('{"accuracy":9}')])
    with pytest.raises(GradeParseError):
        managed.grade(replace(REQUEST, schema=Unconstrained))
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    "message", ["Insufficient prepaid credits", "Credit balance is zero"]
)
def test_credit_word_order_is_classified(tmp_path, message):
    managed, fake, _ = grader(tmp_path, [quota_error(message=message)])
    with pytest.raises(CreditExhausted):
        managed.grade(REQUEST)
    assert len(fake.calls) == 1
