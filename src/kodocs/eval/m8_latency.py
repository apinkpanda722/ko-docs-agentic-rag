"""Operator-run, serial retrieval plus reranking latency; no LLM calls."""

from __future__ import annotations

import argparse
import importlib.metadata
import math
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from kodocs.eval.m5_dev import _config_hash, _git_commit
from kodocs.eval.m6_inputs import load_m6_questions, require_input_manifest
from kodocs.eval.m6_runner import _append_record as append_record
from kodocs.eval.m7_latency import (
    LatencyLockError,
    _p95_ci,
    _statistics,
    _verify_history,
    _write,
    require_serial,
)
from kodocs.eval.m7_rawhash import fingerprint
from kodocs.eval.m8_contract import LATENCY_PATH, validate_latency
from kodocs.eval.m8_test import (
    M5_CONFIG_HASH,
    M5_INDEX_PATH,
    CrossEncoderReranker,
    chunks_record,
    frozen,
    mps_memory_snapshot,
    preflight_indexes,
    read_json,
    reranked_chunks,
    resolve_local_reranker_snapshot,
    search_question,
)
from kodocs.ingest.manifest import now_iso
from kodocs.retrieval.embedding import (
    Bge3Embedder,
    CachedEmbedder,
    embedder_settings_hash,
)
from kodocs.retrieval.opensearch import OpenSearchClient

RAW_PATH = Path("data/interim/m8/latency/runs.jsonl")
LOCK_PATH = Path("data/interim/m8/latency/.lock")
NOTE = "두 P95를 단순히 더한 보수적 근사이며 분포를 합성한 값이 아니다. M7 값은 임베딩 캐시 적중 상태에서 쟀다"
PACKAGES = ("sentence-transformers", "torch", "transformers")


def preflight(root):
    config = frozen(root)
    inputs = require_input_manifest(root)
    questions = sorted(load_m6_questions(root, split="test"), key=lambda q: q.id)
    dev = sorted(load_m6_questions(root, split="dev"), key=lambda q: q.id)
    m7 = read_json(root / "data/manifest/m7_latency.json")
    warmup_ids = m7["warmup_ids"]
    by_id = {q.id: q for q in dev}
    if (
        len([q for q in questions if q.set == "gold"]) != 30
        or [q.id for q in questions if q.set == "silver"]
        != inputs["silver_test"]["ids"]
        or [q.id for q in dev] != inputs["dev_set"]["ids"]
        or len(questions) != 113
        or len({q.id for q in questions}) != 113
        or warmup_ids != [q.id for q in dev[:3]]
    ):
        raise ValueError("M7 지연 문항/예열 목록 불일치")
    return config, questions, [by_id[qid] for qid in warmup_ids]


def summarize(rows, root):
    if len(rows) != 113 or any(r.get("warmup") for r in rows):
        raise ValueError("지연 집계에는 예열 제외 113문항 필요")
    result = {}
    for segment in ("embed_s", "search_s", "rerank_s"):
        values = [r.get(segment) for r in rows]
        stat = _statistics(values)
        observed = [v for v in values if v is not None]
        stat["mean_s"] = math.fsum(observed) / len(observed) if observed else None
        if segment == "rerank_s":
            stat["p95_ci95_s"] = _p95_ci(observed) if observed else None
        result[segment] = stat
    m7 = read_json(root / "data/manifest/m7_latency.json")
    p95 = m7["systems"]["simple"]["p95_s"]
    total = p95 + result["rerank_s"]["p95_s"]
    result.update(
        simple_p95_m7=p95,
        conservative_sum_s=total,
        margin_to_5s=5 - total,
        note=NOTE,
        failure_count=sum(r["status"] != "completed" for r in rows),
        bootstrap_repetitions=10_000,
        bootstrap_seed=m7["systems"]["simple"].get("bootstrap_seed", 20261001),
        p95_method="nearest-rank",
    )
    return result


class TimedEmbedder:
    """Time the real embed call inside authorized_search, including failures."""

    def __init__(self, inner, clock):
        self.inner, self.clock, self.elapsed = inner, clock, 0.0

    def embed(self, *args, **kwargs):
        start = self.clock()
        try:
            return self.inner.embed(*args, **kwargs)
        finally:
            self.elapsed += self.clock() - start


def attempt(q, runtime, clock):
    row = {
        "question_id": q.id,
        "status": "completed",
        "embed_s": None,
        "search_s": None,
        "rerank_s": None,
    }
    timed = TimedEmbedder(runtime.embedder, clock)
    start = clock()
    rerank_start = None
    try:
        result = search_question(runtime.client, timed, q)
        row["embed_s"] = timed.elapsed
        row["search_s"] = clock() - start - timed.elapsed
        rerank_start = clock()
        chunks = chunks_record(result)
        if len(chunks) != 50:
            raise ValueError("후보 50청크 미충족")
        record = {"question": q.question, "chunks": chunks}
        scores = runtime.model.score(q.question, [c["text"] for c in chunks])
        reranked_chunks(record, scores, runtime.config)
    except BaseException as exc:  # noqa: BLE001 - pin evidence on failure/interrupt
        row.update(status="failed", error_type=type(exc).__name__)
        if not isinstance(exc, Exception):
            row["stop"] = exc
    finally:
        now = clock()
        if rerank_start is not None:
            row["rerank_s"] = now - rerank_start
        else:
            row["embed_s"] = timed.elapsed
            row["search_s"] = max(0, now - start - timed.elapsed)
        row["total_s"] = now - start
    return row


def environment(embedder, model):
    versions = {}
    for name in PACKAGES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return {
        "device": model.describe()["device"],
        "batch_size": model.describe().get("batch_size"),
        "max_length": model.describe().get("max_length"),
        "reranker": model.describe(),
        "embedder": {
            "model_id": embedder.model_id,
            "revision": embedder.revision,
            "settings": dict(embedder.settings),
        },
        "embedding_cache_used": True,
        "embedding_cache_stats": embedder.stats,
        "mps_memory": mps_memory_snapshot(),
        "packages": versions,
    }


def build_runtime(config, root):
    client = OpenSearchClient()
    try:
        m5 = read_json(root / "data/manifest/m5_config.json")
        preflight_indexes(client, m5, read_json(root / M5_INDEX_PATH))
        embedder = CachedEmbedder(
            Bge3Embedder(), root / "data/interim/m8/embedding_cache"
        )
        start = time.perf_counter()
        embedder.prepare()
        if embedder_settings_hash(embedder) != m5["embedder"]["settings_hash"]:
            raise ValueError("M5 임베더 설정 불일치")
        embed_init_s = time.perf_counter() - start
        start = time.perf_counter()
        model = CrossEncoderReranker(
            resolve_local_reranker_snapshot(),
            max_length=config["max_length"],
            device=config["device"],
            batch_size=config["batch_size"],
        )
        init_s = time.perf_counter() - start
        return SimpleNamespace(
            client=client,
            embedder=embedder,
            model=model,
            config=config,
            init_s=init_s,
            embed_init_s=embed_init_s,
            environment=environment(embedder, model),
        )
    except BaseException:
        client.close()
        raise


def run_latency(
    *,
    root=Path("."),
    concurrency=1,
    rerun_reason=None,
    runtime_factory=None,
    attempt_fn=attempt,
    clock=time.perf_counter,
):
    require_serial(concurrency)
    root = Path(root)
    config, questions, warmup = preflight(root)
    manifest_path, raw_path = root / LATENCY_PATH, root / RAW_PATH
    old = read_json(manifest_path) if manifest_path.exists() else {}
    reason = rerun_reason.strip() if rerun_reason else None
    if old:
        _verify_history(old)
        if fingerprint(raw_path) != old["raw"]:
            raise ValueError("M8 latency 원본 지문 불일치")
        if not reason:
            raise LatencyLockError("이전 지연 실행 있음; RERUN_REASON 필요")
    elif reason or raw_path.exists():
        raise LatencyLockError("첫 지연 실행 사유/미등록 원본 거부")
    lock = root / LOCK_PATH
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lock.open("x"):
            pass
    except FileExistsError as exc:
        raise LatencyLockError("M8 latency 실행 중") from exc
    current = read_json(manifest_path) if manifest_path.exists() else {}
    if current != old:
        lock.unlink()
        raise LatencyLockError("M8 지연 기록이 검사 중 변경됨")
    history = old.get("history", [])
    seq = len(history) + 1
    rows, runtime = [], None
    raw_path.touch(exist_ok=True)
    base = {
        "status": "failed",
        "ran_at": now_iso(),
        "code_commit": _git_commit(root),
        "concurrency": 1,
        "config_hash": config.get("config_hash"),
        "m5_config_hash": M5_CONFIG_HASH,
        "warmup_ids": [q.id for q in warmup],
        "warmup_excluded_count": 3,
        "note": NOTE,
    }
    try:
        factory = runtime_factory or (lambda c: build_runtime(c, root))
        runtime = factory(config)
        base.update(
            init_s=runtime.init_s,
            embed_init_s=getattr(runtime, "embed_init_s", None),
            environment=runtime.environment,
        )
        for q in warmup:
            row = attempt_fn(q, runtime, clock)
            if row.get("stop"):
                raise row["stop"]
        for q in questions:
            row = attempt_fn(q, runtime, clock)
            stop = row.pop("stop", None)
            row["run_seq"] = seq
            append_record(raw_path, row)
            rows.append(row)
            if stop:
                raise stop
        base.update(summarize(rows, root), status="completed")
        if hasattr(runtime, "embedder"):
            base["environment"]["embedding_cache_stats"] = runtime.embedder.stats
        validate_latency(base)
    except BaseException as exc:
        base.update(
            status="failed" if isinstance(exc, Exception) else "stopped",
            error_type=type(exc).__name__,
        )
        raise
    finally:
        # Pin bytes at completion/failure/interrupt, never in the report generator.
        base["raw"] = fingerprint(raw_path)
        entry = {
            "seq": seq,
            "ran_at": base["ran_at"],
            "status": base["status"],
            "rerun_reason": reason,
            "previous_result_sha256": history[-1]["result_sha256"] if history else None,
            "raw": base["raw"],
            "result": dict(base),
        }
        entry["result_sha256"] = _config_hash(entry)
        base["history"] = history + [entry]
        _write(manifest_path, base)
        lock.unlink()
        if runtime and hasattr(runtime, "client"):
            runtime.client.close()
    return base


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--rerun-reason")
    args = parser.parse_args(argv)
    try:
        run_latency(root=args.root, rerun_reason=args.rerun_reason)
        print("M8 latency 완료")
        return 0
    except Exception as exc:  # noqa: BLE001 - no raw service text
        print(f"M8 latency 실패: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
