"""Mocked transport tests for the managed OpenAI answer client."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from kodocs.config import Settings
from kodocs.costs import BudgetExceededError
from kodocs.llm.answer_settings import load_answer_config
from kodocs.llm.openai_chat import (
    BillingUncertainError,
    ChatParseError,
    FatalLLMError,
    InfrastructureLLMError,
    InputTokenLimitError,
    ManagedChat,
)

SECRET = "sk-test-not-real"


def _response(
    *,
    model: str = "gpt-6-luna",
    text: str = "완료",
    input_tokens: int = 100,
    cached_input_tokens: int = 20,
    cache_write_tokens: int | None = 10,
    output_tokens: int = 40,
    reasoning_tokens: int = 30,
) -> dict[str, object]:
    input_details: dict[str, int] = {"cached_tokens": cached_input_tokens}
    if cache_write_tokens is not None:
        input_details["cache_write_tokens"] = cache_write_tokens
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 1_799_000_000,
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "model": model,
        "output": [
            {
                "id": "msg_test",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "annotations": [],
                        "logprobs": [],
                        "text": text,
                    }
                ],
            }
        ],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": input_tokens,
            "input_tokens_details": input_details,
            "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
            "total_tokens": input_tokens + output_tokens,
        },
    }


def _settings(*, budget: float = 45.0) -> Settings:
    return Settings(
        api_keys={"OPENAI_API_KEY": SECRET},
        monthly_budget_usd=budget,
    )


def _managed(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    model_name: str = "luna",
    budget: float = 45.0,
) -> tuple[ManagedChat, httpx.Client]:
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    managed = ManagedChat(
        load_answer_config(model_name),
        _settings(budget=budget),
        http_client=http_client,
        ledger_path=tmp_path / "costs.jsonl",
        run_id="test-run",
        sleep=lambda _seconds: None,
    )
    return managed, http_client


def _error(status_code: int, code: str) -> httpx.Response:
    return httpx.Response(
        status_code,
        json={
            "error": {
                "message": "safe test error",
                "type": "request_error",
                "param": None,
                "code": code,
            }
        },
    )


def test_actual_responses_request_omits_sampling_and_disables_sdk_retry(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_response(),
            headers={"x-request-id": "req_test"},
        )

    managed, http_client = _managed(tmp_path, handler)
    try:
        result = managed.invoke([{"role": "user", "content": "안녕하세요"}])
    finally:
        http_client.close()

    body = json.loads(requests[0].content)
    assert requests[0].url.path == "/v1/responses"
    assert body["reasoning"] == {"effort": "low"}
    assert body["max_output_tokens"] == 4000
    assert "temperature" not in body
    assert "top_p" not in body
    assert managed.max_retries == 0
    assert result.request_id == "req_test"
    assert result.usage is not None
    assert result.usage.input_tokens == 70
    assert result.usage.cached_input_tokens == 20
    assert result.usage.cache_write_tokens == 10
    assert result.usage.output_tokens == 40
    assert result.usage.reasoning_tokens == 30
    assert result.usage.cache_write_tokens_present is True
    assert result.ttft_ms is None


def test_stream_uses_terminal_responses_usage_and_records_ttft(tmp_path: Path) -> None:
    full_response = _response()
    created_response = {
        **full_response,
        "output": [],
        "status": "in_progress",
        "usage": None,
    }
    events = [
        {
            "type": "response.created",
            "response": created_response,
            "sequence_number": 0,
        },
        {
            "type": "response.output_text.delta",
            "content_index": 0,
            "delta": "완료",
            "item_id": "msg_test",
            "logprobs": [],
            "output_index": 0,
            "sequence_number": 1,
        },
        {
            "type": "response.completed",
            "response": full_response,
            "sequence_number": 2,
        },
    ]
    stream_body = (
        "".join(
            f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
        )
        + "data: [DONE]\n\n"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=stream_body,
            headers={
                "content-type": "text/event-stream",
                "x-request-id": "req_stream",
            },
        )

    managed, http_client = _managed(tmp_path, handler)
    try:
        result = managed.invoke("질문", stream=True)
    finally:
        http_client.close()

    assert result.ttft_ms is not None
    assert result.usage is not None
    assert result.usage.total_input_tokens == 100
    assert result.usage.output_tokens == 40
    assert result.request_id == "req_stream"


def test_tool_calls_are_preserved_on_the_response_message(tmp_path: Path) -> None:
    response = _response(text="")
    response["output"] = [
        {
            "type": "function_call",
            "id": "fc_test",
            "call_id": "call_test",
            "name": "lookup_id",
            "arguments": '{"value":"PAY-1234"}',
            "status": "completed",
        }
    ]
    tool = {
        "type": "function",
        "function": {
            "name": "lookup_id",
            "description": "식별자를 찾는다.",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    managed, http_client = _managed(tmp_path, handler)
    try:
        result = managed.invoke("질문", tools=[tool])
    finally:
        http_client.close()

    assert result.message.tool_calls == [
        {
            "name": "lookup_id",
            "args": {"value": "PAY-1234"},
            "id": "call_test",
            "type": "tool_call",
        }
    ]


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_retryable_status_then_success(
    tmp_path: Path,
    status_code: int,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _error(status_code, "rate_limit" if status_code == 429 else "server")
        return httpx.Response(200, json=_response())

    managed, http_client = _managed(tmp_path, handler)
    try:
        result = managed.invoke("질문")
    finally:
        http_client.close()

    assert calls == 2
    assert [attempt.status for attempt in result.attempts] == ["retry", "success"]


def test_connect_failure_then_success(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("connect failed", request=request)
        return httpx.Response(200, json=_response())

    managed, http_client = _managed(tmp_path, handler)
    try:
        result = managed.invoke("질문")
    finally:
        http_client.close()

    assert calls == 2
    assert result.attempts[0].status == "retry"


def test_persistent_connect_failure_is_question_level_infrastructure_error(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connect failed", request=request)

    managed, http_client = _managed(tmp_path, handler)
    try:
        with pytest.raises(InfrastructureLLMError) as caught:
            managed.invoke("질문")
    finally:
        http_client.close()

    assert calls == 3
    assert caught.value.attempts[-1].status == "infra_error"


def test_read_timeout_is_not_retried(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("read failed", request=request)

    managed, http_client = _managed(tmp_path, handler)
    try:
        with pytest.raises(BillingUncertainError):
            managed.invoke("질문")
    finally:
        http_client.close()

    assert calls == 1


def test_401_and_persistent_429_are_fatal(tmp_path: Path) -> None:
    statuses = [401, 429]
    for status in statuses:
        calls = 0

        def handler(_request: httpx.Request, *, status: int = status) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _error(status, "rate_limit")

        managed, http_client = _managed(tmp_path / str(status), handler)
        try:
            with pytest.raises(FatalLLMError):
                managed.invoke("질문")
        finally:
            http_client.close()

        assert calls == (3 if status == 429 else 1)


@pytest.mark.parametrize("model_name", ["luna", "sol"])
def test_cost_separates_usage_and_does_not_double_count_reasoning(
    tmp_path: Path,
    model_name: str,
) -> None:
    config = load_answer_config(model_name)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_response(model=config.model))

    managed, http_client = _managed(tmp_path, handler, model_name=model_name)
    try:
        result = managed.invoke("질문")
    finally:
        http_client.close()

    records = [
        json.loads(line)
        for line in (tmp_path / "costs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [(record["service"], record["quantity"]) for record in records] == [
        (f"openai.{config.model}.input", 70),
        (f"openai.{config.model}.cached_input", 20),
        (f"openai.{config.model}.cache_write", 10),
        (f"openai.{config.model}.output", 40),
    ]
    assert result.cost_usd == pytest.approx(
        sum(float(record["amount_usd"]) for record in records)
    )
    assert all(record["generation"]["temperature"] is None for record in records)
    assert all(record["generation"]["effort"] == "low" for record in records)
    assert all(record["settings_hash"] == config.settings_hash() for record in records)
    assert all(record["reasoning_tokens"] == 30 for record in records)


def test_missing_cache_write_field_is_recorded_as_observed_absent(
    tmp_path: Path,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_response(cache_write_tokens=None))

    managed, http_client = _managed(tmp_path, handler)
    try:
        result = managed.invoke("질문")
    finally:
        http_client.close()

    assert result.usage is not None
    assert result.usage.cache_write_tokens == 0
    assert result.usage.cache_write_tokens_present is False
    records = [
        json.loads(line)
        for line in (tmp_path / "costs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert all(record["cache_write_tokens_present"] is False for record in records)


def test_budget_and_input_limit_reject_before_transport(tmp_path: Path) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_response())

    budget_client, first_http = _managed(tmp_path / "budget", handler, budget=0)
    limit_client, second_http = _managed(tmp_path / "limit", handler)
    try:
        with pytest.raises(BudgetExceededError):
            budget_client.invoke("질문")
        with pytest.raises(InputTokenLimitError):
            limit_client.dry_run("가" * 272_001)
        with pytest.raises(InputTokenLimitError):
            limit_client.invoke("가" * 272_001)
    finally:
        first_http.close()
        second_http.close()

    assert calls == 0


def test_post_response_input_limit_is_recorded_then_raised(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_response(input_tokens=272_001))

    managed, http_client = _managed(tmp_path, handler)
    try:
        with pytest.raises(InputTokenLimitError):
            managed.invoke("질문")
    finally:
        http_client.close()

    records = [
        json.loads(line)
        for line in (tmp_path / "costs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert records
    assert all(record["status"] == "input_limit_exceeded" for record in records)


class Answer(BaseModel):
    answer: str


def test_structured_output_returns_parsed_model_and_raises_parse_error(
    tmp_path: Path,
) -> None:
    responses = iter(
        [
            _response(text='{"answer":"구조화 완료"}'),
            _response(text='{"wrong":"field"}'),
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    managed, http_client = _managed(tmp_path, handler)
    try:
        result = managed.invoke("질문", response_schema=Answer)
        with pytest.raises(ChatParseError):
            managed.invoke("질문", response_schema=Answer)
    finally:
        http_client.close()

    assert result.parsed == Answer(answer="구조화 완료")


@pytest.mark.parametrize("with_usage", [True, False])
def test_structured_stream_sdk_events_ttft_and_missing_usage(tmp_path, with_usage):
    from openai.types.responses import (
        Response,
        ResponseCompletedEvent,
        ResponseCreatedEvent,
        ResponseTextDeltaEvent,
    )

    full = _response(text='{"answer":"완료"}')
    full["text"] = {
        "format": {
            "type": "json_schema",
            "name": "Answer",
            "schema": Answer.model_json_schema(),
            "strict": True,
        }
    }
    if not with_usage:
        full["usage"] = None
    events = [
        ResponseCreatedEvent(
            type="response.created",
            sequence_number=0,
            response=Response.model_validate(
                full | {"output": [], "status": "in_progress", "usage": None}
            ),
        ),
        ResponseTextDeltaEvent(
            type="response.output_text.delta",
            sequence_number=1,
            content_index=0,
            output_index=0,
            item_id="msg_test",
            delta='{"answer":"완료"}',
            logprobs=[],
        ),
        ResponseCompletedEvent(
            type="response.completed",
            sequence_number=2,
            response=Response.model_validate(full),
        ),
    ]
    requests = []
    ticks = [0.0]

    def handler(request):
        requests.append(json.loads(request.content))

        def body():
            for i, event in enumerate(events):
                ticks[0] = [1.0, 3.0, 5.0][i]
                yield (
                    f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"
                ).encode()

        return httpx.Response(
            200, content=body(), headers={"content-type": "text/event-stream"}
        )

    managed, client = _managed(tmp_path, handler)
    managed._monotonic = lambda: ticks[0]
    try:
        if with_usage:
            result = managed.invoke("질문", response_schema=Answer, stream=True)
            assert result.parsed == Answer(answer="완료")
            assert result.ttft_ms == 3000
            assert result.usage.output_tokens == 40
            assert result.cost_usd > 0
        else:
            with pytest.raises(BillingUncertainError):
                managed.invoke("질문", response_schema=Answer, stream=True)
        assert len(requests) == 1
        assert requests[0]["stream"] is True
        assert requests[0]["text"]["format"]["type"] == "json_schema"
        assert requests[0]["text"]["format"]["strict"] is True
    finally:
        client.close()


def test_stream_default_request_snapshot_preserves_m6_settings(tmp_path):
    from kodocs.agent.finalize import finalizer_hash
    from kodocs.agent.prompts import prompt_hashes
    from kodocs.agent.types import ModelAnswer

    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, json=_response(text='{"answer":"완료","citations":[],"refused":true}')
        )

    managed, client = _managed(tmp_path, handler)
    try:
        result = managed.invoke("질문", response_schema=ModelAnswer)
    finally:
        client.close()
    import hashlib

    schema = requests[0]["text"]["format"].pop("schema")
    assert (
        hashlib.sha256(
            json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        == "dfae9591f5de8bfa92be8c178f65f88e71b8d4410e99750eb8c52f9d107937fe"
    )
    assert requests[0] == {
        "input": [{"content": "질문", "role": "user", "type": "message"}],
        "max_output_tokens": 4000,
        "model": "gpt-6-luna",
        "reasoning": {"effort": "low"},
        "stream": False,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "ModelAnswer",
                "strict": True,
            }
        },
    }
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["answer", "citations", "refused"]
    frozen = json.loads(Path("data/manifest/m6_config.json").read_text())
    assert result.settings_hash == frozen["answer"]["settings_hash"]
    assert result.generation == frozen["answer"]["settings"]
    assert prompt_hashes() == frozen["prompt_hashes"]
    assert finalizer_hash() == frozen["finalizer_hash"]
    assert result.ttft_ms is None
