"""Shared committed H8 contract for the README and the future test runner.

Hashes are SHA-256 of file bytes, including FREEZE_PATH (no self-hash).
Completed JSONL records require seq, status, result_path, result_sha256 and
raw={sha256, rows}. result_path is repository-relative within RESULTS_DIR.
Metrics use gold/silver.{control,reranked}.{ndcg10,recall10}.
"""

import hashlib
import json
import math
import re
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from kodocs.eval.m7_rawhash import fingerprint

FREEZE_PATH = Path("data/manifest/m8_config.json")
RESULTS_DIR = Path("data/manifest/m8_test_results")
RESULT_PATH = RESULTS_DIR / "run-01.json"
RUNS_PATH = Path("data/manifest/m8_test_runs.jsonl")
LATENCY_PATH = Path("data/manifest/m8_latency.json")
required_result_keys = {
    "h8": ("verdict", "diff", "ci_low", "ci_high", "n"),
    "gold": {"control": ("ndcg10", "recall10"), "reranked": ("ndcg10", "recall10")},
    "silver": {"control": ("ndcg10", "recall10"), "reranked": ("ndcg10", "recall10")},
    "harm": ("gold", "silver"),
    "selected": ("depth", "fusion"),
    "max_length": (),
    "truncated_ratio": (),
    "device": (),
    "raw": ("sha256", "rows"),
    "config_hash": (),
}
required_latency_keys = {
    "rerank_s": ("p50_s", "p95_s", "max_s"),
    "conservative_sum_s": (),
    "margin_to_5s": (),
    "simple_p95_m7": (),
}


class ContractError(ValueError):
    """Missing or malformed committed fields."""


class H8ResultError(ContractError):
    """Execution traces exist but their result cannot be authenticated."""


@dataclass(frozen=True)
class H8State:
    status: str
    result: dict | None = None
    first_result: dict | None = None
    result_path: Path | None = None
    local_raw_checked: bool = False
    first_local_raw_checked: bool = False
    latency: dict | None = None


def _keys(payload, schema, prefix=""):
    if not isinstance(payload, dict):
        raise ContractError(f"{prefix or 'payload'}: object 필요")
    for key, nested in schema.items():
        name = prefix + key
        if key not in payload:
            raise ContractError(f"필수 키 없음: {name}")
        if nested:
            _keys(
                payload[key],
                nested if isinstance(nested, dict) else dict.fromkeys(nested, ()),
                name + ".",
            )


def _number(value, name, *, integer=False, minimum=None, maximum=None):
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or (integer and type(value) is not int)
        or (minimum is not None and value < minimum)
        or (maximum is not None and value > maximum)
    ):
        raise ContractError(f"{name}: 숫자 형식·범위 오류")


def _digest(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ContractError(f"{name}: SHA-256 형식 오류")


def _raw(value):
    _keys(value, {"sha256": (), "rows": ()}, "raw.")
    _digest(value["sha256"], "raw.sha256")
    _number(value["rows"], "raw.rows", integer=True, minimum=0)


def validate_result(payload) -> None:
    _keys(payload, required_result_keys)
    h8 = payload["h8"]
    if h8["verdict"] not in (
        "H8 채택",
        "H8 기각(반대 방향)",
        "H8 판정 보류(차이 입증 못 함)",
    ):
        raise ContractError("h8.verdict: 판정 문구 오류")
    for key in ("diff", "ci_low", "ci_high"):
        _number(h8[key], "h8." + key, minimum=-1, maximum=1)
    if not h8["ci_low"] <= h8["diff"] <= h8["ci_high"]:
        raise ContractError("h8: CI 순서 오류")
    expected = (
        "H8 채택"
        if h8["ci_low"] > 0
        else "H8 기각(반대 방향)"
        if h8["ci_high"] < 0
        else "H8 판정 보류(차이 입증 못 함)"
    )
    if h8["verdict"] != expected:
        raise ContractError("h8.verdict: CI와 판정 불일치")
    _number(h8["n"], "h8.n", integer=True, minimum=1)
    for scope in ("gold", "silver"):
        for variant in ("control", "reranked"):
            for metric in ("ndcg10", "recall10"):
                _number(
                    payload[scope][variant][metric],
                    f"{scope}.{variant}.{metric}",
                    minimum=0,
                    maximum=1,
                )
        _number(payload["harm"][scope], "harm." + scope, integer=True, minimum=0)
    if (
        payload["selected"]["depth"] not in (20, 30, 50)
        or type(payload["selected"]["depth"]) is not int
    ):
        raise ContractError("selected.depth: 격자 밖")
    if payload["selected"]["fusion"] not in ("단독", "RRF"):
        raise ContractError("selected.fusion: 격자 밖")
    _number(payload["max_length"], "max_length", integer=True, minimum=1, maximum=1024)
    _number(payload["truncated_ratio"], "truncated_ratio", minimum=0, maximum=1)
    if not isinstance(payload["device"], str) or not payload["device"]:
        raise ContractError("device: 문자열 필요")
    _raw(payload["raw"])
    _digest(payload["config_hash"], "config_hash")


def validate_latency(payload) -> None:
    _keys(payload, required_latency_keys)
    for key in ("p50_s", "p95_s", "max_s"):
        _number(payload["rerank_s"][key], "rerank_s." + key, minimum=0)
    for key in ("conservative_sum_s", "margin_to_5s", "simple_p95_m7"):
        _number(payload[key], key)


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _same(actual, expected, field):
    if actual != expected:
        # Never echo full hashes or arbitrary untrusted manifest text.
        detail = (
            f" ({str(actual)[:12]} / {str(expected)[:12]})"
            if "sha256" in field or "hash" in field
            else ""
        )
        raise H8ResultError(f"{field}: 불일치{detail}")


def _checked(root, record):
    _keys(
        record,
        dict.fromkeys(("seq", "status", "result_path", "result_sha256", "raw"), ()),
    )
    _number(record["seq"], "seq", integer=True, minimum=1)
    _digest(record["result_sha256"], "result_sha256")
    _raw(record["raw"])
    path = Path(record["result_path"])
    if path != RESULTS_DIR / f"run-{record['seq']:02d}.json":
        raise H8ResultError("result_path: 실행 번호·경로 불일치")
    result = root / path
    if not result.is_file():
        raise H8ResultError("result_path: 결과 파일 없음")
    _same(_sha(result), record["result_sha256"], "result_sha256")
    payload = json.loads(result.read_bytes())
    validate_result(payload)
    for key in ("sha256", "rows"):
        _same(payload["raw"][key], record["raw"][key], "raw." + key)
    _same(payload["config_hash"], _sha(root / FREEZE_PATH), "config_hash")
    local = root / f"data/interim/m8/test/run-{record['seq']:02d}/runs.jsonl"
    if local.exists():
        actual = fingerprint(local)
        for key in ("sha256", "rows"):
            _same(actual[key], record["raw"][key], "local_raw." + key)
    return payload, path, local.exists()


def load_h8_state(root=Path(".")) -> H8State:
    root = Path(root)
    if not (root / RESULTS_DIR).exists() and not (root / RUNS_PATH).exists():
        return H8State("not_run")
    try:
        records = [
            json.loads(line)
            for line in (root / RUNS_PATH).read_text().splitlines()
            if line.strip()
        ]
        completed = [r for r in records if r["status"] == "completed"]
        if not completed or not (root / RESULT_PATH).is_file():
            raise H8ResultError("완료 기록 또는 첫 실행 결과 파일 없음")
        if completed[0]["seq"] != 1:
            raise H8ResultError("첫 실행 completed 기록 없음")
        if any(a["seq"] >= b["seq"] for a, b in pairwise(completed)):
            raise H8ResultError("completed 실행 번호 순서 오류")
        first, _, first_checked = _checked(root, completed[0])
        latest, path, checked = _checked(root, completed[-1])
        latency = None
        if (root / LATENCY_PATH).exists():
            latency = json.loads((root / LATENCY_PATH).read_bytes())
            validate_latency(latency)
        return H8State("result", latest, first, path, checked, first_checked, latency)
    except H8ResultError:
        raise
    except ContractError as exc:
        raise H8ResultError(str(exc)) from exc
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        raise H8ResultError(f"H8 파일·기록 읽기 실패: {type(exc).__name__}") from exc
