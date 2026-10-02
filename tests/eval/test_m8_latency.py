"""Serial local retrieval timing: failed samples remain in statistics."""

from types import SimpleNamespace as NS

import pytest
from test_m8_contract import put

from kodocs.eval import m8_latency as m
from kodocs.eval.m7_latency import ConcurrencyError, LatencyLockError, nearest_rank
from kodocs.eval.m7_rawhash import fingerprint
from kodocs.eval.m8_contract import LATENCY_PATH, validate_latency


def test_summary_reads_m7_p95(tmp_path):
    put(
        tmp_path,
        "data/manifest/m7_latency.json",
        {"systems": {"simple": {"p95_s": 9.876}}},
    )
    rows = [
        {
            "rerank_s": i / 100,
            "embed_s": 0,
            "search_s": 0,
            "status": "failed" if i == 1 else "completed",
        }
        for i in range(113)
    ]
    result = m.summarize(rows, tmp_path)
    validate_latency(result)
    assert result["rerank_s"]["p95_s"] == nearest_rank(
        [r["rerank_s"] for r in rows], 0.95
    )
    assert result["conservative_sum_s"] == 9.876 + result["rerank_s"]["p95_s"]
    assert result["margin_to_5s"] == 5 - result["conservative_sum_s"]
    assert result["failure_count"] == 1
    assert "분포를 합성한 값이 아니다" in result["note"]
    assert "캐시 적중" in result["note"]
    assert "verdict" not in result


def test_concurrency_preflight():
    with pytest.raises(ConcurrencyError):
        m.run_latency(concurrency=2)


def test_warmup_excluded_failure_continues_lock_fingerprint(tmp_path, monkeypatch):
    questions = [NS(id=f"q-{i}") for i in range(113)]
    warmup = [NS(id=f"w-{i}") for i in range(3)]
    monkeypatch.setattr(m, "preflight", lambda root: ({}, questions, warmup))
    put(
        tmp_path, "data/manifest/m7_latency.json", {"systems": {"simple": {"p95_s": 2}}}
    )
    calls = []

    def attempt(q, runtime, clock):
        calls.append(q.id)
        return {
            "question_id": q.id,
            "embed_s": 0,
            "search_s": 0,
            "rerank_s": 0.5,
            "status": "failed" if q.id == "q-0" else "completed",
        }

    kwargs = {
        "root": tmp_path,
        "runtime_factory": lambda c: NS(init_s=0.1, environment={}),
        "attempt_fn": attempt,
    }
    result = m.run_latency(**kwargs)
    assert len(calls) == 116 and result["warmup_ids"] == [q.id for q in warmup]
    assert result["raw"] == fingerprint(tmp_path / m.RAW_PATH)
    assert result["raw"]["rows"] == 113
    assert result["failure_count"] == 1
    with pytest.raises(LatencyLockError):
        m.run_latency(**kwargs)
    m.run_latency(**kwargs, rerun_reason="again")
    assert len(m.read_json(tmp_path / LATENCY_PATH)["history"]) == 2
    assert fingerprint(tmp_path / m.RAW_PATH)["rows"] == 226


def test_reranking_failure_keeps_elapsed_and_no_text(monkeypatch):
    import itertools

    clock = lambda: next(ticks)
    ticks = itertools.count()
    q = NS(id="failure", question="PRIVATE")

    def search(client, embedder, question):
        embedder.embed(["PRIVATE"], kind="query")
        return object()

    monkeypatch.setattr(m, "search_question", search)
    monkeypatch.setattr(
        m, "chunks_record", lambda r: [{"text": "PRIVATE"} for _ in range(50)]
    )

    class FailingModel:
        def score(self, *args):
            raise RuntimeError("PRIVATE")

    runtime = NS(
        client=object(), embedder=NS(embed=lambda *a, **k: []), model=FailingModel()
    )
    row = m.attempt(q, runtime, clock)
    assert row["status"] == "failed" and row["rerank_s"] > 0
    assert row["embed_s"] > 0 and row["search_s"] >= 0
    assert "PRIVATE" not in str(row)


def test_initialization_failure_registers_empty_raw(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "preflight", lambda root: ({}, [], []))

    def fail(config):
        raise RuntimeError("PRIVATE")

    with pytest.raises(RuntimeError):
        m.run_latency(root=tmp_path, runtime_factory=fail)
    manifest = m.read_json(tmp_path / LATENCY_PATH)
    assert manifest["status"] == "failed"
    assert manifest["raw"] == fingerprint(tmp_path / m.RAW_PATH)
    assert manifest["raw"]["rows"] == 0
    assert not (tmp_path / m.LOCK_PATH).exists()
