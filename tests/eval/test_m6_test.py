from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodocs.agent.guardrails import GuardrailConfig
from kodocs.agent.prompts import prompt_hashes
from kodocs.agent.types import canonical_hash
from kodocs.eval.m6_test import ExperimentOutput, main, run_test
from kodocs.ingest.manifest import load_records, sha256_file
from kodocs.llm.answer_settings import load_answer_config
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
