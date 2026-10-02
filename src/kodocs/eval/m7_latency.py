"""M7-P1~P4: serial streaming latency measurement of the frozen M6 systems."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import os
import platform
import random
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from kodocs.agent.answer import answer
from kodocs.agent.types import ChatLike, canonical_hash
from kodocs.config import load_settings
from kodocs.costs import (
    DEFAULT_LEDGER_PATH,
    BudgetExceededError,
    BudgetGuard,
    CostLedger,
)
from kodocs.eval.m6_config import CONFIG_PATH, verify_config
from kodocs.eval.m6_dev import SYSTEMS, _git_commit
from kodocs.eval.m6_inputs import load_m6_questions, require_input_manifest
from kodocs.eval.m6_runner import _append_record, _load_records, require_m5_config
from kodocs.eval.m6_test import _selected_model_config
from kodocs.eval.m7_rawhash import fingerprint, record_fingerprint, verify_fingerprint
from kodocs.ingest.manifest import now_iso, sha256_file
from kodocs.llm.openai_chat import BillingUncertainError, FatalLLMError
from kodocs.tools import UserContext, build_tool_deps
from kodocs.tools.deps import M5_CONFIG_HASH

RESULTS_PATH = Path("data/manifest/m7_latency.json")
RAW_PATH = Path("data/interim/m7/latency/runs.jsonl")
DRY_PATH = Path("data/manifest/m7_dry_runs.jsonl")
LOCK_PATH = Path("data/interim/m7/latency/.lock")
SEGMENTS = ("embed_s", "search_s", "ttft_s")
PACKAGE_NAMES = (
    "langgraph",
    "langchain-openai",
    "openai",
    "mcp",
    "google-genai",
    "langchain-core",
    "httpx",
    "torch",
    "FlagEmbedding",
    "opensearch-py",
)


class ConcurrencyError(ValueError):
    """M7 latency permits exactly one simultaneous answer."""


class LatencyLockError(ValueError):
    """A previous attempt or a live process prevents an unqualified run."""


def require_serial(concurrency):
    if type(concurrency) is not int or concurrency != 1:
        raise ConcurrencyError("M7 latency concurrency must be exactly 1")


class StreamingChat:
    """Enable streaming only for the shared final structured-answer call."""

    def __init__(self, inner: ChatLike):
        self.inner = inner

    def invoke(self, messages, *, tools=None, response_schema=None, stream=False):
        kwargs = {}
        if tools is not None:
            kwargs["tools"] = tools
        if response_schema is not None:
            kwargs.update(response_schema=response_schema, stream=True)
        return self.inner.invoke(messages, **kwargs)


def load_rows(path):
    return _load_records(Path(path))


def _read(path):
    return json.loads(Path(path).read_bytes())


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )
    temp.replace(path)


def nearest_rank(values, p):
    """Return the ceil(p*n)th sorted observation, without interpolation."""
    if not 0 < p <= 1:
        raise ValueError("quantile must be in (0, 1]")
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return None
    if any(not math.isfinite(v) or v < 0 for v in ordered):
        raise ValueError("latency must be finite and non-negative")
    return ordered[math.ceil(p * len(ordered)) - 1]


def _statistics(values):
    observed = [v for v in values if v is not None]
    return {
        "n": len(observed),
        "missing_count": len(values) - len(observed),
        "p50_s": nearest_rank(observed, 0.5),
        "p95_s": nearest_rank(observed, 0.95),
        "max_s": max(observed) if observed else None,
    }


def _p95_ci(values):
    rng = random.Random(20261001)
    estimates = [
        nearest_rank(rng.choices(values, k=len(values)), 0.95) for _ in range(10_000)
    ]
    return [nearest_rank(estimates, 0.025), nearest_rank(estimates, 0.975)]


def summarize(rows):
    """Include every failure/guardrail; only simple total latency has a target."""
    result = {}
    for system in SYSTEMS:
        selected = [r for r in rows if r["system"] == system]
        if len(selected) != 113 or any(r.get("warmup") for r in selected):
            raise ValueError(
                "completed measurement requires 113 non-warmup rows per system"
            )
        total = [r["total_s"] for r in selected]
        p95 = nearest_rank(total, 0.95)
        result[system] = {
            "execution_count": len(selected),
            "failure_count": sum(r["status"] != "completed" for r in selected),
            "segments": {
                k: _statistics([r.get(k) for r in selected]) for k in SEGMENTS
            },
            "total": _statistics(total),
            "p95_s": p95,
            "p95_ci95_s": _p95_ci(total),
            "bootstrap_repetitions": 10_000,
            "bootstrap_seed": 20261001,
            "p95_method": "nearest-rank",
            "p95_rank": 108,
        }
        if system == "simple":
            result[system]["verdict"] = (
                "P95 목표 달성" if p95 <= 5.0 else "P95 목표 미달"
            )
            result[system]["target_s"] = 5.0
    return result


def collect_environment(runtime, *, base_url="http://127.0.0.1:9200", client=None):
    """Collect versions and read-only local OpenSearch info, never remote hosts."""
    url = urlsplit(base_url)
    if (
        url.scheme not in {"http", "https"}
        or url.hostname not in {"localhost", "127.0.0.1", "::1"}
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in {"", "/"}
    ):
        raise ValueError("OpenSearch information API must use localhost")
    reasons = {}
    packages = {}
    for package in PACKAGE_NAMES:
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            packages[package] = None
            reasons[f"packages.{package}"] = "PackageNotFoundError"
    env = {
        "python": platform.python_version(),
        "packages": packages,
        "machine": platform.machine() or None,
        "os": platform.platform() or None,
        "embedding_device": None,
        "opensearch_version": None,
        "opensearch_heap": None,
        "reasons": reasons,
    }
    for field in ("machine", "os"):
        if env[field] is None:
            reasons[field] = "platform returned empty value"
    try:
        device = runtime.deps.embedder.settings.get("device")
        if not device:
            raise ValueError()
        env["embedding_device"] = str(device)
    except Exception as exc:  # noqa: BLE001 - device information is optional
        reasons["embedding_device"] = type(exc).__name__
    own_client = client is None
    http = client or httpx.Client(timeout=3, follow_redirects=False, trust_env=False)
    try:
        for field, endpoint in (
            ("opensearch_version", "/"),
            ("opensearch_heap", "/_nodes/jvm"),
        ):
            try:
                response = http.get(base_url.rstrip("/") + endpoint)
                response.raise_for_status()
                body = response.json()
                if field == "opensearch_version":
                    env[field] = body["version"]["number"]
                    if not isinstance(env[field], str) or not env[field]:
                        raise ValueError()
                else:
                    env[field] = {
                        node: {
                            "heap_init_in_bytes": info["jvm"]["mem"][
                                "heap_init_in_bytes"
                            ],
                            "heap_max_in_bytes": info["jvm"]["mem"][
                                "heap_max_in_bytes"
                            ],
                            "heap_arguments": [
                                a
                                for a in info["jvm"].get("input_arguments", [])
                                if a.startswith(("-Xms", "-Xmx"))
                            ],
                        }
                        for node, info in body["nodes"].items()
                    }
                    if not env[field]:
                        raise ValueError()
            except Exception as exc:  # noqa: BLE001 - never record transport text/secrets
                env[field] = None
                reasons[field] = type(exc).__name__
    finally:
        if own_client:
            http.close()
    return env


def preflight(root):
    check = verify_config(root / CONFIG_PATH)
    if not check.ok:
        raise ValueError("M6 config mismatch: " + ", ".join(check.mismatches))
    require_m5_config(root / "data/manifest/m5_config.json")
    inputs = require_input_manifest(root)
    questions = sorted(load_m6_questions(root, split="test"), key=lambda q: q.id)
    gold = [q.id for q in questions if q.set == "gold"]
    silver = [q.id for q in questions if q.set == "silver"]
    # The manifest pins the gold file hash, but lists only its H5 IDs; derive the
    # full gold test IDs from that authenticated file. Silver IDs are explicit.
    if (
        len(gold) != 30
        or len(silver) != 83
        or len({q.id for q in questions}) != 113
        or silver != inputs["silver_test"]["ids"]
        or not set(inputs["h5_pool"]["ids"]).issubset(gold)
    ):
        raise ValueError("M6 test IDs/counts mismatch")
    dev = sorted(load_m6_questions(root, split="dev"), key=lambda q: q.id)
    if [q.id for q in dev] != inputs["dev_set"]["ids"]:
        raise ValueError("M6 dev IDs mismatch")
    config = _read(root / CONFIG_PATH)
    if config["answer"]["model"] != "gpt-6-luna":
        raise ValueError("M7 latency requires the frozen gpt-6-luna")
    return config, questions, dev[:3]


def _budget(root):
    # Explicit env mapping keeps offline commands from opening .env.
    settings = load_settings(env=os.environ)
    return BudgetGuard(
        CostLedger(root / DEFAULT_LEDGER_PATH), settings.monthly_budget_usd
    )


def estimate_latency(root):
    config, questions, warmup = preflight(root)
    records = load_rows(root / "data/manifest/m6_test_runs.jsonl")
    completed = [r for r in records if r["status"] == "completed"]
    if not completed:
        raise ValueError("completed M6 test cost basis missing")
    first = min(completed, key=lambda r: r["seq"])
    result_path = root / first["result_path"]
    if sha256_file(result_path) != first["result_sha256"]:
        raise ValueError("M6 test result hash mismatch")
    result = _read(result_path)
    raw_dir = root / f"data/interim/m6/test/run-{first['seq']:02d}"
    paths = [raw_dir / "runs.jsonl", raw_dir / "silver/runs.jsonl"]
    # Cost basis authenticates the same raw files as M6's first test result.
    hashes = [result["runs_sha256"], result["silver_runs_sha256"]]
    if [sha256_file(p) for p in paths] != hashes:
        raise ValueError("M6 test cost basis raw hash mismatch")
    rows = [
        r
        for p in paths
        for r in load_rows(p)
        if r["model"] == config["answer"]["model"]
    ]
    by_system = {}
    model = _selected_model_config(config)
    for system in SYSTEMS:
        costs = [float(r["cost_usd"]) for r in rows if r["system"] == system]
        if not costs:
            raise ValueError("M6 test mean costs missing")
        mean = math.fsum(costs) / len(costs)
        from kodocs.costs import budget_estimate_usd

        upper = budget_estimate_usd(
            "openai.gpt-6-luna.input", 100_000 + model.max_input_tokens
        ) + budget_estimate_usd(
            "openai.gpt-6-luna.output", 100_000 + model.max_output_tokens
        )
        by_system[system] = {
            "m6_test_mean_usd": mean,
            "n": 116,
            "estimated_cost_usd": mean * 116,
            "upper_cost_usd": upper * 116,
        }
    return {
        "kind": "latency",
        "run_id": "m7-latency",
        "ran_at": now_iso(),
        "config_hash": config["config_hash"],
        "input_hash": canonical_hash(
            {
                "config_hash": config["config_hash"],
                "m6_test_hashes": hashes,
                "ids": [q.id for q in questions],
                "warmup_ids": [q.id for q in warmup],
                "concurrency": 1,
                "streaming": True,
            }
        ),
        "concurrency": 1,
        "streaming": True,
        "execution_count": 232,
        "measured_execution_count": 226,
        "warmup_execution_count": 6,
        "estimated_cost_usd": math.fsum(
            v["estimated_cost_usd"] for v in by_system.values()
        ),
        "upper_cost_usd": math.fsum(v["upper_cost_usd"] for v in by_system.values()),
        "by_system": by_system,
        "basis": "M6 first gold+silver test average; M6 guarded token ceiling",
    }


def write_dry_run(*, root=Path(".")):
    root = Path(root)
    result = estimate_latency(root)
    guard = _budget(root)
    guard.check(result["estimated_cost_usd"])
    result["remaining_monthly_budget_usd"] = guard.remaining_usd()
    result["upper_exceeds_remaining_budget"] = (
        result["upper_cost_usd"] > result["remaining_monthly_budget_usd"]
    )
    _append_record(root / DRY_PATH, result)
    return result


def _verify_history(manifest):
    previous = None
    for entry in manifest.get("history", []):
        if (
            entry["result_sha256"]
            != canonical_hash({k: v for k, v in entry.items() if k != "result_sha256"})
            or entry["previous_result_sha256"] != previous
        ):
            raise ValueError("latency result history hash mismatch")
        previous = entry["result_sha256"]


def _attempt(question, system, runtime, answer_fn, clock, seq):
    started = clock()
    row = {
        "run_seq": seq,
        "system": system,
        "question_id": question.id,
        "status": "completed",
        "refuse_reason": None,
        **{k: None for k in SEGMENTS},
    }
    stop = None
    try:
        result = answer_fn(
            question.question,
            UserContext(question.asker.team, question.asker.grade),
            system=system,
            runtime=runtime,
            question_id=question.id,
        )
        # Capture wall time immediately at return, before parsing the trace.
        row["total_s"] = clock() - started
        row["refuse_reason"] = result.refuse_reason
        if result.refuse_reason and (
            result.refuse_reason.startswith("guardrail_")
            or result.refuse_reason == "parse_error"
        ):
            row["status"] = "failed"
        timings = result.trace.get("timings", {})
        for out, ms in (
            ("embed_s", "embed_ms"),
            ("search_s", "search_ms"),
            ("ttft_s", "ttft_ms"),
        ):
            value = timings.get(out) if out != "ttft_s" else None
            if value is None and timings.get(ms) is not None:
                value = float(timings[ms]) / 1000
            row[out] = value
    except BaseException as exc:  # noqa: BLE001 - persist latency even when user stops
        row["total_s"] = clock() - started
        row["status"] = (
            "billing_uncertain" if isinstance(exc, BillingUncertainError) else "failed"
        )
        row["error_type"] = type(exc).__name__
        if isinstance(exc, (FatalLLMError, BudgetExceededError)) or not isinstance(
            exc, Exception
        ):
            stop = exc
    return row, stop


def run_latency(
    *,
    root=Path("."),
    runtime_factory,
    answer_fn=answer,
    clock=time.perf_counter,
    environment_fn=collect_environment,
    concurrency=1,
    rerun_reason=None,
):
    """Run sequentially with no executor, no configurable CLI concurrency."""
    require_serial(concurrency)
    root = Path(root)
    config, questions, warmup = preflight(root)
    dry = estimate_latency(root)
    matching = [
        r
        for r in load_rows(root / DRY_PATH)
        if r.get("kind") == "latency" and r.get("input_hash") == dry["input_hash"]
    ]
    if not matching:
        raise ValueError("matching make m7-latency-dry required before execution")
    _budget(root).check(dry["estimated_cost_usd"])
    lock = root / LOCK_PATH
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lock.open("x") as handle:
            handle.write(str(os.getpid()))
    except FileExistsError as exc:
        raise LatencyLockError("latency process lock exists") from exc
    try:
        return _execute(
            root,
            config,
            questions,
            warmup,
            matching[-1],
            runtime_factory,
            answer_fn,
            clock,
            environment_fn,
            rerun_reason,
        )
    finally:
        lock.unlink()


def _execute(
    root,
    config,
    questions,
    warmup,
    dry,
    runtime_factory,
    answer_fn,
    clock,
    environment_fn,
    rerun_reason,
):
    manifest_path, raw_path = root / RESULTS_PATH, root / RAW_PATH
    old = _read(manifest_path) if manifest_path.exists() else {}
    _verify_history(old)
    if old:
        verify_fingerprint(manifest_path, "latency", raw_path)
        if not rerun_reason or not rerun_reason.strip():
            raise LatencyLockError(
                "previous latency attempt exists; RERUN_REASON required"
            )
    elif rerun_reason and rerun_reason.strip():
        raise LatencyLockError("first latency run cannot have RERUN_REASON")
    elif raw_path.exists() and raw_path.stat().st_size:
        raise LatencyLockError("unregistered latency raw file exists")
    history = old.get("history", [])
    seq = len(history) + 1
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.touch(exist_ok=True)
    base = {
        "schema_version": 1,
        "status": "running",
        "concurrency": 1,
        "streaming": True,
        "measurement_mode": "스트리밍 모드에서 측정",
        "config_hash": config["config_hash"],
        "model_settings_hash": config["answer"]["settings_hash"],
        "m5_config_hash": M5_CONFIG_HASH,
        "code_commit": _git_commit(root),
        "ran_at": now_iso(),
        "warmup_excluded_count": 6,
        "warmup_ids": [q.id for q in warmup],
        "init_s": None,
        "environment": None,
        "dry_run": dry,
        "raw": old.get("raw", []),
        "history": history,
        "limitation": "외부 API 지연 변동; 단일 실행 점추정, CI는 병기만",
    }
    _write(manifest_path, base)
    status, failure, summary = "stopped", None, None
    try:
        started = clock()
        model = _selected_model_config(config)
        try:
            runtime = runtime_factory("luna", model)
        finally:
            base["init_s"] = clock() - started
        if (
            runtime.m5_config_hash != M5_CONFIG_HASH
            or runtime.guardrails.settings_hash()
            != config["guardrails"]["settings_hash"]
        ):
            raise ValueError("latency runtime frozen settings mismatch")
        runtime = replace(runtime, chat=StreamingChat(runtime.chat))
        base["environment"] = environment_fn(runtime)
        for system in SYSTEMS:
            for q in warmup:
                _, stop = _attempt(q, system, runtime, answer_fn, clock, seq)
                if stop:
                    raise stop
        for system in SYSTEMS:
            for q in questions:
                row, stop = _attempt(q, system, runtime, answer_fn, clock, seq)
                _append_record(raw_path, row)
                if stop:
                    raise stop
        status = "completed"
    except BaseException as exc:
        failure = type(exc).__name__
        raise
    finally:
        base["status"] = status
        base["error_type"] = failure
        if base["environment"] is None:
            base["environment"] = environment_fn(None)
        # Register before any aggregation. Completed and stopped attempts both pin
        # the bytes; all later analysis/report consumers must use recalculate().
        _write(manifest_path, base)
        record_fingerprint(manifest_path, "latency", raw_path, status)
        if status == "completed":
            verify_fingerprint(manifest_path, "latency", raw_path)
            summary = summarize([r for r in load_rows(raw_path) if r["run_seq"] == seq])
        current = _read(manifest_path)
        if summary is not None:
            current["systems"] = summary
        entry = {
            "seq": seq,
            "ran_at": base["ran_at"],
            "status": status,
            "rerun_reason": rerun_reason.strip() if rerun_reason else None,
            "previous_result_sha256": history[-1]["result_sha256"] if history else None,
            "raw": fingerprint(raw_path),
            "result": {k: v for k, v in current.items() if k not in {"raw", "history"}},
        }
        entry["result_sha256"] = canonical_hash(entry)
        current["history"] = history + [entry]
        _write(manifest_path, current)
    return _read(manifest_path)


def recalculate(*, root=Path(".")):
    """Verified statistics accessor for later result-document generation."""
    root = Path(root)
    manifest = _read(root / RESULTS_PATH)
    _verify_history(manifest)
    verify_fingerprint(root / RESULTS_PATH, "latency", root / RAW_PATH)
    if manifest["status"] != "completed":
        raise ValueError("latency run must be completed before analysis/report")
    seq = manifest["history"][-1]["seq"]
    return summarize([r for r in load_rows(root / RAW_PATH) if r["run_seq"] == seq])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rerun-reason", default=os.environ.get("RERUN_REASON"))
    args = parser.parse_args(argv)
    deps = None
    try:
        if args.dry_run:
            result = write_dry_run(root=args.root)
        else:

            def factory(name, config):
                nonlocal deps
                from kodocs.agent.types import AgentRuntime
                from kodocs.llm.openai_chat import ManagedChat

                settings = load_settings(env=os.environ)
                chat = ManagedChat(
                    config,
                    settings,
                    ledger_path=args.root / DEFAULT_LEDGER_PATH,
                    run_id="m7-latency",
                )
                deps = build_tool_deps(root=args.root)
                return AgentRuntime(
                    deps=deps,
                    chat=chat,
                    model_record=config.record()
                    | {"settings_hash": config.settings_hash()},
                    m5_config_hash=M5_CONFIG_HASH,
                    trace_dir=args.root / "logs/traces",
                )

            result = run_latency(
                root=args.root, runtime_factory=factory, rerun_reason=args.rerun_reason
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001 - never print transport text or credentials
        print(f"M7 latency 실행 거부: {type(exc).__name__}")
        return 2
    finally:
        if deps is not None:
            deps.client.close()


if __name__ == "__main__":
    raise SystemExit(main())
