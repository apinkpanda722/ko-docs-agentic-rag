"""Contracts for resumable synthetic-question Batch drafting."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from kodocs.eval import query_draft
from kodocs.eval.golden import Question, load_questions, write_questions
from kodocs.eval.query_draft import (
    BATCHES_MANIFEST,
    DRAFT_LOG,
    DRAFTS_DIR,
    FAILURES_PATH,
    VALIDATION_PATH,
    build_request,
    pending_specs,
    run,
    validate_draft,
)
from kodocs.llm.anthropic_batch import BatchRequest, BatchResult
from kodocs.llm.settings import GenerationConfig, load_generation_config
from kodocs.synth.seed import Seed, load_seed

REPO_ROOT = Path(__file__).parents[2]


def _config(**overrides: object) -> GenerationConfig:
    values: dict[str, object] = {
        "model": "claude-sonnet-5",
        "max_tokens": 400,
        "thinking": "disabled",
        "effort": None,
        "temperature": None,
        "max_attempts": 3,
        "pilot_limit": 1,
        "chars_per_token_estimate": 0.8,
    }
    values.update(overrides)
    return GenerationConfig(**values)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def seed() -> Seed:
    return load_seed(REPO_ROOT)


@pytest.fixture(scope="module")
def specs() -> list[Question]:
    # Frozen copies of five step-2 specs so regenerating specs.jsonl (e.g. with
    # new diversity rules) does not break drafts written against these specs.
    return load_questions(Path(__file__).parent / "fixtures/query_draft_specs.jsonl")


def _by_id(specs: Sequence[Question], question_id: str) -> Question:
    return next(spec for spec in specs if spec.id == question_id)


class FakeClient:
    def __init__(
        self,
        *,
        uncertain: Sequence[Mapping[str, object]] = (),
        opened: Sequence[Mapping[str, object]] = (),
        statuses: Mapping[str, object] | None = None,
        results: Mapping[str, Sequence[BatchResult]] | None = None,
    ) -> None:
        self._uncertain = list(uncertain)
        self._opened = list(opened)
        self._statuses = dict(statuses or {})
        self._results = {key: list(value) for key, value in (results or {}).items()}
        self.submit_calls: list[list[BatchRequest]] = []
        self.collect_calls: list[str] = []
        self.mark_collected_calls: list[str] = []

    def uncertain_submissions(self) -> list[Mapping[str, object]]:
        return self._uncertain

    def open_batches(self) -> list[Mapping[str, object]]:
        return self._opened

    def status(self, batch_id: str) -> object:
        return self._statuses[batch_id]

    def collect(self, batch_id: str) -> list[BatchResult]:
        self.collect_calls.append(batch_id)
        return self._results[batch_id]

    def mark_collected(self, batch_id: str) -> None:
        self.mark_collected_calls.append(batch_id)

    def submit(self, requests: Sequence[BatchRequest]) -> str:
        self.submit_calls.append(list(requests))
        return "batch-new"


def _status(value: str) -> object:
    return SimpleNamespace(
        processing_status=value,
        request_counts={
            "processing": int(value != "ended"),
            "succeeded": int(value == "ended"),
            "errored": 0,
            "canceled": 0,
            "expired": 0,
        },
        warnings=(),
    )


def _result(custom_id: str, text: str) -> BatchResult:
    return BatchResult(
        custom_id=custom_id,
        result_type="succeeded",
        text=text,
        model="claude-sonnet-5",
        stop_reason="end_turn",
        usage={
            "input_tokens": 20,
            "output_tokens": 10,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        error=None,
    )


def _write_specs(root: Path, specs: Sequence[Question]) -> None:
    write_questions(root / query_draft.SPECS_PATH, specs)


def test_golden_query_config_records_unsupported_sampling_as_null() -> None:
    config = load_generation_config(section="golden_queries")

    assert config.model == "claude-sonnet-5"
    assert config.max_tokens == 400
    assert config.max_attempts == 3
    assert config.chars_per_token_estimate == 0.8
    assert config.request_params() == {
        "model": "claude-sonnet-5",
        "max_tokens": 400,
        "thinking": {"type": "disabled"},
    }
    assert config.record()["temperature"] is None
    assert config.record()["effort"] is None


def test_all_requests_exclude_answer_evidence_and_internal_ids(
    specs: Sequence[Question],
) -> None:
    config = _config()

    for spec in specs:
        request = build_request(spec, config=config, attempt=1)
        assert request.custom_id == f"{spec.id}-a1"
        assert all(anchor in request.user for anchor in spec.anchor_values)
        assert "묻는 속성 이름:" in request.user
        assert not re.search(r"(?<![A-Za-z0-9])p-\d{3}(?!\d)", request.user)
        assert all(
            qrel.fact_id not in request.user
            for qrel in spec.qrels
            if qrel.fact_id is not None
        )
        # A few multi-document specs have an answer fact whose value is also the
        # required subject anchor. That permitted subject occurrence is not an
        # answer disclosure; every non-anchor answer form must remain absent.
        assert all(
            value not in request.user
            for value in spec.answer_values
            if value not in spec.anchor_values
        )
        assert request.cache_key


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", ["empty_or_multiline"]),
        (
            "DAI-1001의 조치 내용은 무엇인가요?\n답변은 쓰지 마세요.",
            ["empty_or_multiline"],
        ),
        ("이 장애의 조치 내용은 무엇인가요?", ["anchor_missing"]),
        (
            "DAI-1001의 조치 내용은 무엇인가요? 자세히 알려주세요.",
            ["empty_or_multiline"],
        ),
        ("DAI-1001의 조치 내용은 무엇인가요", ["empty_or_multiline"]),
        ("DAI-1001의 조치 내용은 무엇인가요!", ["empty_or_multiline"]),
        (
            "DAI-1001은 심각한 장애입니다. 조치 내용을 알려주세요.",
            ["empty_or_multiline"],
        ),
    ],
)
def test_validate_draft_basic_failure_codes(
    specs: Sequence[Question], seed: Seed, text: str, expected: list[str]
) -> None:
    spec = _by_id(specs, "g-syn-001")
    assert validate_draft(spec, text, seed=seed, people=seed.people) == expected


def test_validate_draft_anchor_uses_nfc_and_whitespace_normalization(
    specs: Sequence[Question], seed: Seed
) -> None:
    spec = replace(
        _by_id(specs, "g-syn-001"),
        anchor_values=("DAI-  1001",),
        answer_values=(),
        reference_answer=None,
        qrels=(),
    )
    text = "ＤＡＩ가 아닌 DAI- 1001의 조치 내용을 알려주세요?"

    assert validate_draft(spec, text, seed=seed, people=seed.people) == []


def test_validate_draft_detects_atomic_answer_leak(
    specs: Sequence[Question], seed: Seed
) -> None:
    spec = _by_id(specs, "g-syn-003")
    text = "DAI-1001의 담당자가 오가안인가요?"

    assert validate_draft(spec, text, seed=seed, people=seed.people) == ["answer_leak"]


def test_validate_draft_detects_narrative_bigram_answer_leak(
    specs: Sequence[Question], seed: Seed
) -> None:
    spec = _by_id(specs, "g-syn-001")
    text = "DAI-1001에서 스키마 호환 검사를 추가하고 적재를 재실행했나요?"

    assert validate_draft(spec, text, seed=seed, people=seed.people) == ["answer_leak"]


def test_validate_draft_allows_identifier_subject_even_if_listed_as_answer(
    specs: Sequence[Question], seed: Seed
) -> None:
    base = _by_id(specs, "g-syn-001")
    spec = replace(
        base,
        answer_values=base.anchor_values,
        reference_answer=None,
        qrels=(),
    )

    assert (
        validate_draft(
            spec,
            "DAI-1001의 후속 조치 내용을 알려주세요?",
            seed=seed,
            people=seed.people,
        )
        == []
    )


def test_validate_draft_does_not_treat_required_multi_doc_anchor_as_a_leak(
    specs: Sequence[Question], seed: Seed
) -> None:
    # Step 2 intentionally produced a few linked-fact specs where the service
    # subject is also one answer fact. The anchor is still mandatory prompt
    # input, so that exact occurrence cannot make the draft impossible to pass.
    spec = _by_id(specs, "s-syn-087")

    assert (
        validate_draft(
            spec,
            "analytics-hub와 연결된 다른 사건은 무엇인가요?",
            seed=seed,
            people=seed.people,
        )
        == []
    )


def test_validate_draft_requires_temporal_date_in_an_allowed_form(
    specs: Sequence[Question], seed: Seed
) -> None:
    spec = replace(
        _by_id(specs, "s-syn-101"),
        answer_values=(),
        reference_answer=None,
        qrels=(),
    )

    assert validate_draft(
        spec,
        "analytics-hub의 해당 장애 심각도는 무엇인가요?",
        seed=seed,
        people=seed.people,
    ) == ["as_of_missing"]
    assert (
        validate_draft(
            spec,
            "2025년 4월 10일 기준 analytics-hub의 해당 장애 심각도는 무엇인가요?",
            seed=seed,
            people=seed.people,
        )
        == []
    )


@pytest.mark.parametrize("artifact", ["p-123", "fact", "f-event-1", "명세", "주어 값"])
def test_validate_draft_detects_prompt_artifacts(
    specs: Sequence[Question], seed: Seed, artifact: str
) -> None:
    spec = replace(
        _by_id(specs, "g-syn-001"),
        answer_values=(),
        reference_answer=None,
        qrels=(),
    )
    text = f"DAI-1001의 {artifact} 관련 조치 내용을 알려주세요?"

    assert validate_draft(spec, text, seed=seed, people=seed.people) == [
        "prompt_artifact"
    ]


def test_pending_specs_counts_only_current_template_attempts_and_is_monotonic(
    tmp_path: Path, specs: Sequence[Question]
) -> None:
    spec = _by_id(specs, "g-syn-001")
    current_hash = query_draft.template_hash()
    drafts = tmp_path / DRAFTS_DIR
    drafts.mkdir(parents=True)
    drafts.joinpath(f"{spec.id}-a1.json").write_text(
        json.dumps(
            {
                "id": spec.id,
                "attempt": 1,
                "template_hash": "old-template",
                "result_type": "errored",
            }
        ),
        encoding="utf-8",
    )
    drafts.joinpath(f"{spec.id}-a2.json").write_text(
        json.dumps(
            {
                "id": spec.id,
                "attempt": 2,
                "template_hash": current_hash,
                "result_type": "succeeded",
            }
        ),
        encoding="utf-8",
    )

    assert pending_specs(tmp_path, [spec], config=_config()) == [(spec, 3)]

    for attempt in (3, 4):
        drafts.joinpath(f"{spec.id}-a{attempt}.json").write_text(
            json.dumps(
                {
                    "id": spec.id,
                    "attempt": attempt,
                    "template_hash": current_hash,
                    "result_type": "errored",
                }
            ),
            encoding="utf-8",
        )
    assert pending_specs(tmp_path, [spec], config=_config()) == []


def test_pending_specs_skips_validated_question(
    tmp_path: Path, specs: Sequence[Question]
) -> None:
    spec = replace(_by_id(specs, "g-syn-001"), question="유효한 질문?")
    write_questions(tmp_path / VALIDATION_PATH, [spec])

    assert pending_specs(tmp_path, [spec], config=_config()) == []


def test_pending_specs_treats_append_only_submission_log_as_an_attempt(
    tmp_path: Path, specs: Sequence[Question]
) -> None:
    spec = _by_id(specs, "g-syn-001")
    request = build_request(spec, config=_config(), attempt=1)
    path = tmp_path / DRAFT_LOG
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "id": spec.id,
                "attempt": 1,
                "custom_id": request.custom_id,
                "cache_key": request.cache_key,
                "batch_id": "batch-old",
                "status": "submitted",
                "generation": _config().record(),
                "settings_hash": _config().settings_hash(),
                "template_hash": query_draft.template_hash(),
                "prompt_sha256": "a" * 64,
                "ts": "2026-09-28T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert pending_specs(tmp_path, [spec], config=_config()) == [(spec, 2)]


def test_dry_run_has_no_client_and_appends_estimate_manifest(
    tmp_path: Path, specs: Sequence[Question], monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = [
        replace(specs[(index - 1) % len(specs)], id=f"s-syn-{index:03d}")
        for index in range(1, 22)
    ]
    _write_specs(tmp_path, selected)
    monkeypatch.setenv("MONTHLY_BUDGET_USD", "45")
    messages: list[tuple[str, dict[str, object]]] = []

    def capture(message: str, **kwargs: object) -> None:
        messages.append((message, kwargs))

    assert (
        run(
            tmp_path,
            client=None,
            config=_config(),
            dry_run=True,
            assume_yes=False,
            limit=None,
            print_fn=capture,
        )
        == 0
    )

    output = "\n".join(message for message, _ in messages)
    assert "대상 21건" in output
    assert "추정 입력" in output
    assert "예상 비용(Batch, VAT 포함)" in output
    assert any(
        "[20/21]" in message and kwargs.get("flush") is True
        for message, kwargs in messages
    )
    records = [
        json.loads(line)
        for line in (tmp_path / query_draft.DRY_RUNS_PATH)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(records) == 1
    assert records[0]["n_requests"] == 21
    assert records[0]["estimated_input_tokens"] > 0
    assert records[0]["expected_cost_usd"] > 0


def test_dry_run_never_touches_a_supplied_client(
    tmp_path: Path, specs: Sequence[Question], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_specs(tmp_path, specs[:1])
    monkeypatch.setenv("MONTHLY_BUDGET_USD", "45")

    class ExplodingClient:
        def uncertain_submissions(self) -> list[object]:
            raise AssertionError("dry-run must not inspect remote state")

    assert (
        run(
            tmp_path,
            client=ExplodingClient(),  # type: ignore[arg-type]
            config=_config(),
            dry_run=True,
            assume_yes=False,
            limit=None,
            print_fn=lambda *args, **kwargs: None,
        )
        == 0
    )


def test_dry_run_fails_when_guarded_maximum_exceeds_remaining_budget(
    tmp_path: Path, specs: Sequence[Question], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_specs(tmp_path, specs[:1])
    monkeypatch.setenv("MONTHLY_BUDGET_USD", "0")

    assert (
        run(
            tmp_path,
            client=None,
            config=_config(),
            dry_run=True,
            assume_yes=False,
            limit=None,
            print_fn=lambda *args, **kwargs: None,
        )
        == 1
    )


def test_uncertain_submission_blocks_create_without_loading_specs() -> None:
    client = FakeClient(uncertain=[{"submission_id": "sub-uncertain"}])

    assert (
        run(
            Path("unused"),
            client=client,  # type: ignore[arg-type]
            config=_config(),
            dry_run=False,
            assume_yes=True,
            limit=None,
            print_fn=lambda *args, **kwargs: None,
        )
        == 2
    )
    assert client.submit_calls == []


def test_open_batch_is_polled_without_new_submission(
    tmp_path: Path,
) -> None:
    client = FakeClient(
        opened=[{"batch_id": "batch-open"}],
        statuses={"batch-open": _status("in_progress")},
    )
    messages: list[str] = []

    assert (
        run(
            tmp_path,
            client=client,  # type: ignore[arg-type]
            config=_config(),
            dry_run=False,
            assume_yes=True,
            limit=None,
            print_fn=lambda message, **kwargs: messages.append(message),
        )
        == 0
    )
    assert client.submit_calls == []
    assert any("make queries" in message for message in messages)


def test_collection_preserves_every_spec_field_and_attempt_metadata(
    tmp_path: Path,
    specs: Sequence[Question],
    seed: Seed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _by_id(specs, "g-syn-010")
    _write_specs(tmp_path, [spec])
    monkeypatch.setattr(query_draft, "load_seed", lambda _root: seed)
    client = FakeClient(
        opened=[{"batch_id": "batch-done"}],
        statuses={"batch-done": _status("ended")},
        results={
            "batch-done": [
                _result(
                    f"{spec.id}-a1",
                    "analytics-hub의 운영 담당 팀은 어디인가요?",
                )
            ]
        },
    )

    assert (
        run(
            tmp_path,
            client=client,  # type: ignore[arg-type]
            config=_config(),
            dry_run=False,
            assume_yes=True,
            limit=None,
            print_fn=lambda *args, **kwargs: None,
        )
        == 0
    )

    validated = load_questions(tmp_path / VALIDATION_PATH)
    assert validated == [
        replace(spec, question="analytics-hub의 운영 담당 팀은 어디인가요?")
    ]
    assert validated[0].unmarked_candidates == spec.unmarked_candidates
    assert validated[0].no_answer_checks == spec.no_answer_checks
    cache = json.loads(
        (tmp_path / DRAFTS_DIR / f"{spec.id}-a1.json").read_text(encoding="utf-8")
    )
    assert cache["generation"] == _config().record()
    assert cache["template_hash"] == query_draft.template_hash()
    assert cache["batch_id"] == "batch-done"
    assert client.mark_collected_calls == ["batch-done"]


def test_validation_failure_is_append_only_and_deduplicated(
    tmp_path: Path,
    specs: Sequence[Question],
    seed: Seed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _by_id(specs, "g-syn-001")
    _write_specs(tmp_path, [spec])
    monkeypatch.setattr(query_draft, "load_seed", lambda _root: seed)
    client = FakeClient(
        opened=[{"batch_id": "batch-invalid"}],
        statuses={"batch-invalid": _status("ended")},
        results={
            "batch-invalid": [
                _result(
                    f"{spec.id}-a1",
                    "DAI-1001에서 스키마 호환 검사를 추가하고 적재를 재실행했나요?",
                )
            ]
        },
    )

    for _ in range(2):
        assert (
            run(
                tmp_path,
                client=client,  # type: ignore[arg-type]
                config=_config(),
                dry_run=False,
                assume_yes=True,
                limit=None,
                print_fn=lambda *args, **kwargs: None,
            )
            == 0
        )

    failures = [
        json.loads(line)
        for line in (tmp_path / FAILURES_PATH).read_text(encoding="utf-8").splitlines()
    ]
    assert failures == [
        {
            "attempt": 1,
            "codes": ["answer_leak"],
            "id": spec.id,
            "ts": failures[0]["ts"],
        }
    ]
    assert (
        len(
            [
                json.loads(line)
                for line in (tmp_path / DRAFT_LOG)
                .read_text(encoding="utf-8")
                .splitlines()
                if json.loads(line).get("status") == "stored"
            ]
        )
        == 2
    )


def test_submit_requires_confirmation_and_records_template_metadata(
    tmp_path: Path, specs: Sequence[Question], monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _by_id(specs, "g-syn-001")
    _write_specs(tmp_path, [spec])
    monkeypatch.setenv("MONTHLY_BUDGET_USD", "45")
    client = FakeClient()

    assert (
        run(
            tmp_path,
            client=client,  # type: ignore[arg-type]
            config=_config(),
            dry_run=False,
            assume_yes=False,
            limit=None,
            input_fn=lambda _prompt: "n",
            print_fn=lambda *args, **kwargs: None,
        )
        == 0
    )
    assert client.submit_calls == []

    assert (
        run(
            tmp_path,
            client=client,  # type: ignore[arg-type]
            config=_config(),
            dry_run=False,
            assume_yes=True,
            limit=None,
            print_fn=lambda *args, **kwargs: None,
        )
        == 0
    )
    assert len(client.submit_calls) == 1
    log = json.loads((tmp_path / DRAFT_LOG).read_text(encoding="utf-8").splitlines()[0])
    assert log["status"] == "submitted"
    assert log["generation"] == _config().record()
    assert log["template_hash"] == query_draft.template_hash()
    assert log["batch_id"] == "batch-new"


def test_main_constructs_client_with_golden_only_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class Client:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def uncertain_submissions(self) -> list[object]:
            return []

        def open_batches(self) -> list[object]:
            return []

    settings = SimpleNamespace(
        monthly_budget_usd=45.0,
        require_key=lambda _name: "test-key",
    )
    monkeypatch.setattr(query_draft, "load_settings", lambda *args, **kwargs: settings)
    monkeypatch.setattr(query_draft, "anthropic_client_factory", lambda _key: object())
    monkeypatch.setattr(query_draft, "AnthropicBatchClient", Client)

    assert query_draft.main(["--root", str(tmp_path), "--status"]) == 0
    assert captured["manifest_path"] == tmp_path / BATCHES_MANIFEST
    assert captured["manifest_path"] != tmp_path / Path(
        "data/manifest/anthropic_batches.jsonl"
    )


def test_draft_written_for_an_older_spec_is_stale_and_costs_no_attempt(
    tmp_path: Path,
    specs: Sequence[Question],
    seed: Seed,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _by_id(specs, "g-syn-010")
    current = replace(old, anchor_values=("core-gateway",))
    _write_specs(tmp_path, [current])
    monkeypatch.setattr(query_draft, "load_seed", lambda _root: seed)
    request = build_request(old, config=_config(), attempt=1)
    query_draft._append_draft_log(
        tmp_path,
        {
            **query_draft._request_metadata(request, config=_config()),
            "id": old.id,
            "attempt": 1,
            "custom_id": request.custom_id,
            "cache_key": request.cache_key,
            "batch_id": "batch-old",
            "status": "submitted",
            "ts": "2026-09-28T00:00:00+00:00",
        },
    )
    client = FakeClient(
        opened=[{"batch_id": "batch-old"}],
        statuses={"batch-old": _status("ended")},
        results={
            "batch-old": [
                _result(f"{old.id}-a1", "analytics-hub의 담당 팀은 어디인가요?")
            ]
        },
    )

    assert (
        run(
            tmp_path,
            client=client,  # type: ignore[arg-type]
            config=_config(),
            dry_run=False,
            assume_yes=True,
            limit=None,
            print_fn=lambda *args, **kwargs: None,
        )
        == 0
    )

    log = [
        json.loads(line)
        for line in (tmp_path / DRAFT_LOG).read_text(encoding="utf-8").splitlines()
    ]
    assert [r["status"] for r in log] == ["submitted", "stale_spec"]
    assert not (tmp_path / FAILURES_PATH).exists()
    assert not (tmp_path / DRAFTS_DIR / f"{old.id}-a1.json").exists()
    assert load_questions(tmp_path / VALIDATION_PATH) == []
    assert client.mark_collected_calls == ["batch-old"]
    pending = pending_specs(tmp_path, [current], config=_config())
    assert [(spec.id, attempt) for spec, attempt in pending] == [(old.id, 1)]


@pytest.mark.parametrize(
    "text",
    [
        "DAI-1001의 조치 내용을 알려주세요.",
        "DAI-1001의 조치 내용은 무엇인가요?",
        "2026. 5. 5. 기준으로 DAI-1001의 조치 내용을 알려주세요.",
        "가용성 99.95% 기준에서 DAI-1001의 조치 내용을 알려주세요.",
    ],
)
def test_bad_shape_accepts_one_sentence_ending_in_a_single_mark(text: str) -> None:
    assert not query_draft._bad_shape(None, text, None, {})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "조치 내용을 알려주세요.\n감사합니다.",
        "장애입니다. 조치 내용을 알려주세요.",
        "조치 내용을 알려주세요",
        "조치 내용을 알려주세요!",
    ],
)
def test_bad_shape_rejects_empty_multiline_and_multi_sentence(text: str) -> None:
    assert query_draft._bad_shape(None, text, None, {})  # type: ignore[arg-type]
