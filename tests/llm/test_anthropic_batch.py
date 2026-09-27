"""Tests for Sonnet 5 generation settings and the Anthropic Batch wrapper."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from kodocs.costs import BudgetExceededError, BudgetGuard, CostLedger
from kodocs.ingest.manifest import append_record, load_records
from kodocs.llm.anthropic_batch import (
    AnthropicBatchClient,
    BatchRequest,
    SubmitRejectedError,
    SubmitUncertainError,
)
from kodocs.llm.settings import GenerationConfig, load_generation_config

SECRET = "sk-ant-fake-secret-value"
NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)


def _config(**overrides: object) -> GenerationConfig:
    values: dict[str, object] = {
        "model": "claude-sonnet-5",
        "max_tokens": 6000,
        "thinking": "disabled",
        "effort": None,
        "temperature": None,
        "max_attempts": 3,
        "pilot_limit": 10,
        "chars_per_token_estimate": 0.8,
    }
    values.update(overrides)
    return GenerationConfig(**values)  # type: ignore[arg-type]


def _request(custom_id: str, *, secret_prompt: bool = False) -> BatchRequest:
    marker = f" {SECRET}" if secret_prompt else ""
    return BatchRequest(
        custom_id=custom_id,
        system=f"가상 회사 문서만 생성하세요.{marker}",
        user=f"시드 사실을 문서로 작성하세요.{marker}",
        cache_key=f"cache-{custom_id}",
    )


def _usage(
    input_tokens: int = 10,
    output_tokens: int = 20,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
    )


def _success(
    custom_id: str,
    text: str,
    *,
    usage: SimpleNamespace | None = None,
    stop_reason: str = "end_turn",
) -> SimpleNamespace:
    return SimpleNamespace(
        custom_id=custom_id,
        result=SimpleNamespace(
            type="succeeded",
            message=SimpleNamespace(
                model="claude-sonnet-5-20260901",
                content=[
                    SimpleNamespace(type="text", text=text),
                    SimpleNamespace(type="tool_use", text="ignored"),
                    SimpleNamespace(type="text", text=" 끝"),
                ],
                stop_reason=stop_reason,
                usage=usage or _usage(),
            ),
        ),
    )


def _non_success(custom_id: str, result_type: str) -> SimpleNamespace:
    result = SimpleNamespace(type=result_type)
    if result_type == "errored":
        result.error = SimpleNamespace(type="invalid_request")
    return SimpleNamespace(custom_id=custom_id, result=result)


def _batch(
    *,
    batch_id: str = "msgbatch_01",
    status: str = "ended",
    created_at: datetime = NOW,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=batch_id,
        processing_status=status,
        request_counts=SimpleNamespace(
            processing=0 if status == "ended" else 1,
            succeeded=1 if status == "ended" else 0,
            errored=0,
            canceled=0,
            expired=0,
        ),
        created_at=created_at,
        expires_at=created_at + timedelta(hours=24),
        ended_at=created_at + timedelta(hours=1) if status == "ended" else None,
    )


class FakeBatches:
    def __init__(
        self,
        *,
        create_result: object | None = None,
        create_error: Exception | None = None,
        results: list[object] | None = None,
        retrieve_result: object | None = None,
    ) -> None:
        self.create_result = create_result or _batch()
        self.create_error = create_error
        self.result_items = results or []
        self.retrieve_result = retrieve_result or _batch()
        self.create_calls: list[list[object]] = []
        self.retrieve_calls: list[str] = []
        self.results_calls: list[str] = []
        self.list_calls: list[int] = []

    def create(self, *, requests: list[object]) -> object:
        self.create_calls.append(requests)
        if self.create_error is not None:
            raise self.create_error
        return self.create_result

    def retrieve(self, batch_id: str) -> object:
        self.retrieve_calls.append(batch_id)
        return self.retrieve_result

    def results(self, batch_id: str) -> list[object]:
        self.results_calls.append(batch_id)
        return self.result_items

    def list(self, *, limit: int) -> list[object]:
        self.list_calls.append(limit)
        return [self.retrieve_result]


class FakeFactory:
    def __init__(self, batches: FakeBatches) -> None:
        self.batches = batches
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(messages=SimpleNamespace(batches=self.batches))


def _client(
    tmp_path: Path,
    batches: FakeBatches,
    *,
    budget: float = 45.0,
    clock=lambda: NOW,
    new_id=lambda: "submission01",
    manifest_path: Path | None = None,
) -> tuple[AnthropicBatchClient, FakeFactory, CostLedger, Path]:
    ledger = CostLedger(tmp_path / "costs.jsonl", clock=clock)
    factory = FakeFactory(batches)
    manifest = manifest_path or tmp_path / "anthropic_batches.jsonl"
    return (
        AnthropicBatchClient(
            client_factory=factory,
            config=_config(),
            ledger=ledger,
            guard=BudgetGuard(ledger, budget),
            manifest_path=manifest,
            clock=clock,
            new_id=new_id,
        ),
        factory,
        ledger,
        manifest,
    )


def test_default_generation_config_omits_unsupported_sampling_params() -> None:
    config = load_generation_config()

    assert config == _config()
    assert config.request_params() == {
        "model": "claude-sonnet-5",
        "max_tokens": 6000,
        "thinking": {"type": "disabled"},
    }
    assert not {"temperature", "top_p", "top_k"}.intersection(config.request_params())
    assert config.record()["temperature"] is None
    assert config.record()["sampling"] == "unsupported_by_model"
    assert len(config.settings_hash()) == 64


def test_generation_config_adds_only_documented_optional_effort(tmp_path: Path) -> None:
    config_path = tmp_path / "models.toml"
    config_path.write_text(
        """
[synth]
model = "another-model"
max_tokens = 100
thinking = "adaptive"
effort = "high"
temperature = "0.25"
max_attempts = 2
pilot_limit = 1
chars_per_token_estimate = 1.5
""".strip(),
        encoding="utf-8",
    )

    config = load_generation_config(config_path)

    assert config.request_params() == {
        "model": "another-model",
        "max_tokens": 100,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
        "temperature": 0.25,
    }
    assert "top_p" not in config.request_params()
    assert "top_k" not in config.request_params()


def test_estimate_uses_conservative_tokens_tax_and_margin(tmp_path: Path) -> None:
    client, _factory, _ledger, _manifest = _client(tmp_path, FakeBatches())
    requests = [
        BatchRequest("a", "가" * 4, "나" * 4, "one"),
        BatchRequest("b", "다" * 2, "라" * 2, "two"),
    ]

    estimate = client.estimate(requests)

    assert estimate.estimated_input_tokens == 15
    assert estimate.maximum_output_tokens == 12_000
    assert estimate.expected_cost_usd == pytest.approx(15 * 1e-6 * 1.1)
    assert estimate.maximum_cost_usd == pytest.approx((15 * 1e-6 + 12_000 * 5e-6) * 1.1)
    assert estimate.budget_estimate_usd == pytest.approx(
        estimate.maximum_cost_usd * 1.05
    )


def test_submit_records_submitting_then_submitted_and_disables_retries(
    tmp_path: Path,
) -> None:
    batches = FakeBatches()
    client, factory, _ledger, manifest = _client(tmp_path, batches)

    batch_id = client.submit([_request("doc_001")])

    assert batch_id == "msgbatch_01"
    assert factory.calls == [{"max_retries": 0}]
    assert len(batches.create_calls) == 1
    params = batches.create_calls[0][0]["params"]
    assert params["thinking"] == {"type": "disabled"}
    assert not {"temperature", "top_p", "top_k", "cache_control"}.intersection(params)
    records = load_records(manifest)
    assert [record["state"] for record in records] == ["submitting", "submitted"]
    assert records[0]["custom_ids"] == ["doc_001"]
    assert records[1]["batch_id"] == "msgbatch_01"
    assert "system" not in manifest.read_text(encoding="utf-8")
    assert "user" not in manifest.read_text(encoding="utf-8")


class FakeAPIStatusError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class FakeAPIConnectionError(Exception):
    pass


class FakeAPITimeoutError(FakeAPIConnectionError):
    pass


@pytest.mark.parametrize("status_code", [400, 401, 429])
def test_rejected_submit_is_recorded_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    from kodocs.llm import anthropic_batch

    monkeypatch.setattr(anthropic_batch.anthropic, "APIStatusError", FakeAPIStatusError)
    batches = FakeBatches(create_error=FakeAPIStatusError(status_code, SECRET))
    client, _factory, _ledger, manifest = _client(tmp_path, batches)

    with pytest.raises(SubmitRejectedError, match=str(status_code)) as caught:
        client.submit([_request("doc_001", secret_prompt=True)])

    assert len(batches.create_calls) == 1
    assert load_records(manifest)[-1]["state"] == "submit_rejected"
    all_output = str(caught.value) + manifest.read_text(encoding="utf-8")
    assert SECRET not in all_output


@pytest.mark.parametrize(
    "error",
    [
        FakeAPIConnectionError(SECRET),
        FakeAPITimeoutError(SECRET),
        FakeAPIStatusError(500, SECRET),
        RuntimeError(SECRET),
    ],
    ids=["connection", "timeout", "server", "unexpected"],
)
def test_uncertain_submit_blocks_every_resubmission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    from kodocs.llm import anthropic_batch

    monkeypatch.setattr(
        anthropic_batch.anthropic,
        "APIConnectionError",
        FakeAPIConnectionError,
    )
    monkeypatch.setattr(anthropic_batch.anthropic, "APIStatusError", FakeAPIStatusError)
    batches = FakeBatches(create_error=error)
    client, _factory, _ledger, manifest = _client(tmp_path, batches)

    with pytest.raises(SubmitUncertainError) as first:
        client.submit([_request("doc_001", secret_prompt=True)])
    with pytest.raises(
        SubmitUncertainError, match=r"submission01.*adopt.*abandon"
    ) as second:
        client.submit([_request("doc_001")])

    assert len(batches.create_calls) == 1
    assert load_records(manifest)[-1]["state"] == "submit_uncertain"
    all_output = (
        str(first.value) + str(second.value) + manifest.read_text(encoding="utf-8")
    )
    assert SECRET not in all_output


def test_orphan_submitting_can_be_adopted_or_abandoned(tmp_path: Path) -> None:
    batches = FakeBatches()
    client, _factory, _ledger, manifest = _client(tmp_path, batches)
    common = {
        "submission_id": "orphan01",
        "state": "submitting",
        "ts": NOW.isoformat(),
        "n_requests": 1,
        "custom_ids": ["doc_001"],
        "request_set_hash": "a" * 64,
        "generation": _config().record(),
        "settings_hash": _config().settings_hash(),
    }
    append_record(manifest, common, required=common.keys())

    with pytest.raises(SubmitUncertainError):
        client.submit([_request("new_001")])
    assert batches.create_calls == []

    client.adopt("orphan01", "msgbatch_adopted")
    assert [record["batch_id"] for record in client.open_batches()] == [
        "msgbatch_adopted"
    ]
    assert client.uncertain_submissions() == []

    second_manifest = tmp_path / "abandon.jsonl"
    second, _factory2, _ledger2, _path = _client(
        tmp_path / "second",
        batches,
        manifest_path=second_manifest,
    )
    append_record(second_manifest, common, required=common.keys())
    second.abandon("orphan01", "콘솔에서 배치 없음 확인")
    assert second.submit([_request("new_001")]) == "msgbatch_01"


@pytest.mark.parametrize("custom_id", ["a" * 65, "bad.id", "한글"])
def test_invalid_custom_id_never_creates(tmp_path: Path, custom_id: str) -> None:
    batches = FakeBatches()
    client, factory, _ledger, _manifest = _client(tmp_path, batches)

    with pytest.raises(ValueError, match="custom_id"):
        client.submit([_request(custom_id)])

    assert batches.create_calls == []
    assert factory.calls == []


def test_duplicate_custom_id_and_budget_excess_never_create(tmp_path: Path) -> None:
    batches = FakeBatches()
    client, factory, _ledger, _manifest = _client(tmp_path, batches, budget=0)

    with pytest.raises(ValueError, match="unique"):
        client.submit([_request("same"), _request("same")])
    with pytest.raises(BudgetExceededError):
        client.submit([_request("valid")])

    assert batches.create_calls == []
    assert factory.calls == []


def test_collect_matches_reversed_results_by_custom_id(
    tmp_path: Path,
) -> None:
    batches = FakeBatches(
        results=[_success("second", "둘째"), _success("first", "첫째")]
    )
    client, _factory, _ledger, _manifest = _client(tmp_path, batches)
    client.submit([_request("first"), _request("second")])

    results = client.collect("msgbatch_01")

    assert [result.custom_id for result in results] == ["first", "second"]
    assert results[0].text == "첫째 끝"
    assert results[1].text == "둘째 끝"


def test_collect_marks_a_missing_custom_id(tmp_path: Path) -> None:
    batches = FakeBatches(results=[_success("second", "둘째")])
    client, _factory, _ledger, _manifest = _client(tmp_path, batches)
    client.submit([_request("first"), _request("second")])

    results = client.collect("msgbatch_01")

    assert results[0].result_type == "missing"
    assert results[0].error == "missing"


def test_collect_rejects_unknown_custom_id(tmp_path: Path) -> None:
    batches = FakeBatches(results=[_success("unknown", "본문")])
    client, _factory, _ledger, _manifest = _client(tmp_path, batches)
    client.submit([_request("expected")])

    with pytest.raises(ValueError, match="unknown"):
        client.collect("msgbatch_01")


def test_collect_mixed_results_records_only_success_usage_once(tmp_path: Path) -> None:
    batches = FakeBatches(
        results=[
            _non_success("expired", "expired"),
            _success("ok", "본문"),
            _non_success("error", "errored"),
            _non_success("cancel", "canceled"),
        ]
    )
    client, _factory, _ledger, manifest = _client(tmp_path, batches)
    client.submit(
        [
            _request("ok"),
            _request("error"),
            _request("cancel"),
            _request("expired"),
        ]
    )

    first = client.collect("msgbatch_01")
    second = client.collect("msgbatch_01")

    assert [result.result_type for result in first] == [
        "succeeded",
        "errored",
        "canceled",
        "expired",
    ]
    assert first == second
    entries = [
        json.loads(line)
        for line in (tmp_path / "costs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [(entry["service"], entry["quantity"]) for entry in entries] == [
        ("anthropic.claude-sonnet-5.batch.input", 10),
        ("anthropic.claude-sonnet-5.batch.output", 20),
    ]
    assert all(entry["ref"] == "ok" for entry in entries)
    assert all(entry["run_id"] == "msgbatch_01" for entry in entries)

    client.mark_collected("msgbatch_01")
    collected = load_records(manifest)[-1]
    assert collected["state"] == "collected"
    assert collected["result_counts"] == {
        "succeeded": 1,
        "errored": 1,
        "canceled": 1,
        "expired": 1,
        "missing": 0,
    }
    assert collected["usage_totals"] == {
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def test_collect_records_nonzero_cache_usage_and_max_token_success(
    tmp_path: Path,
) -> None:
    batches = FakeBatches(
        results=[
            _success(
                "ok",
                "잘린 본문",
                usage=_usage(
                    input_tokens=11,
                    output_tokens=12,
                    cache_creation_input_tokens=13,
                    cache_read_input_tokens=14,
                ),
                stop_reason="max_tokens",
            )
        ]
    )
    client, _factory, _ledger, _manifest = _client(tmp_path, batches)
    client.submit([_request("ok")])

    (result,) = client.collect("msgbatch_01")

    assert result.result_type == "succeeded"
    assert result.stop_reason == "max_tokens"
    entries = [
        json.loads(line)
        for line in (tmp_path / "costs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [(entry["service"], entry["quantity"]) for entry in entries] == [
        ("anthropic.claude-sonnet-5.batch.input", 11),
        ("anthropic.claude-sonnet-5.batch.output", 12),
        ("anthropic.claude-sonnet-5.batch.cache-write-5m", 13),
        ("anthropic.claude-sonnet-5.batch.cache-read", 14),
    ]


def test_collect_requires_ended_batch(tmp_path: Path) -> None:
    batches = FakeBatches(retrieve_result=_batch(status="in_progress"))
    client, _factory, _ledger, _manifest = _client(tmp_path, batches)
    client.submit([_request("doc_001")])

    with pytest.raises(RuntimeError, match="ended"):
        client.collect("msgbatch_01")

    assert batches.result_items == []
    assert batches.results_calls == []


def test_status_warns_after_24_hours_and_25_days_when_uncollected(
    tmp_path: Path,
) -> None:
    created = NOW - timedelta(days=26)
    batches = FakeBatches(
        retrieve_result=_batch(status="in_progress", created_at=created)
    )
    client, _factory, _ledger, _manifest = _client(tmp_path, batches)

    status = client.status("msgbatch_old")

    assert status.elapsed_s == pytest.approx(timedelta(days=26).total_seconds())
    assert any("24" in warning for warning in status.warnings)
    assert any("25" in warning and "29" in warning for warning in status.warnings)


def test_read_operations_keep_sdk_default_retries(tmp_path: Path) -> None:
    batches = FakeBatches(retrieve_result=_batch())
    client, factory, _ledger, _manifest = _client(tmp_path, batches)

    client.status("msgbatch_01")
    client.open_batches()

    assert factory.calls == [{}]
