"""Offline M7 latency contracts, real frozen settings and fake answers/clock."""

import json
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from kodocs.agent.finalize import finalizer_hash
from kodocs.agent.prompts import prompt_hashes
from kodocs.agent.types import AgentRuntime, AnswerResult, ModelAnswer
from kodocs.costs import BudgetExceededError
from kodocs.eval import m7_latency as latency
from kodocs.eval.m7_rawhash import RawHashMismatch, fingerprint
from kodocs.ingest.manifest import sha256_file
from kodocs.llm.answer_settings import load_answer_config
from kodocs.tools.deps import M5_CONFIG_HASH


@pytest.fixture
def project(tmp_path):
    for name in (
        "m6_config.json",
        "m6_dev.json",
        "m5_config.json",
        "m6_eval_inputs.json",
        "m6_test_runs.jsonl",
    ):
        dst = tmp_path / "data/manifest" / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(Path("data/manifest") / name, dst)
    for name in ("gold.jsonl", "silver.jsonl"):
        dst = tmp_path / "data/golden/queries" / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(Path("data/golden/queries") / name, dst)
    for prefix in ("", "silver/"):
        dst = tmp_path / f"data/interim/m6/test/run-01/{prefix}runs.jsonl"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(
            "".join(
                json.dumps({"system": s, "cost_usd": 0.001, "model": "gpt-6-luna"})
                + "\n"
                for s in ("simple", "agentic")
            )
        )
    result_path = tmp_path / "data/manifest/m6_test_results/run-01.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(
            {
                "runs_sha256": sha256_file(
                    tmp_path / "data/interim/m6/test/run-01/runs.jsonl"
                ),
                "silver_runs_sha256": sha256_file(
                    tmp_path / "data/interim/m6/test/run-01/silver/runs.jsonl"
                ),
            }
        )
    )
    record_path = tmp_path / "data/manifest/m6_test_runs.jsonl"
    records = latency.load_rows(record_path)
    records[0]["result_sha256"] = sha256_file(result_path)
    record_path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return tmp_path


class FakeChat:
    def __init__(self):
        self.calls = []

    def invoke(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return SimpleNamespace(ttft_ms=1000)


def runtime_factory(name, config):
    return AgentRuntime(
        deps=SimpleNamespace(embedder=SimpleNamespace(settings={"device": "cpu"})),
        chat=FakeChat(),
        model_record=config.record(),
        m5_config_hash=M5_CONFIG_HASH,
    )


def test_m7_latency_adapter_only_final_calls_and_frozen_hashes():
    inner = FakeChat()
    chat = latency.StreamingChat(inner)
    chat.invoke("choose", tools=[{"name": "search_docs"}])
    chat.invoke("final", response_schema=ModelAnswer)
    assert "stream" not in inner.calls[0][1]
    assert inner.calls[1][1]["stream"] is True
    chat.invoke("choose", stream=True)
    assert "stream" not in inner.calls[2][1]
    config = json.loads(Path("data/manifest/m6_config.json").read_text())
    assert (
        config["answer"]["settings_hash"] == load_answer_config("luna").settings_hash()
    )
    assert config["prompt_hashes"] == prompt_hashes()
    assert config["finalizer_hash"] == finalizer_hash()


@pytest.mark.parametrize("n,expected", [(113, 108), (100, 95), (1, 1)])
def test_m7_latency_nearest_rank(n, expected):
    assert latency.nearest_rank(list(reversed(range(1, n + 1))), 0.95) == expected
    assert latency.nearest_rank([5.0] * n, 0.95) == 5.0


@pytest.mark.parametrize(
    "value, verdict", [(5.0, "P95 목표 달성"), (5.0001, "P95 목표 미달")]
)
def test_m7_latency_statistics_include_failures(value, verdict):
    rows = [
        {
            "system": s,
            "total_s": value,
            "embed_s": None,
            "search_s": 0.1,
            "ttft_s": None,
            "status": "failed",
            "refuse_reason": "guardrail_timeout",
        }
        for s in ("simple", "agentic")
        for _ in range(113)
    ]
    result = latency.summarize(rows)
    assert result["simple"]["verdict"] == verdict
    assert result["simple"]["failure_count"] == 113
    assert result["simple"]["segments"]["embed_s"]["missing_count"] == 113
    assert result["simple"]["p95_ci95_s"] == [value, value]
    for s in result.values():
        for segment in s["segments"].values():
            assert set(segment) == {"n", "missing_count", "p50_s", "p95_s", "max_s"}
    assert "verdict" not in result["agentic"]


@pytest.mark.parametrize("concurrency", [0, 2, 4, True, 1.0])
def test_m7_latency_concurrency_rejected_before_init(concurrency, tmp_path):
    with pytest.raises(latency.ConcurrencyError):
        latency.run_latency(
            root=tmp_path,
            runtime_factory=lambda *a: pytest.fail(),
            concurrency=concurrency,
        )


def test_m7_latency_warmup_rawhash_lock_history_and_stop(project):
    ticks = [0.0]
    calls = []

    def answer(question, user, *, system, runtime, question_id):
        calls.append((system, question_id))
        ticks[0] += 50 if len(calls) <= 6 else 5
        return AnswerResult(
            "",
            (),
            True,
            "guardrail_timeout",
            {"timings": {"embed_ms": 200, "search_ms": 300, "ttft_ms": 1000}},
        )

    def factory(*args):
        ticks[0] += 10
        return runtime_factory(*args)

    options = {
        "root": project,
        "runtime_factory": factory,
        "answer_fn": answer,
        "clock": lambda: ticks[0],
        "environment_fn": lambda runtime: {"fake": True},
    }
    latency.write_dry_run(root=project)
    result = latency.run_latency(**options)
    assert result["status"] == "completed"
    assert result["init_s"] == 10
    assert result["concurrency"] == 1 and result["streaming"] is True
    assert result["warmup_excluded_count"] == 6
    assert len(calls) == 232
    assert result["systems"]["simple"]["execution_count"] == 113
    assert result["systems"]["simple"]["p95_s"] == 5
    assert result["systems"]["simple"]["failure_count"] == 113
    assert result["raw"][-1]["rows"] == 226
    assert (
        result["raw"][-1]["sha256"] == fingerprint(project / latency.RAW_PATH)["sha256"]
    )
    rows = latency.recalculate(root=project)
    assert rows == result["systems"]
    raw_rows = latency.load_rows(project / latency.RAW_PATH)
    assert raw_rows[0]["embed_s"] == 0.2
    assert raw_rows[0]["ttft_s"] == 1
    for system in ("simple", "agentic"):
        ids = [r["question_id"] for r in raw_rows if r["system"] == system]
        assert ids == sorted(ids)
    with pytest.raises(latency.LatencyLockError):
        latency.run_latency(**options)
    rerun = latency.run_latency(**options, rerun_reason="환경 재측정")
    assert len(rerun["history"]) == 2
    assert rerun["history"][0] == result["history"][0]
    assert (
        rerun["history"][1]["previous_result_sha256"]
        == result["history"][0]["result_sha256"]
    )
    assert rerun["history"][1]["rerun_reason"] == "환경 재측정"
    (project / latency.RAW_PATH).write_text("{}\n")
    with pytest.raises(RawHashMismatch):
        latency.recalculate(root=project)
    with pytest.raises(RawHashMismatch):
        latency.run_latency(**options, rerun_reason="변조 후 거부")


def test_m7_latency_stopped_registers_raw_and_locks(project):
    count = [0]

    def answer(*args, **kwargs):
        count[0] += 1
        if count[0] == 8:
            raise KeyboardInterrupt()
        return AnswerResult("", (), False, None, {"timings": {}})

    latency.write_dry_run(root=project)
    with pytest.raises(KeyboardInterrupt):
        latency.run_latency(
            root=project,
            runtime_factory=runtime_factory,
            answer_fn=answer,
            environment_fn=lambda r: {},
        )
    result = json.loads((project / latency.RESULTS_PATH).read_text())
    assert result["status"] == "stopped"
    assert result["raw"][-1]["rows"] == 2
    assert (
        result["raw"][-1]["sha256"] == fingerprint(project / latency.RAW_PATH)["sha256"]
    )
    assert "systems" not in result
    with pytest.raises(latency.LatencyLockError):
        latency.run_latency(root=project, runtime_factory=runtime_factory)
    with pytest.raises(ValueError, match="completed"):
        latency.recalculate(root=project)


def test_m7_latency_dry_run_offline(project, monkeypatch):
    monkeypatch.setattr(httpx.Client, "get", lambda *a, **k: pytest.fail("network"))
    result = latency.write_dry_run(root=project)
    assert result["execution_count"] == 232
    assert result["measured_execution_count"] == 226
    assert result["warmup_execution_count"] == 6
    assert result["estimated_cost_usd"] == pytest.approx(0.232)
    assert result["remaining_monthly_budget_usd"] >= 0
    assert latency.load_rows(project / latency.DRY_PATH)[-1]["kind"] == "latency"
    cfg = project / "data/manifest/m6_config.json"
    cfg.write_text("{}")
    with pytest.raises(ValueError, match="M6"):
        latency.write_dry_run(root=project)


def test_m7_latency_environment_local_readonly_and_missing_reasons():
    urls = []

    def handler(request):
        urls.append(str(request.url))
        assert request.method == "GET"
        if request.url.path == "/":
            return httpx.Response(200, json={"version": {"number": "3.2.0"}})
        return httpx.Response(
            200,
            json={
                "nodes": {
                    "n": {
                        "jvm": {
                            "mem": {"heap_init_in_bytes": 10, "heap_max_in_bytes": 20},
                            "input_arguments": ["-Xms1g", "-Xmx1g"],
                        }
                    }
                }
            },
        )

    config = load_answer_config("luna")
    runtime = runtime_factory("luna", config)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        env = latency.collect_environment(runtime, client=client)
    assert env["opensearch_version"] == "3.2.0"
    assert env["embedding_device"] == "cpu"
    assert env["opensearch_heap"]["n"]["heap_max_in_bytes"] == 20
    assert all(u.startswith("http://127.0.0.1:9200/") for u in urls)
    assert all(k in env for k in ("python", "packages", "machine", "os", "reasons"))
    assert all(
        k in env["packages"]
        for k in ("langgraph", "langchain-openai", "openai", "mcp", "google-genai")
    )
    with pytest.raises(ValueError, match="localhost"):
        latency.collect_environment(runtime, base_url="https://example.com")

    def fail(request):
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        env = latency.collect_environment(replace(runtime, deps=None), client=client)
    for field in ("opensearch_version", "opensearch_heap", "embedding_device"):
        assert env[field] is None
        assert field in env["reasons"]


@pytest.mark.parametrize(
    "error_type", [RuntimeError, latency.BillingUncertainError, BudgetExceededError]
)
def test_m7_latency_exception_wall_time_and_missing_segments(error_type):
    ticks = [0.0]

    def answer(*args, **kwargs):
        ticks[0] += 47.0
        raise error_type("safe fake")

    q = latency.load_m6_questions(Path("."), split="test")[0]
    row, stop = latency._attempt(
        q,
        "simple",
        runtime_factory("luna", load_answer_config("luna")),
        answer,
        lambda: ticks[0],
        1,
    )
    assert row["total_s"] == 47.0
    assert all(row[s] is None for s in latency.SEGMENTS)
    assert row["status"] != "completed"
    assert bool(stop) == (error_type is BudgetExceededError)


def test_m7_latency_init_failure_preserves_empty_raw_and_environment(project):
    latency.write_dry_run(root=project)

    def factory(*args):
        raise ValueError("fake model loading failure")

    seen = []

    def environment(runtime):
        seen.append(runtime)
        return {
            "embedding_device": None,
            "reasons": {"embedding_device": "init failed"},
        }

    with pytest.raises(ValueError, match="loading"):
        latency.run_latency(
            root=project, runtime_factory=factory, environment_fn=environment
        )
    manifest = json.loads((project / latency.RESULTS_PATH).read_text())
    assert manifest["status"] == "stopped"
    assert manifest["init_s"] is not None
    assert manifest["raw"][-1]["rows"] == 0
    assert manifest["environment"]["embedding_device"] is None
    assert seen == [None]


def test_m7_latency_cli_has_no_concurrency_option():
    with pytest.raises(SystemExit) as exc:
        latency.main(["--concurrency", "2", "--dry-run"])
    assert exc.value.code == 2


def test_m7_latency_requires_dry_run_and_authenticates_basis(project):
    with pytest.raises(ValueError, match="dry"):
        latency.run_latency(
            root=project, runtime_factory=lambda *a: pytest.fail("init")
        )
    basis = project / "data/interim/m6/test/run-01/runs.jsonl"
    basis.write_text("{}\n")
    with pytest.raises(ValueError, match="hash"):
        latency.write_dry_run(root=project)


def test_m7_latency_history_tampering_refused(project):
    latency.write_dry_run(root=project)
    answer = lambda *a, **k: AnswerResult("", (), False, None, {"timings": {}})
    latency.run_latency(
        root=project,
        runtime_factory=runtime_factory,
        answer_fn=answer,
        environment_fn=lambda r: {},
    )
    path = project / latency.RESULTS_PATH
    manifest = json.loads(path.read_text())
    manifest["history"][0]["result"]["init_s"] = -1
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="history"):
        latency.recalculate(root=project)
