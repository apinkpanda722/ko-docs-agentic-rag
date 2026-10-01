from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kodocs.agent.finalize import finalizer_hash
from kodocs.agent.guardrails import GuardrailConfig
from kodocs.agent.types import AnswerResult, Citation
from kodocs.costs import BudgetExceededError
from kodocs.eval.golden import Asker, Question
from kodocs.eval.m6_runner import run_answers
from kodocs.llm.openai_chat import BillingUncertainError, FatalLLMError
from kodocs.tools.deps import M5_CONFIG_HASH


def _question(qid: str) -> Question:
    return Question(
        id=qid,
        set="gold",
        source="synthetic",
        type="semantic",
        tags=(),
        question=f"질문-{qid}",
        asker=Asker("platform", 3),
        as_of=None,
        qrels=(),
        anchor_values=(),
        answer_values=("답",),
        expected_response="answer",
        sql=None,
        reference_answer=None,
        reference_sha256=None,
        summary80=None,
        split="dev",
    )


def _runtime():
    return SimpleNamespace(
        m5_config_hash=M5_CONFIG_HASH,
        guardrails=SimpleNamespace(
            settings_hash=lambda: GuardrailConfig().settings_hash()
        ),
    )


def _result(qid: str) -> AnswerResult:
    return AnswerResult(
        answer=f"답-{qid}",
        citations=(Citation(kind="doc", doc_id="d1"),),
        refused=False,
        refuse_reason=None,
        trace={
            "trace_id": f"trace-{qid}",
            "steps": [],
            "timings": {"total_ms": 10.0, "embed_ms": 1.0, "search_ms": 2.0},
            "guardrail": {"steps_used": 1, "tokens_used": 12},
        },
    )


def _models():
    return {
        "m": SimpleNamespace(
            model="fake-model",
            record=lambda: {"model": "fake-model", "effort": "low"},
            settings_hash=lambda: "model-settings-hash",
        )
    }


def test_runner_reuses_completed_cache_and_retries_only_infra_error(
    tmp_path: Path,
) -> None:
    output = tmp_path / "runs.jsonl"
    calls: list[str] = []

    def fake_answer(question, _ctx, *, system, runtime, question_id):
        del question, system, runtime
        calls.append(question_id)
        return _result(question_id)

    rows = run_answers(
        [_question("q1")],
        systems=("simple",),
        model_configs=_models(),
        runtime_factory=lambda *_args: _runtime(),
        output_path=output,
        m5_config_hash=M5_CONFIG_HASH,
        answer_fn=fake_answer,
        concurrency=1,
    )
    assert rows[0]["status"] == "completed"
    run_answers(
        [_question("q1")],
        systems=("simple",),
        model_configs=_models(),
        runtime_factory=lambda *_args: _runtime(),
        output_path=output,
        m5_config_hash=M5_CONFIG_HASH,
        answer_fn=fake_answer,
        concurrency=1,
    )
    assert calls == ["q1"]

    records = [json.loads(line) for line in output.read_text().splitlines()]
    records[-1]["status"] = "infra_error"
    output.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
    )
    run_answers(
        [_question("q1")],
        systems=("simple",),
        model_configs=_models(),
        runtime_factory=lambda *_args: _runtime(),
        output_path=output,
        m5_config_hash=M5_CONFIG_HASH,
        answer_fn=fake_answer,
        concurrency=1,
    )
    assert calls == ["q1", "q1"]
    assert json.loads(output.read_text().splitlines()[-1])["infra_attempt"] == 2


def test_runner_records_question_failures_and_continues(tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_answer(_question_text, _ctx, *, system, runtime, question_id):
        del system, runtime
        calls.append(question_id)
        if question_id == "q1":
            raise RuntimeError("secret sk-plainmustnotappear")
        return _result(question_id)

    rows = run_answers(
        [_question("q1"), _question("q2")],
        systems=("simple",),
        model_configs=_models(),
        runtime_factory=lambda *_args: _runtime(),
        output_path=tmp_path / "runs.jsonl",
        m5_config_hash=M5_CONFIG_HASH,
        answer_fn=fake_answer,
        concurrency=1,
    )

    assert calls == ["q1", "q2"]
    assert [row["status"] for row in rows] == ["infra_error", "completed"]
    assert "sk-plainmustnotappear" not in (tmp_path / "runs.jsonl").read_text()


def test_runner_billing_is_cached_but_fatal_and_budget_stop(tmp_path: Path) -> None:
    billing_calls = 0

    def billing(*_args, **_kwargs):
        nonlocal billing_calls
        billing_calls += 1
        raise BillingUncertainError("maybe billed")

    output = tmp_path / "billing.jsonl"
    kwargs = {
        "questions": [_question("q1")],
        "systems": ("simple",),
        "model_configs": _models(),
        "runtime_factory": lambda *_args: _runtime(),
        "output_path": output,
        "m5_config_hash": M5_CONFIG_HASH,
        "answer_fn": billing,
        "concurrency": 1,
    }
    assert run_answers(**kwargs)[0]["status"] == "billing_uncertain"
    assert run_answers(**kwargs)[0]["status"] == "billing_uncertain"
    assert billing_calls == 1

    with pytest.raises(FatalLLMError):
        run_answers(
            [_question("q2")],
            systems=("simple",),
            model_configs=_models(),
            runtime_factory=lambda *_args: _runtime(),
            output_path=tmp_path / "fatal.jsonl",
            m5_config_hash=M5_CONFIG_HASH,
            answer_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                FatalLLMError("auth")
            ),
            concurrency=1,
        )

    answer_calls = 0

    def should_not_call(*_args, **_kwargs):
        nonlocal answer_calls
        answer_calls += 1
        return _result("q3")

    with pytest.raises(BudgetExceededError):
        run_answers(
            [_question("q3")],
            systems=("simple",),
            model_configs=_models(),
            runtime_factory=lambda *_args: _runtime(),
            output_path=tmp_path / "budget.jsonl",
            m5_config_hash=M5_CONFIG_HASH,
            answer_fn=should_not_call,
            pre_call_check=lambda *_args: (_ for _ in ()).throw(
                BudgetExceededError("over")
            ),
            concurrency=1,
        )
    assert answer_calls == 0


def test_runner_checks_m5_hash_and_flushes_safe_progress(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(ValueError, match="M5 config hash"):
        run_answers(
            [_question("q1")],
            systems=("simple",),
            model_configs=_models(),
            runtime_factory=lambda *_args: _runtime(),
            output_path=tmp_path / "bad.jsonl",
            m5_config_hash="wrong",
            answer_fn=lambda *_args, **_kwargs: _result("q1"),
            concurrency=1,
        )

    run_answers(
        [_question("q1")],
        systems=("simple",),
        model_configs=_models(),
        runtime_factory=lambda *_args: _runtime(),
        output_path=tmp_path / "ok.jsonl",
        m5_config_hash=M5_CONFIG_HASH,
        answer_fn=lambda *_args, **_kwargs: _result("q1"),
        concurrency=1,
        progress_every=1,
    )
    output = capsys.readouterr().out
    assert "M6 progress" in output
    assert "질문-q1" not in output
    assert "token" not in output.casefold()


def test_rows_record_dropped_citations_and_finalizer_hash(tmp_path: Path) -> None:
    dropped = [{"kind": "doc", "doc_id": "d1", "section_key": "-", "call_id": None}]

    def fake_answer(question, _ctx, *, system, runtime, question_id):
        del question, system, runtime
        result = _result(question_id)
        return AnswerResult(
            result.answer,
            result.citations,
            result.refused,
            result.refuse_reason,
            {**result.trace, "dropped_citations": dropped},
        )

    rows = run_answers(
        [_question("q1")],
        systems=("simple",),
        model_configs=_models(),
        runtime_factory=lambda *_args: _runtime(),
        output_path=tmp_path / "runs.jsonl",
        m5_config_hash=M5_CONFIG_HASH,
        answer_fn=fake_answer,
        concurrency=1,
    )

    assert rows[0]["dropped_citations"] == dropped
    assert rows[0]["finalizer_hash"] == finalizer_hash()


def test_cache_is_not_reused_across_finalizer_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "runs.jsonl"
    calls: list[str] = []

    def fake_answer(question, _ctx, *, system, runtime, question_id):
        del question, system, runtime
        calls.append(question_id)
        return _result(question_id)

    def run() -> None:
        run_answers(
            [_question("q1")],
            systems=("simple",),
            model_configs=_models(),
            runtime_factory=lambda *_args: _runtime(),
            output_path=output,
            m5_config_hash=M5_CONFIG_HASH,
            answer_fn=fake_answer,
            concurrency=1,
        )

    run()
    run()
    assert calls == ["q1"]
    monkeypatch.setattr("kodocs.eval.m6_runner.finalizer_hash", lambda: "f" * 64)
    run()
    assert calls == ["q1", "q1"]
