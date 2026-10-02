"""Committed H8 integrity is mandatory; local raw output is optional."""

import hashlib
import json

import pytest
from kodocs.eval.m8_contract import (
    FREEZE_PATH,
    LATENCY_PATH,
    RESULT_PATH,
    RESULTS_DIR,
    RUNS_PATH,
    ContractError,
    H8ResultError,
    load_h8_state,
    validate_latency,
    validate_result,
)


def put(root, path, payload):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def completed(root, run=1, raw_present=False):
    freeze = put(root, FREEZE_PATH, {"selected": {"depth": 20, "fusion": "단독"}})
    raw_bytes = b'{"question_id":"fixture"}\n'
    raw = {"sha256": hashlib.sha256(raw_bytes).hexdigest(), "rows": 1}
    payload = {
        "h8": {
            "verdict": "H8 채택",
            "diff": 0.123,
            "ci_low": 0.02,
            "ci_high": 0.2,
            "n": 21,
        },
        "gold": {
            "control": {"ndcg10": 0.5, "recall10": 0.6},
            "reranked": {"ndcg10": 0.623, "recall10": 0.7},
        },
        "silver": {
            "control": {"ndcg10": 0.4, "recall10": 0.5},
            "reranked": {"ndcg10": 0.6, "recall10": 0.7},
        },
        "harm": {"gold": 2, "silver": 3},
        "selected": {"depth": 20, "fusion": "단독"},
        "max_length": 512,
        "truncated_ratio": 0.01,
        "device": "mps",
        "raw": raw,
        "config_hash": hashlib.sha256(freeze.read_bytes()).hexdigest(),
    }
    path = RESULTS_DIR / f"run-{run:02d}.json"
    result = put(root, path, payload)
    record = {
        "seq": run,
        "status": "completed",
        "result_path": path.as_posix(),
        "result_sha256": hashlib.sha256(result.read_bytes()).hexdigest(),
        "raw": raw,
    }
    runs = root / RUNS_PATH
    runs.parent.mkdir(parents=True, exist_ok=True)
    with runs.open("a") as handle:
        handle.write(json.dumps(record) + "\n")
    if raw_present:
        local = root / f"data/interim/m8/test/run-{run:02d}/runs.jsonl"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(raw_bytes)
    return payload


def test_not_run_and_freeze_only(tmp_path):
    assert load_h8_state(tmp_path).status == "not_run"
    put(tmp_path, FREEZE_PATH, {})
    assert load_h8_state(tmp_path).status == "not_run"


@pytest.mark.parametrize("raw_present", [True, False])
def test_completed(tmp_path, raw_present):
    payload = completed(tmp_path, raw_present=raw_present)
    state = load_h8_state(tmp_path)
    assert state.status == "result"
    assert state.result == payload
    assert state.local_raw_checked is raw_present


def test_latest_completed_and_first_run(tmp_path):
    first = completed(tmp_path)
    latest = completed(tmp_path, run=2)
    with (tmp_path / RUNS_PATH).open("a") as handle:
        handle.write(json.dumps({"seq": 3, "status": "stopped"}) + "\n")
    state = load_h8_state(tmp_path)
    assert state.first_result == first
    assert state.result == latest
    assert state.result_path.name == "run-02.json"


@pytest.mark.parametrize("trace", ["directory", "record", "file"])
def test_incomplete_is_error(tmp_path, trace):
    if trace == "directory":
        (tmp_path / RESULTS_DIR).mkdir(parents=True)
    elif trace == "record":
        put(tmp_path, RUNS_PATH, {"status": "completed"})
    else:
        put(tmp_path, RESULT_PATH, {})
    with pytest.raises(H8ResultError):
        load_h8_state(tmp_path)


@pytest.mark.parametrize(
    "damage", ["result", "config", "raw", "rows", "local", "first"]
)
def test_mismatch_is_error(tmp_path, damage):
    completed(tmp_path, raw_present=True)
    if damage == "result":
        (tmp_path / RESULT_PATH).write_text("{}")
    elif damage == "config":
        (tmp_path / FREEZE_PATH).write_text("{}")
    elif damage in {"raw", "rows"}:
        record = json.loads((tmp_path / RUNS_PATH).read_text())
        record["raw"]["sha256" if damage == "raw" else "rows"] = (
            "a" * 64 if damage == "raw" else 2
        )
        (tmp_path / RUNS_PATH).write_text(json.dumps(record) + "\n")
    elif damage == "first":
        completed(tmp_path, run=2)
        (tmp_path / RESULT_PATH).write_text("{}")
    else:
        (tmp_path / "data/interim/m8/test/run-01/runs.jsonl").write_text("changed\n")
    with pytest.raises(H8ResultError):
        load_h8_state(tmp_path)


def test_schema_requires_nested_keys(tmp_path):
    payload = completed(tmp_path)
    del payload["gold"]["control"]["ndcg10"]
    with pytest.raises(ContractError, match="gold.control.ndcg10"):
        validate_result(payload)


@pytest.mark.parametrize(
    "key,value",
    [("config_hash", "invalid"), ("truncated_ratio", 2), ("max_length", True)],
)
def test_schema_rejects_invalid_values(tmp_path, key, value):
    payload = completed(tmp_path)
    payload[key] = value
    with pytest.raises(ContractError):
        validate_result(payload)


def test_latency_contract():
    validate_latency(
        {
            "rerank_s": {"p50_s": 0.1, "p95_s": 0.2, "max_s": 0.3},
            "conservative_sum_s": 4,
            "margin_to_5s": 1,
            "simple_p95_m7": 3.8,
        }
    )
    with pytest.raises(ContractError, match="rerank_s.p95_s"):
        validate_latency({"rerank_s": {"p50_s": 0.1}})
    assert LATENCY_PATH.name == "m8_latency.json"
