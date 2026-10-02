"""Offline Gemini contracts using installed SDK exceptions and mock transport."""

import json
from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from google.genai import errors, types
from pydantic import BaseModel, ConfigDict, Field

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
    transmitted_response_schema,
    transmitted_schema_sha256,
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
    # Confirmed 2026-10-01: thinking low (documented minimum); temperature/seed never sent.
    assert config.thinking_level == "low"
    assert config.temperature is config.seed is None
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
    assert params["thinking_config"].thinking_level.value == "LOW"
    assert params["response_schema"] == transmitted_response_schema(Score)
    assert params["automatic_function_calling"].disable is True
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


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score: int = Field(ge=0, le=3, strict=True)
    rationale: str = Field(max_length=200)


def test_transmitted_schema_drops_additional_properties_everywhere():
    assert Strict.model_json_schema()["additionalProperties"] is False
    sent = transmitted_response_schema(Strict)
    assert "additionalProperties" not in json.dumps(sent)
    nested = {"a": {"additionalProperties": False, "b": [{"additionalProperties": {}}]}}
    from kodocs.llm.gemini_chat import _strip_schema_keys

    assert _strip_schema_keys(nested) == {"a": {"b": [{}]}}
    # The schema contract for the model is unchanged (strict local validation).
    assert (
        Strict.model_validate_json('{"score":3,"rationale":"x"}', strict=True).score
        == 3
    )
    with pytest.raises(ValueError):
        Strict.model_validate_json('{"score":3,"rationale":"x","extra":1}', strict=True)
    assert transmitted_schema_sha256(Strict) == transmitted_schema_sha256(Strict)


def test_grade_schemas_only_use_keys_the_sdk_schema_accepts():
    from kodocs.eval.m7_grading import AccGrade, FaithGrade

    allowed = {f.alias or name for name, f in types.Schema.model_fields.items()}

    def keys(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield key
                if key == "properties":  # property names are data, not schema keys
                    for sub in item.values():
                        yield from keys(sub)
                else:
                    yield from keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from keys(item)

    for schema in (AccGrade, FaithGrade):
        sent = transmitted_response_schema(schema)
        assert set(keys(sent)) <= allowed, set(keys(sent)) - allowed


def test_wire_request_has_no_additional_properties_and_error_is_sanitized(tmp_path):
    from google import genai

    seen = {}
    secret_body = "참조 답변 원문을 길게 적은 요청 본문 문장입니다"
    request = GradeRequest("item", "시스템 지시문입니다 길게", secret_body, Strict)

    def handler(http_request):
        seen["body"] = http_request.content.decode()
        message = f"Invalid payload {SECRET} {secret_body} " + "x" * 800
        return httpx.Response(
            400,
            json={
                "error": {"code": 400, "status": "INVALID_ARGUMENT", "message": message}
            },
        )

    client = genai.Client(
        api_key=SECRET,
        http_options=types.HttpOptions(
            httpx_client=httpx.Client(transport=httpx.MockTransport(handler))
        ),
    )
    managed = GeminiGrader(
        load_grade_config(),
        Settings({"GEMINI_API_KEY": SECRET}, 45),
        client=client,
        ledger_path=tmp_path / "costs.jsonl",
        rate_log_path=tmp_path / "429.jsonl",
    )
    with pytest.raises(FatalLLMError) as caught:
        managed.grade(request)
    assert "additionalProperties" not in seen["body"]
    assert "AutomaticFunctionCalling" not in seen["body"]
    message = caught.value.error_message
    assert message and len(message) <= 500
    assert "INVALID_ARGUMENT" in message or "Invalid payload" in message
    assert SECRET not in message and secret_body not in message
    assert caught.value.status_code == 400
    ledger = (tmp_path / "costs.jsonl").read_text()
    assert SECRET not in ledger and secret_body not in ledger
    recorded = json.loads(ledger.splitlines()[0])
    assert recorded["attempts"][0]["error_message"] == message


def test_local_validation_stays_strict_after_schema_stripping(tmp_path):
    managed, _, _ = grader(
        tmp_path,
        [response('{"score":3,"rationale":"ok","extra":1}')],
    )
    with pytest.raises(GradeParseError):
        managed.grade(GradeRequest("item", "Return JSON scores", "dummy", Strict))
