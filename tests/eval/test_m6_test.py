from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kodocs.agent.finalize import finalizer_hash
from kodocs.agent.guardrails import GuardrailConfig
from kodocs.agent.prompts import prompt_hashes
from kodocs.agent.types import AnswerResult, Citation, canonical_hash
from kodocs.eval.m6_test import (
    ExperimentOutput,
    Preflight,
    execute_experiment,
    main,
    run_test,
)
from kodocs.ingest.manifest import load_records, sha256_file
from kodocs.llm.answer_settings import load_answer_config
from kodocs.llm.openai_chat import FatalLLMError
from kodocs.tools.deps import M5_CONFIG_HASH


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    dev_path = root / "data/manifest/m6_dev.json"
    _write(dev_path, {"fixture": True})
    config = load_answer_config("luna")
    payload = {
        "answer": {
            "model": config.model,
            "settings": config.record(),
            "settings_hash": config.settings_hash(),
        },
        "prompt_hashes": prompt_hashes(),
        "finalizer_hash": finalizer_hash(),
        "guardrails": {
            "settings": GuardrailConfig().record(),
            "settings_hash": GuardrailConfig().settings_hash(),
        },
        "m5_config_hash": M5_CONFIG_HASH,
        "code_commit": "fixture",
        "m6_dev_sha256": sha256_file(dev_path),
        "frozen_at": "2026-10-01T00:00:00+09:00",
    }
    payload["config_hash"] = canonical_hash(payload)
    _write(root / "data/manifest/m6_config.json", payload)
    return root


class _Experiment:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def __call__(self, preflight) -> ExperimentOutput:
        self.calls += 1
        if self.fail:
            raise RuntimeError("local failure")
        preflight.raw_dir.mkdir(parents=True, exist_ok=True)
        (preflight.raw_dir / "runs.jsonl").write_text(
            '{"question_id":"g-1"}\n', encoding="utf-8"
        )
        return ExperimentOutput(
            result={"status_counts": {"completed": 60}},
            runs_sha256=sha256_file(preflight.raw_dir / "runs.jsonl"),
        )


def test_m6_test_requires_config_before_experiment(tmp_path: Path) -> None:
    experiment = _Experiment()
    assert main([], root=tmp_path / "missing", experiment_fn=experiment) == 2
    assert experiment.calls == 0


def test_m6_test_lock_requires_rerun_reason(tmp_path: Path) -> None:
    root = _root(tmp_path)
    out = tmp_path / "out"
    experiment = _Experiment()

    run_test(root=root, out_root=out, experiment_fn=experiment)
    first = out / "data/manifest/m6_test_results/run-01.json"
    assert first.is_file()
    assert main([], root=root, out_root=out, experiment_fn=experiment) == 2
    assert experiment.calls == 1

    run_test(
        root=root,
        out_root=out,
        rerun_reason="장애 복구 후 재실행",
        experiment_fn=experiment,
    )
    records = load_records(out / "data/manifest/m6_test_runs.jsonl")
    assert records[-1]["seq"] == 2
    assert records[-1]["previous_result_sha256"] == sha256_file(first)


def test_failed_attempt_does_not_consume_lock(tmp_path: Path) -> None:
    root = _root(tmp_path)
    out = tmp_path / "out"
    with pytest.raises(RuntimeError, match="local failure"):
        run_test(root=root, out_root=out, experiment_fn=_Experiment(fail=True))
    records = load_records(out / "data/manifest/m6_test_runs.jsonl")
    assert records[-1]["status"] == "failed"
    assert records[-1]["seq"] == 1

    run_test(root=root, out_root=out, experiment_fn=_Experiment())
    records = load_records(out / "data/manifest/m6_test_runs.jsonl")
    assert records[-1]["status"] == "completed"
    assert records[-1]["seq"] == 1


def _preflight(tmp_path: Path) -> Preflight:
    out = tmp_path / "out"
    return Preflight(
        root=Path("."),
        output_root=out,
        config={"config_hash": "h"},
        seq=1,
        run_id="m6-test-01",
        ran_at="2026-10-01T00:00:00+09:00",
        rerun_reason=None,
        previous_result_sha256=None,
        result_path=out / "data/manifest/m6_test_results/run-01.json",
        raw_dir=out / "data/interim/m6/test/run-01",
        runs_path=out / "data/manifest/m6_test_runs.jsonl",
    )


def _fake_answer(calls: list[tuple[str, str]], *, fail_on: str | None = None):
    def fake(question, _ctx, *, system, runtime, question_id):
        del question, runtime
        calls.append((question_id, system))
        if fail_on is not None and question_id == fail_on:
            raise FatalLLMError("stop")
        return AnswerResult(
            answer="답",
            citations=(Citation(kind="doc", doc_id="d1"),),
            refused=False,
            refuse_reason=None,
            trace={
                "trace_id": f"t-{question_id}",
                "steps": [],
                "timings": {"total_ms": 1.0},
                "guardrail": {"steps_used": 1, "tokens_used": 5},
            },
        )

    return fake


def _runtime():
    return SimpleNamespace(
        m5_config_hash=M5_CONFIG_HASH,
        guardrails=SimpleNamespace(
            settings_hash=lambda: GuardrailConfig().settings_hash()
        ),
    )


def test_experiment_runs_gold_and_silver_sets_in_one_unit_with_separate_files(
    tmp_path: Path,
) -> None:
    preflight = _preflight(tmp_path)
    calls: list[tuple[str, str]] = []

    output = execute_experiment(
        preflight,
        runtime_factory=lambda *_args: _runtime(),
        model_config=load_answer_config("luna"),
        init_s=0.0,
        answer_fn=_fake_answer(calls),
        concurrency=1,
    )

    assert len(calls) == (30 + 83) * 2
    gold_path = preflight.raw_dir / "runs.jsonl"
    silver_path = preflight.raw_dir / "silver/runs.jsonl"
    gold_rows = [json.loads(line) for line in gold_path.read_text().splitlines()]
    silver_rows = [json.loads(line) for line in silver_path.read_text().splitlines()]
    assert len(gold_rows) == 60 and len(silver_rows) == 166
    assert {row["question_id"][:2] for row in gold_rows} == {"g-"}
    assert {row["question_id"][:2] for row in silver_rows} == {"s-"}
    assert all(row["run_seq"] == 1 for row in (*gold_rows, *silver_rows))
    # Gold keeps its existing top-level result shape; silver is a separate block.
    result = output.result
    assert result["question_count"] == 30 and result["execution_count"] == 60
    assert result["silver_test"]["question_count"] == 83
    assert result["silver_test"]["execution_count"] == 166
    assert result["set_counts"] == {
        "gold_test": {"question_count": 30, "execution_count": 60},
        "silver_test": {"question_count": 83, "execution_count": 166},
    }
    assert output.runs_sha256 == sha256_file(gold_path)
    assert result["silver_runs_sha256"] == sha256_file(silver_path)
    assert sum(row["h5_pool"] for row in gold_rows) == 13 * 2
    assert not any(row["h5_pool"] for row in silver_rows)


def test_a_silver_failure_fails_the_whole_run_without_consuming_the_lock(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    queries = root / "data/golden/queries"
    queries.mkdir(parents=True)
    for name in ("gold.jsonl", "silver.jsonl"):
        (queries / name).write_bytes(Path("data/golden/queries", name).read_bytes())
    out = tmp_path / "out"
    calls: list[tuple[str, str]] = []
    silver_id = next(
        json.loads(line)["id"]
        for line in (queries / "silver.jsonl").read_text().splitlines()
        if json.loads(line)["split"] == "test"
    )

    def experiment(preflight):
        return execute_experiment(
            preflight,
            runtime_factory=lambda *_args: _runtime(),
            model_config=load_answer_config("luna"),
            init_s=0.0,
            answer_fn=_fake_answer(calls, fail_on=silver_id),
            concurrency=1,
        )

    with pytest.raises(FatalLLMError):
        run_test(root=root, out_root=out, experiment_fn=experiment)

    # All gold rows ran before the silver set stopped the unit.
    assert {qid[:2] for qid, _system in calls} == {"g-", "s-"}
    records = load_records(out / "data/manifest/m6_test_runs.jsonl")
    assert records[-1]["status"] == "failed"
    assert not (out / "data/manifest/m6_test_results/run-01.json").exists()
