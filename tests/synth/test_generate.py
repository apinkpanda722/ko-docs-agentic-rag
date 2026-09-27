"""Contracts for resumable synthetic-document Batch generation."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from kodocs.ingest.manifest import append_record
from kodocs.llm.anthropic_batch import BatchRequest, BatchResult
from kodocs.llm.settings import GenerationConfig
from kodocs.synth import generate
from kodocs.synth.generate import (
    GENERATION_LOG,
    GENERATIONS_DIR,
    build_request,
    cache_key,
    pending_docs,
    run,
    template_hash,
)
from kodocs.synth.plan import DOC_TYPES, DocPlan, build_plan
from kodocs.synth.seed import Seed, load_seed

REPO_ROOT = Path(__file__).parents[2]


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


@pytest.fixture(scope="module")
def corpus() -> tuple[Seed, list[DocPlan]]:
    seed = load_seed(REPO_ROOT)
    plans, _, _ = build_plan(seed)
    return seed, plans


def _plan_record(plan: DocPlan) -> dict[str, object]:
    record = asdict(plan)
    record["access"] = asdict(plan.access)
    return record


def _write_plan(root: Path, plans: Sequence[DocPlan]) -> None:
    path = root / "data/synthetic/plan.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(_plan_record(plan), ensure_ascii=False) + "\n" for plan in plans
        ),
        encoding="utf-8",
    )


def _write_canaries(root: Path, plans: Sequence[DocPlan]) -> None:
    rows = [
        {
            "fact_id": plan.canary_fact_id,
            "doc_id": plan.doc_id,
            "value": f"시험카나리아-{index:04d}",
        }
        for index, plan in enumerate(plans, start=1)
        if plan.canary_fact_id is not None
    ]
    path = root / "data/synthetic/canaries.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"canaries": rows, "count": len(rows)}, ensure_ascii=False),
        encoding="utf-8",
    )


def _prepare_root(
    tmp_path: Path,
    plans: Sequence[DocPlan],
    monkeypatch: pytest.MonkeyPatch,
    seed: Seed,
) -> None:
    _write_plan(tmp_path, plans)
    _write_canaries(tmp_path, plans)
    monkeypatch.setattr(generate, "load_seed", lambda _root: seed)


def _cache_record(
    root: Path,
    plan: DocPlan,
    seed: Seed,
    *,
    result_type: str,
    attempt: int = 1,
    config: GenerationConfig | None = None,
) -> dict[str, object]:
    request, metadata = generate._build_request(
        plan,
        seed,
        config=config or _config(),
        attempt=attempt,
        canary_values={},
    )
    record = {
        **metadata,
        "doc_id": plan.doc_id,
        "attempt": attempt,
        "custom_id": request.custom_id,
        "batch_id": "batch-old",
        "result_type": result_type,
        "text": "# 결과" if result_type == "succeeded" else None,
        "model": "claude-sonnet-5",
        "stop_reason": "end_turn" if result_type == "succeeded" else None,
        "usage": {"input_tokens": 1, "output_tokens": 1}
        if result_type == "succeeded"
        else None,
        "collected_at": "2026-09-27T00:00:00+00:00",
    }
    path = root / GENERATIONS_DIR / f"{request.cache_key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    return record


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
        self.status_calls: list[str] = []
        self.collect_calls: list[str] = []
        self.mark_collected_calls: list[str] = []

    def uncertain_submissions(self) -> list[Mapping[str, object]]:
        return self._uncertain

    def open_batches(self) -> list[Mapping[str, object]]:
        return self._opened

    def status(self, batch_id: str) -> object:
        self.status_calls.append(batch_id)
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
            "processing": value != "ended",
            "succeeded": value == "ended",
            "errored": 0,
            "canceled": 0,
            "expired": 0,
        },
        warnings=(),
    )


def test_cache_key_covers_fact_value_template_seed_attempt_model_and_prompt(
    corpus: tuple[Seed, list[DocPlan]],
) -> None:
    seed, plans = corpus
    plan = plans[0]
    fact = seed.facts[
        next(fact_id for fact_id in plan.fact_ids if fact_id in seed.facts)
    ]
    config = _config()
    common = {
        "template_hash": "a" * 64,
        "facts": [fact],
        "doc_seed": plan.seed,
        "attempt": 1,
        "config": config,
        "prompt_sha256": "b" * 64,
    }

    assert cache_key(**common) == cache_key(**common)
    assert cache_key(
        **{**common, "facts": [replace(fact, value="바뀐 값")]}
    ) != cache_key(**common)
    for field, value in (
        ("template_hash", "c" * 64),
        ("doc_seed", plan.seed + 1),
        ("attempt", 2),
        ("config", replace(config, model="another-model")),
        ("prompt_sha256", "d" * 64),
    ):
        assert cache_key(**{**common, field: value}) != cache_key(**common)


def test_template_bytes_change_template_hash_and_request_key(
    corpus: tuple[Seed, list[DocPlan]], tmp_path: Path
) -> None:
    seed, plans = corpus
    source = REPO_ROOT / "src/kodocs/synth/templates"
    target = tmp_path / "templates"
    target.mkdir()
    for path in source.iterdir():
        target.joinpath(path.name).write_bytes(path.read_bytes())
    first_hash = template_hash("meeting", templates_dir=target)
    first, _ = build_request(
        plans[0], seed, config=_config(), attempt=1, templates_dir=target
    )

    with target.joinpath("meeting.md").open("ab") as destination:
        destination.write(b"\nchanged")

    second_hash = template_hash("meeting", templates_dir=target)
    second, _ = build_request(
        plans[0], seed, config=_config(), attempt=1, templates_dir=target
    )
    assert first_hash != second_hash
    assert first.cache_key != second.cache_key


def test_request_prompt_has_all_facts_only_its_canary_and_no_temperature(
    corpus: tuple[Seed, list[DocPlan]], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, plans = corpus
    restricted = next(plan for plan in plans if plan.canary_fact_id)
    normal = next(plan for plan in plans if plan.canary_fact_id is None)
    values = {
        restricted.canary_fact_id: "청록고래-1000",
        "f-canary-other": "금빛수달-2000",
    }
    monkeypatch.setattr(generate, "_default_canary_values", lambda: values)

    restricted_request, restricted_meta = build_request(
        restricted, seed, config=_config(), attempt=1
    )
    normal_request, _ = build_request(normal, seed, config=_config(), attempt=1)

    assert all(fact_id in restricted_request.user for fact_id in restricted.fact_ids)
    assert "청록고래-1000" in restricted_request.user
    assert "금빛수달-2000" not in restricted_request.user
    assert "청록고래-1000" not in normal_request.user
    assert "금빛수달-2000" not in normal_request.user
    assert restricted_meta["generation"]["temperature"] is None
    assert "temperature" not in _config().request_params()


@pytest.mark.parametrize("answer, expected_submits", [("n", 0), ("y", 1)])
def test_first_run_confirmation_controls_submit(
    corpus: tuple[Seed, list[DocPlan]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
    expected_submits: int,
) -> None:
    seed, plans = corpus
    selected = plans[:2]
    _prepare_root(tmp_path, selected, monkeypatch, seed)
    client = FakeClient()
    output: list[str] = []

    code = run(
        tmp_path,
        client=client,  # type: ignore[arg-type]
        config=_config(),
        dry_run=False,
        limit=None,
        assume_yes=False,
        input_fn=lambda _prompt: answer,
        print_fn=lambda message, **_kwargs: output.append(str(message)),
    )

    assert code == 0
    assert len(client.submit_calls) == expected_submits
    if client.submit_calls:
        assert all(
            re.fullmatch(r"syn-[a-z]+-\d{3}-a1", request.custom_id)
            for request in client.submit_calls[0]
        )
    assert any("예상 비용" in line for line in output)


def test_dry_run_never_submits_and_prints_cost(
    corpus: tuple[Seed, list[DocPlan]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, plans = corpus
    _prepare_root(tmp_path, plans[:3], monkeypatch, seed)
    client = FakeClient()
    output: list[str] = []

    code = run(
        tmp_path,
        client=client,  # type: ignore[arg-type]
        config=_config(),
        dry_run=True,
        limit=None,
        assume_yes=False,
        print_fn=lambda message, **_kwargs: output.append(str(message)),
    )

    assert code == 0
    assert client.submit_calls == []
    assert any("예상 비용" in line and "일반 호출" in line for line in output)


def test_existing_success_cache_is_never_submitted(
    corpus: tuple[Seed, list[DocPlan]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, plans = corpus
    plan = plans[0]
    _prepare_root(tmp_path, [plan], monkeypatch, seed)
    _cache_record(tmp_path, plan, seed, result_type="succeeded")
    client = FakeClient()

    assert (
        run(
            tmp_path,
            client=client,  # type: ignore[arg-type]
            config=_config(),
            dry_run=False,
            limit=None,
            assume_yes=True,
        )
        == 0
    )
    assert client.submit_calls == []


def test_uncertain_submission_blocks_new_submit(
    corpus: tuple[Seed, list[DocPlan]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, plans = corpus
    _prepare_root(tmp_path, plans[:1], monkeypatch, seed)
    client = FakeClient(uncertain=[{"submission_id": "sub-1"}])
    output: list[str] = []

    code = run(
        tmp_path,
        client=client,  # type: ignore[arg-type]
        config=_config(),
        dry_run=False,
        limit=None,
        assume_yes=True,
        print_fn=lambda message, **_kwargs: output.append(str(message)),
    )

    assert code == 2
    assert client.submit_calls == []
    assert any(
        "--adopt-batch" in line and "--abandon-submission" in line for line in output
    )


def test_open_in_progress_batch_blocks_new_submit(
    corpus: tuple[Seed, list[DocPlan]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, plans = corpus
    _prepare_root(tmp_path, plans[:1], monkeypatch, seed)
    client = FakeClient(
        opened=[{"batch_id": "batch-open"}],
        statuses={"batch-open": _status("in_progress")},
    )

    code = run(
        tmp_path,
        client=client,  # type: ignore[arg-type]
        config=_config(),
        dry_run=False,
        limit=None,
        assume_yes=True,
    )

    assert code == 0
    assert client.status_calls == ["batch-open"]
    assert client.submit_calls == []


def test_in_progress_batch_does_not_run_post_collection_pipeline(
    corpus: tuple[Seed, list[DocPlan]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, plans = corpus
    _prepare_root(tmp_path, plans[:1], monkeypatch, seed)
    client = FakeClient(
        opened=[{"batch_id": "batch-open"}],
        statuses={"batch-open": _status("in_progress")},
    )
    calls: list[Path] = []

    code = run(
        tmp_path,
        client=client,  # type: ignore[arg-type]
        config=_config(),
        dry_run=False,
        limit=None,
        assume_yes=True,
        after_collection=lambda root: calls.append(root) or 0,
    )

    assert code == 0
    assert calls == []


def _append_submitted(
    root: Path,
    plan: DocPlan,
    seed: Seed,
    *,
    attempt: int,
    batch_id: str,
) -> BatchRequest:
    request, metadata = generate._build_request(
        plan,
        seed,
        config=_config(),
        attempt=attempt,
        canary_values={},
    )
    append_record(
        root / GENERATION_LOG,
        {
            **metadata,
            "doc_id": plan.doc_id,
            "attempt": attempt,
            "custom_id": request.custom_id,
            "cache_key": request.cache_key,
            "batch_id": batch_id,
            "status": "submitted",
            "ts": "2026-09-27T00:00:00+00:00",
        },
        required={
            "doc_id",
            "attempt",
            "custom_id",
            "cache_key",
            "batch_id",
            "status",
            "ts",
        },
    )
    return request


def test_ended_batch_collects_by_custom_id_and_marks_collected(
    corpus: tuple[Seed, list[DocPlan]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, plans = corpus
    plan = plans[0]
    _prepare_root(tmp_path, [plan], monkeypatch, seed)
    request = _append_submitted(tmp_path, plan, seed, attempt=1, batch_id="batch-ended")
    result = BatchResult(
        custom_id=request.custom_id,
        result_type="succeeded",
        text='# 생성 결과\n<fact id="test">그대로</fact>',
        model="claude-sonnet-5-20260901",
        stop_reason="end_turn",
        usage={"input_tokens": 10, "output_tokens": 20},
        error=None,
    )
    client = FakeClient(
        opened=[{"batch_id": "batch-ended"}],
        statuses={"batch-ended": _status("ended")},
        results={"batch-ended": [result]},
    )

    code = run(
        tmp_path,
        client=client,  # type: ignore[arg-type]
        config=_config(),
        dry_run=False,
        limit=None,
        assume_yes=True,
    )

    assert code == 0
    cached = json.loads(
        (tmp_path / GENERATIONS_DIR / f"{request.cache_key}.json").read_text(
            encoding="utf-8"
        )
    )
    assert cached["text"] == result.text
    assert cached["generation"]["temperature"] is None
    assert client.mark_collected_calls == ["batch-ended"]
    assert client.submit_calls == []


def test_ended_batch_runs_post_collection_pipeline_after_storing_result(
    corpus: tuple[Seed, list[DocPlan]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, plans = corpus
    plan = plans[0]
    _prepare_root(tmp_path, [plan], monkeypatch, seed)
    request = _append_submitted(tmp_path, plan, seed, attempt=1, batch_id="batch-ended")
    result = BatchResult(
        custom_id=request.custom_id,
        result_type="succeeded",
        text="# 생성 결과",
        model="claude-sonnet-5-20260901",
        stop_reason="end_turn",
        usage={"input_tokens": 10, "output_tokens": 20},
        error=None,
    )
    client = FakeClient(
        opened=[{"batch_id": "batch-ended"}],
        statuses={"batch-ended": _status("ended")},
        results={"batch-ended": [result]},
    )
    calls: list[str] = []

    def pipeline(root: Path) -> int:
        assert (root / GENERATIONS_DIR / f"{request.cache_key}.json").is_file()
        calls.extend(["validate", "metrics", "report"])
        return 0

    code = run(
        tmp_path,
        client=client,  # type: ignore[arg-type]
        config=_config(),
        dry_run=False,
        limit=None,
        assume_yes=True,
        after_collection=pipeline,
    )

    assert code == 0
    assert calls == ["validate", "metrics", "report"]


def test_post_collection_pipeline_stops_on_first_failed_stage(tmp_path: Path) -> None:
    calls: list[str] = []

    def stage(name: str, code: int):
        def invoke(_root: Path) -> int:
            calls.append(name)
            return code

        return invoke

    code = generate._run_post_collection(
        tmp_path,
        stages=(
            ("검증", stage("validate", 0)),
            ("지표", stage("metrics", 1)),
            ("보고서", stage("report", 0)),
        ),
    )

    assert code == 1
    assert calls == ["validate", "metrics"]


def test_mixed_collected_results_retry_only_errored_and_expired(
    corpus: tuple[Seed, list[DocPlan]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, plans = corpus
    selected = plans[:4]
    _prepare_root(tmp_path, selected, monkeypatch, seed)
    requests = [
        _append_submitted(tmp_path, plan, seed, attempt=1, batch_id="batch-mixed")
        for plan in selected
    ]
    result_types = ("succeeded", "succeeded", "errored", "expired")
    results = [
        BatchResult(
            custom_id=request.custom_id,
            result_type=result_type,  # type: ignore[arg-type]
            text="# 결과" if result_type == "succeeded" else None,
            model="claude-sonnet-5" if result_type == "succeeded" else None,
            stop_reason="end_turn" if result_type == "succeeded" else None,
            usage={"input_tokens": 1, "output_tokens": 1}
            if result_type == "succeeded"
            else None,
            error=None if result_type == "succeeded" else result_type,
        )
        for request, result_type in zip(requests, result_types, strict=True)
    ]
    collector = FakeClient(
        opened=[{"batch_id": "batch-mixed"}],
        statuses={"batch-mixed": _status("ended")},
        results={"batch-mixed": list(reversed(results))},
    )

    assert (
        run(
            tmp_path,
            client=collector,  # type: ignore[arg-type]
            config=_config(),
            dry_run=False,
            limit=None,
            assume_yes=True,
        )
        == 0
    )
    assert collector.mark_collected_calls == ["batch-mixed"]

    submitter = FakeClient()
    assert (
        run(
            tmp_path,
            client=submitter,  # type: ignore[arg-type]
            config=_config(),
            dry_run=False,
            limit=None,
            assume_yes=True,
        )
        == 0
    )
    assert [request.custom_id for request in submitter.submit_calls[0]] == [
        f"{selected[2].doc_id}-a2",
        f"{selected[3].doc_id}-a2",
    ]


def test_mixed_results_retry_only_failed_and_exhausted_is_not_requested(
    corpus: tuple[Seed, list[DocPlan]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, plans = corpus
    selected = plans[:5]
    _prepare_root(tmp_path, selected, monkeypatch, seed)
    for plan, result_type in zip(
        selected[:4],
        ("succeeded", "succeeded", "errored", "expired"),
        strict=True,
    ):
        _cache_record(tmp_path, plan, seed, result_type=result_type)
    for attempt in range(1, 4):
        _cache_record(
            tmp_path,
            selected[4],
            seed,
            result_type="errored",
            attempt=attempt,
        )
    client = FakeClient()
    output: list[str] = []

    code = run(
        tmp_path,
        client=client,  # type: ignore[arg-type]
        config=_config(),
        dry_run=False,
        limit=None,
        assume_yes=True,
        print_fn=lambda message, **_kwargs: output.append(str(message)),
    )

    assert code == 0
    assert [
        [request.custom_id for request in call] for call in client.submit_calls
    ] == [[f"{selected[2].doc_id}-a2", f"{selected[3].doc_id}-a2"]]
    assert any("exhausted" in line and selected[4].doc_id in line for line in output)


def test_validation_failure_requests_next_attempt(
    corpus: tuple[Seed, list[DocPlan]], tmp_path: Path
) -> None:
    seed, plans = corpus
    plan = plans[0]
    _cache_record(tmp_path, plan, seed, result_type="succeeded")
    failure_path = tmp_path / "data/synthetic/validation_failures.jsonl"
    append_record(
        failure_path,
        {"doc_id": plan.doc_id, "attempt": 1, "status": "failed"},
        required={"doc_id", "attempt", "status"},
    )

    assert pending_docs(tmp_path, [plan], config=_config()) == [(plan, 2)]


def test_limit_is_balanced_across_types_and_access(
    corpus: tuple[Seed, list[DocPlan]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, plans = corpus
    _prepare_root(tmp_path, plans, monkeypatch, seed)
    client = FakeClient()

    assert (
        run(
            tmp_path,
            client=client,  # type: ignore[arg-type]
            config=_config(),
            dry_run=False,
            limit=10,
            assume_yes=True,
        )
        == 0
    )
    requests = client.submit_calls[0]
    selected_ids = {request.custom_id.removesuffix("-a1") for request in requests}
    selected = [plan for plan in plans if plan.doc_id in selected_ids]
    assert len(selected) == 10
    assert len({plan.doc_type for plan in selected}) >= 2
    assert len({plan.access.level for plan in selected}) >= 2


def test_one_result_storage_failure_keeps_batch_open_and_saves_others(
    corpus: tuple[Seed, list[DocPlan]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, plans = corpus
    selected = plans[:2]
    _prepare_root(tmp_path, selected, monkeypatch, seed)
    requests = [
        _append_submitted(tmp_path, plan, seed, attempt=1, batch_id="batch-ended")
        for plan in selected
    ]
    results = [
        BatchResult(
            custom_id=request.custom_id,
            result_type="succeeded",
            text="# 결과",
            model="claude-sonnet-5",
            stop_reason="end_turn",
            usage={"input_tokens": 1, "output_tokens": 1},
            error=None,
        )
        for request in requests
    ]
    original = generate._write_generation_cache

    def fail_one(path: Path, record: Mapping[str, object]) -> None:
        if record["custom_id"] == requests[0].custom_id:
            raise UnicodeEncodeError("utf-8", "x", 0, 1, "broken")
        original(path, record)

    monkeypatch.setattr(generate, "_write_generation_cache", fail_one)
    client = FakeClient(
        opened=[{"batch_id": "batch-ended"}],
        statuses={"batch-ended": _status("ended")},
        results={"batch-ended": results},
    )

    assert (
        run(
            tmp_path,
            client=client,  # type: ignore[arg-type]
            config=_config(),
            dry_run=False,
            limit=None,
            assume_yes=True,
        )
        == 1
    )
    assert not (tmp_path / GENERATIONS_DIR / f"{requests[0].cache_key}.json").exists()
    assert (tmp_path / GENERATIONS_DIR / f"{requests[1].cache_key}.json").exists()
    assert client.mark_collected_calls == []


def test_zero_plan_and_zero_cache_is_failure(
    corpus: tuple[Seed, list[DocPlan]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, _ = corpus
    _prepare_root(tmp_path, [], monkeypatch, seed)
    output: list[str] = []

    code = run(
        tmp_path,
        client=None,
        config=_config(),
        dry_run=True,
        limit=None,
        assume_yes=False,
        print_fn=lambda message, **_kwargs: output.append(str(message)),
    )

    assert code == 1
    assert any("생성 0건" in line for line in output)


def test_collection_progress_lines_are_flushed(
    corpus: tuple[Seed, list[DocPlan]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, plans = corpus
    selected = plans[:20]
    _prepare_root(tmp_path, selected, monkeypatch, seed)
    requests = [
        _append_submitted(tmp_path, plan, seed, attempt=1, batch_id="batch-ended")
        for plan in selected
    ]
    results = [
        BatchResult(
            custom_id=request.custom_id,
            result_type="errored",
            text=None,
            model=None,
            stop_reason=None,
            usage=None,
            error="invalid_request",
        )
        for request in reversed(requests)
    ]
    calls: list[tuple[str, bool]] = []

    def capture(message: object, *, flush: bool = False) -> None:
        calls.append((str(message), flush))

    client = FakeClient(
        opened=[{"batch_id": "batch-ended"}],
        statuses={"batch-ended": _status("ended")},
        results={"batch-ended": results},
    )
    assert (
        run(
            tmp_path,
            client=client,  # type: ignore[arg-type]
            config=_config(),
            dry_run=False,
            limit=None,
            assume_yes=True,
            print_fn=capture,
        )
        == 0
    )
    assert any(re.search(r"\[20/20\]", line) and flush for line, flush in calls)


def test_prompt_gives_calendar_phrase_per_fact_and_forbids_own_relative_time(
    corpus: tuple[Seed, list[DocPlan]],
) -> None:
    seed, plans = corpus
    plan = next(plan for plan in plans if plan.fact_time_phrases)

    request, _ = build_request(plan, seed, config=_config(), attempt=1)

    for fact_id, phrase in plan.fact_time_phrases.items():
        line = next(line for line in request.user.splitlines() if fact_id in line)
        assert f"시점 표현: {phrase}" in line
    assert "임의로 상대 시점을 만들지 않는다" in request.system
    assert "지난해` 또는" not in request.system


def test_pilot_limit_on_committed_plan_covers_types_access_canary_and_retrospective() -> (
    None
):
    plans = generate._load_plan(REPO_ROOT)
    targets = [(plan, 1) for plan in plans]

    first = generate._balanced_limit(targets, 10)
    second = generate._balanced_limit(targets, 10)
    selected = [plan for plan, _ in first]

    assert first == second
    assert len(selected) == len({plan.doc_id for plan in selected}) == 10
    assert {plan.doc_type for plan in selected} == set(DOC_TYPES)
    assert {plan.access.level for plan in selected} == {"public", "team", "restricted"}
    assert any(plan.canary_fact_id for plan in selected)
    assert any(plan.style["frame"] == "회고" for plan in selected)
    order = {plan.doc_id: index for index, plan in enumerate(plans)}
    assert [order[plan.doc_id] for plan in selected] == sorted(
        order[plan.doc_id] for plan in selected
    )


@pytest.mark.parametrize("limit", [6, 7, 8])
def test_limit_of_six_or_more_covers_every_document_type(limit: int) -> None:
    plans = generate._load_plan(REPO_ROOT)
    selected = generate._balanced_limit([(plan, 1) for plan in plans], limit)

    assert len(selected) == limit
    assert {plan.doc_type for plan, _ in selected} == set(DOC_TYPES)


def test_dry_run_with_limit_lists_selected_doc_ids(
    corpus: tuple[Seed, list[DocPlan]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, plans = corpus
    _prepare_root(tmp_path, plans, monkeypatch, seed)
    printed: list[str] = []

    assert (
        run(
            tmp_path,
            client=None,
            config=_config(),
            dry_run=True,
            limit=10,
            assume_yes=False,
            print_fn=lambda message="", **_kwargs: printed.append(str(message)),
        )
        == 0
    )
    expected = [
        plan.doc_id
        for plan, _ in generate._balanced_limit([(plan, 1) for plan in plans], 10)
    ]
    line = next(line for line in printed if line.startswith("선택 문서"))
    assert all(doc_id in line for doc_id in expected)


def test_prompts_for_all_plans_carry_no_person_ids_or_helper_wording(
    corpus: tuple[Seed, list[DocPlan]], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, plans = corpus
    monkeypatch.setattr(
        generate,
        "_default_canary_values",
        lambda: {
            plan.canary_fact_id: "청록고래-1000"
            for plan in plans
            if plan.canary_fact_id
        },
    )
    person_id = re.compile(r"(?<![A-Za-z0-9-])p-\d{3}(?!\d)")
    for plan in plans:
        request, _ = build_request(plan, seed, config=_config(), attempt=1)
        assert person_id.search(request.user) is None, plan.doc_id
        assert "문서 날짜 기준" not in request.user, plan.doc_id
        assert "⟦" not in request.user and "⟧" not in request.user
    assert '<fact id="f-example-restored">2026-06-25 15:18</fact>' in request.system
    assert "⟦" not in request.system


def _store_attempt(
    root: Path,
    plan: DocPlan,
    seed: Seed,
    *,
    attempt: int,
    template: str | None = None,
    passed: bool = False,
) -> None:
    record = _cache_record(root, plan, seed, result_type="succeeded", attempt=attempt)
    request, _ = generate._build_request(
        plan, seed, config=_config(), attempt=attempt, canary_values={}
    )
    record["cache_key"] = request.cache_key
    if template is not None:
        record["template_hash"] = template
    (root / GENERATIONS_DIR / f"{request.cache_key}.json").write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    if passed:
        document = root / "data/synthetic/docs" / f"{plan.doc_id}.json"
        document.parent.mkdir(parents=True, exist_ok=True)
        document.write_text(
            json.dumps({"generation": {"cache_key": request.cache_key}}),
            encoding="utf-8",
        )


def test_document_from_older_prompt_revision_is_regenerated_even_if_it_passed(
    corpus: tuple[Seed, list[DocPlan]], tmp_path: Path
) -> None:
    seed, plans = corpus
    plan = plans[0]
    _store_attempt(tmp_path, plan, seed, attempt=1, template="0" * 64, passed=True)

    assert pending_docs(tmp_path, [plan], config=_config()) == [(plan, 2)]


def test_document_from_current_revision_that_passed_is_not_regenerated(
    corpus: tuple[Seed, list[DocPlan]], tmp_path: Path
) -> None:
    seed, plans = corpus
    plan = plans[0]
    _store_attempt(tmp_path, plan, seed, attempt=1, passed=True)

    assert pending_docs(tmp_path, [plan], config=_config()) == []


def test_attempt_cap_counts_only_the_current_prompt_revision(
    corpus: tuple[Seed, list[DocPlan]], tmp_path: Path
) -> None:
    seed, plans = corpus
    old_revision, current_revision = plans[0], plans[1]
    failures = tmp_path / "data/synthetic/validation_failures.jsonl"
    failures.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for attempt in (1, 2, 3):
        _store_attempt(tmp_path, old_revision, seed, attempt=attempt, template="0" * 64)
        _store_attempt(tmp_path, current_revision, seed, attempt=attempt)
        for plan in (old_revision, current_revision):
            lines.append(
                json.dumps(
                    {
                        "doc_id": plan.doc_id,
                        "attempt": attempt,
                        "issues": [{"code": "marker_malformed", "detail": "x"}],
                    }
                )
            )
    failures.write_text("\n".join(lines) + "\n", encoding="utf-8")

    pending = pending_docs(
        tmp_path, [old_revision, current_revision], config=_config(max_attempts=3)
    )

    assert pending == [(old_revision, 4)]
    assert generate._exhausted_docs(
        tmp_path, [old_revision, current_revision], config=_config(max_attempts=3)
    ) == [current_revision.doc_id]


def test_only_attempted_limits_scope_to_documents_with_history(
    corpus: tuple[Seed, list[DocPlan]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed, plans = corpus
    _prepare_root(tmp_path, plans, monkeypatch, seed)
    attempted = plans[:3]
    for plan in attempted:
        _store_attempt(tmp_path, plan, seed, attempt=1, template="0" * 64, passed=True)
    client = FakeClient()

    assert (
        run(
            tmp_path,
            client=client,  # type: ignore[arg-type]
            config=_config(),
            dry_run=False,
            limit=None,
            assume_yes=True,
            only_attempted=True,
        )
        == 0
    )
    assert [request.custom_id for request in client.submit_calls[0]] == [
        f"{plan.doc_id}-a2" for plan in attempted
    ]
