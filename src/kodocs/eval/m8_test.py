"""Operator-only M8 test: authenticate the M5 control before loading a reranker.

Preflight candidates stay in memory until every control check passes. Raw
execution records contain identifiers/metrics only; text lives in private caches.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import asdict
from pathlib import Path

from kodocs.eval.m5_dev import _git_commit, _write_json_exclusive
from kodocs.eval.m5_metrics import Span, ndcg_at_k, recall_at_k, span_hit_at_k
from kodocs.eval.m5_runner import RunConfig, _search_params
from kodocs.eval.m5_stats import describe, paired_bootstrap, verdict
from kodocs.eval.m5_test import (
    _BOOTSTRAP_RESAMPLES,
    _BOOTSTRAP_SEED,
    PreflightError,
    TestLockError,
    _load_inputs,
)
from kodocs.eval.m6_runner import _append_record as append_record
from kodocs.eval.m7_rawhash import fingerprint
from kodocs.eval.m8_contract import FREEZE_PATH, RESULTS_DIR, RUNS_PATH, validate_result
from kodocs.eval.m8_dev import (
    INDEX,
    M5_CONFIG_HASH,
    M5_INDEX_PATH,
    preflight_indexes,
    score_key,
    verify_config,
    write_private_jsonl,
)
from kodocs.ingest.manifest import now_iso, sha256_file
from kodocs.retrieval.access import UserContext
from kodocs.retrieval.embedding import Bge3Embedder, embedder_settings_hash
from kodocs.retrieval.opensearch import OpenSearchClient
from kodocs.retrieval.rerank import (
    RERANKER_EXPECTED_FILES,
    RERANKER_LICENSE,
    RERANKER_MODEL_ID,
    RERANKER_REVISION,
    CrossEncoderReranker,
    RerankCandidate,
    measure_pair_lengths,
    mps_memory_snapshot,
    reorder,
    resolve_local_reranker_snapshot,
)
from kodocs.retrieval.search import authorized_search

M5_LOCAL_PATH = Path("data/interim/m5/run-01/results.json")
M5_RESULT_PATH = Path("data/manifest/m5_test_results/run-01.json")
CANDIDATES_PATH = Path("data/interim/m8/candidates/test.jsonl")
SCORES_PATH = Path("data/interim/m8/scores/test.jsonl")
FINAL_CONFIG = asdict(
    RunConfig("composite", "structural", "hybrid_weighted", "nori", 50, 0.7)
)


def read_json(path):
    return json.loads(Path(path).read_bytes())


def read_rows(path):
    return (
        [
            json.loads(line)
            for line in Path(path).read_text().splitlines()
            if line.strip()
        ]
        if Path(path).exists()
        else []
    )


def frozen(root):
    check = verify_config(root / FREEZE_PATH)
    if not check.ok:
        raise PreflightError("M8 freeze 검사 실패: " + ", ".join(check.mismatches))
    return read_json(root / FREEZE_PATH)


def execution_slot(root, rerun_reason):
    records = read_rows(root / RUNS_PATH)
    reason = rerun_reason.strip() if rerun_reason else None
    if records and not reason:
        raise TestLockError(
            "이전 실행이 있습니다. 재실행은 --rerun-reason이 필요합니다"
        )
    if not records and reason:
        raise TestLockError("첫 실행에는 --rerun-reason을 지정할 수 없습니다")
    completed = [r for r in records if r["status"] == "completed"]
    previous = completed[-1] if completed else None
    if (
        previous
        and sha256_file(root / previous["result_path"]) != previous["result_sha256"]
    ):
        raise PreflightError("이전 M8 결과 해시 불일치")
    seq = max((r["seq"] for r in completed), default=0) + 1
    result_path = root / RESULTS_DIR / f"run-{seq:02d}.json"
    raw_path = root / f"data/interim/m8/test/run-{seq:02d}/runs.jsonl"
    failures = [r for r in records if r["seq"] == seq and r["status"] != "completed"]
    for record in failures:
        path = root / record["raw_path"]
        if path.exists() and fingerprint(path) != record["raw"]:
            raise PreflightError("이전 실패 원본 지문 불일치")
    if (
        result_path.exists()
        or raw_path.exists()
        or (raw_path.parent.exists() and not failures)
    ):
        raise TestLockError("기존 M8 결과/원본 경로 덮어쓰기 거부")
    return seq, reason, previous, result_path, raw_path, len(records) + 1


def collect_candidates(client, embedder, questions, config):
    """The exact M5 search and authorization path, without any output writes."""
    if embedder_settings_hash(embedder) != config["embedder"]["settings_hash"]:
        raise PreflightError("M5 임베더 설정 불일치")
    if (config["hybrid_method"], config["candidate_chunks"], config["bm25_weight"]) != (
        "hybrid_weighted",
        50,
        0.7,
    ):
        raise PreflightError("M8 대조군 설정 불일치")
    output = []
    for item in questions:
        q = item.question
        result = search_question(client, embedder, q)
        chunks = chunks_record(result)
        if len(chunks) != 50:
            raise PreflightError(f"{q.id}: 후보 50청크 미충족")
        output.append(
            {
                "question_id": q.id,
                "set": q.set,
                "question": q.question,
                "chunks": chunks,
            }
        )
    return output


def search_question(client, embedder, q):
    return authorized_search(
        client,
        INDEX,
        UserContext(q.asker.team, q.asker.grade),
        q.question,
        _search_params(
            RunConfig("composite", "structural", "hybrid_weighted", "nori", 50, 0.7)
        ),
        embedder,
        prefer_current=False,
    )


def chunks_record(result):
    chunks = []
    for rank, hit in enumerate(result.chunks, 1):
        if hit.text is None:
            raise PreflightError("후보 청크 원문 없음")
        chunks.append(
            {
                "chunk_id": hit.chunk_id,
                "doc_id": hit.doc_id,
                "text": hit.text,
                "section_key": hit.section_key,
                "start": hit.start,
                "end": hit.end,
                "base_rank": rank,
                "base_score": hit.score,
            }
        )
    return chunks


def control_docs(record):
    best = {}
    for c in record["chunks"]:
        best[c["doc_id"]] = max(best.get(c["doc_id"], -math.inf), c["base_score"])
    return sorted(best, key=lambda d: (-best[d], d))


def reranked_chunks(record, scores, config):
    chunks = record["chunks"]
    candidates = [
        RerankCandidate(c["chunk_id"], c["doc_id"], c["text"], c["base_rank"])
        for c in chunks
    ]
    ids = reorder(candidates, scores, depth=config["depth"], fusion=config["fusion"])
    by_id = {c["chunk_id"]: c for c in chunks}
    return [by_id[cid] for cid in ids]


def final_aggregate(manifest, scope):
    analysis = manifest["analysis"]
    ladder = (
        analysis["ladder"]
        if scope == "gold"
        else analysis["silver_direction"]["all"]["ladder"]
    )
    matches = [r for r in ladder["stages"] if r["id"] == "nori_hybrid_structural"]
    if len(matches) != 1:
        raise PreflightError(f"M5 {scope} 최종 집계 행 불일치")
    return matches[0]["mean_ndcg10"]


def verify_control(root, records, questions):
    path = root / M5_LOCAL_PATH
    if not path.is_file():
        raise PreflightError(f"M5 문항별 결과 파일 없음: {M5_LOCAL_PATH.as_posix()}")
    local = read_json(path)
    committed = read_json(root / M5_RESULT_PATH)
    truths = {q.question.id: q for q in questions}
    by_id = {r["question_id"]: r for r in records}
    if len(by_id) != len(records) or set(by_id) != set(truths):
        raise PreflightError("M8 후보 문항 집합 불일치")
    selected_counts = {}
    for scope, count in (("gold", 21), ("silver", 65)):
        matches = [
            r
            for r in local["runs"]
            if r["config"] == FINAL_CONFIG
            and r["split"] == "test"
            and r["question_set"] == scope
        ]
        if len(matches) != 1:
            raise PreflightError(
                f"M5 {scope} 설정 매칭 {len(matches)}개; 정확히 하나 필요"
            )
        values = matches[0]["questions"]
        expected_ids = {q.question.id for q in questions if q.question.set == scope}
        if (
            len(values) != count
            or len(expected_ids) != count
            or {v["qid"] for v in values} != expected_ids
        ):
            raise PreflightError(f"M5 {scope} 문항 수/ID 불일치")
        mean = math.fsum(v["ndcg10"] for v in values) / count
        if (
            not math.isfinite(mean)
            or abs(mean - final_aggregate(committed, scope)) > 1e-9
        ):
            raise PreflightError(f"M5 {scope} 로컬 평균·커밋 집계 불일치")
        selected_counts[scope] = count
    # Authenticate both means before the per-question comparison.
    for scope in selected_counts:
        run = next(
            r
            for r in local["runs"]
            if r["config"] == FINAL_CONFIG
            and r["split"] == "test"
            and r["question_set"] == scope
        )
        for value in run["questions"]:
            qid = value["qid"]
            actual = ndcg_at_k(control_docs(by_id[qid]), truths[qid].rel_by_doc)
            expected = value["ndcg10"]
            if not math.isfinite(expected) or abs(actual - expected) > 1e-9:
                raise PreflightError(
                    f"{qid}: 대조군 NDCG@10 불일치 ({actual} / {expected})"
                )
    return {
        "sha256": sha256_file(path),
        "rows": sum(selected_counts.values()),
        "selected_runs": selected_counts,
        "note": "M5 기록 없음, M8에서 처음 기록",
    }


def h8_verdict(statistic):
    return "H8 " + verdict(statistic)


def harm_count(differences):
    return sum(d < 0 for d in differences)


def comparison(rows, metric):
    return asdict(
        paired_bootstrap(
            [r["reranked"][metric] for r in rows],
            [r["control"][metric] for r in rows],
            n_boot=_BOOTSTRAP_RESAMPLES,
            seed=_BOOTSTRAP_SEED,
        )
    )


def means(rows, variant):
    return {
        key: math.fsum(r[variant][key] for r in rows if r[variant][key] is not None)
        / sum(r[variant][key] is not None for r in rows)
        if any(r[variant][key] is not None for r in rows)
        else None
        for key in ("ndcg10", "recall10", "span_hit")
    }


def metric_row(q, chunks, docs):
    return {
        "ndcg10": ndcg_at_k(docs, q.rel_by_doc),
        "recall10": recall_at_k(docs, q.rel_by_doc),
        "span_hit": span_hit_at_k(
            [Span(c["doc_id"], c["start"], c["end"]) for c in chunks], q.answer_ranges
        )
        if q.answer_ranges
        else None,
    }


def d1_table(root, rows, questions):
    path = root / "data/manifest/m7_failures.json"
    ids = set()

    def walk(value):
        if isinstance(value, dict):
            for key, v in value.items():
                if key == "d1":
                    ids.update(v.get("ids", []))
                else:
                    walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    if path.exists():
        walk(read_json(path))
    by_id = {r["question_id"]: r for r in rows}
    truths = {q.question.id: q.rel_by_doc for q in questions}
    output = []
    for qid in sorted(ids):
        r = by_id.get(qid)
        if r is None:
            output.append({"question_id": qid, "status": "대상 외"})
            continue
        rel2 = [d for d, grade in truths[qid].items() if grade == 2]
        ranks = lambda ranking, rel2=rel2: {
            d: ranking.index(d) + 1 if d in ranking else None for d in rel2
        }
        output.append(
            {
                "question_id": qid,
                "status": "대상",
                "visible_rel2_count": len(rel2),
                "candidate_rel2_count": sum(d in r["control_docs"] for d in rel2),
                "control_ranks": ranks(r["control_docs"]),
                "reranked_ranks": ranks(r["reranked_docs"]),
            }
        )
    return output


def analyze(rows, root, questions):
    result = {
        "harm": {},
        "auxiliary": {"by_source": {}, "by_type": {}},
        "comparisons": {},
    }
    for scope in ("gold", "silver"):
        subset = [r for r in rows if r["set"] == scope]
        result[scope] = {v: means(subset, v) for v in ("control", "reranked")}
        result["harm"][scope] = harm_count(
            [r["reranked"]["ndcg10"] - r["control"]["ndcg10"] for r in subset]
        )
        result["comparisons"][scope] = {
            metric: comparison(subset, metric) for metric in ("ndcg10", "recall10")
        }
        result["auxiliary"][scope] = {
            "recall50": math.fsum(r["recall50"] for r in subset) / len(subset),
            "n": len(subset),
        }
        for field in ("source", "type"):
            result["auxiliary"]["by_" + field][scope] = {
                value: {
                    "n": len(group),
                    "control": means(group, "control"),
                    "reranked": means(group, "reranked"),
                    "ndcg_diff": describe(
                        [
                            r["reranked"]["ndcg10"] - r["control"]["ndcg10"]
                            for r in group
                        ]
                    ),
                }
                for value in sorted({r[field] for r in subset})
                if (group := [r for r in subset if r[field] == value])
            }
    primary = result["comparisons"]["gold"]["ndcg10"]
    result["h8"] = {
        "verdict": h8_verdict(type("CI", (), primary)()),
        "diff": primary["mean_diff"],
        "ci_low": primary["ci_low"],
        "ci_high": primary["ci_high"],
        "n": primary["n"],
    }
    result["auxiliary"]["silver_direction"] = result["comparisons"]["silver"]["ndcg10"]
    result["auxiliary"]["d1"] = d1_table(root, rows, questions)
    return result


def pair_lengths(model, record):
    # ST 6.1.0 base/model.py:1628 exposes the tokenizer used by CrossEncoder.
    return measure_pair_lengths(
        model._model.tokenizer,
        record["question"],
        [c["text"] for c in record["chunks"]],
    )


def run_test(
    *,
    root=Path("."),
    client,
    embedder,
    questions=None,
    rerun_reason=None,
    collect_fn=collect_candidates,
    model_factory=CrossEncoderReranker,
    snapshot_fn=resolve_local_reranker_snapshot,
    lengths_fn=pair_lengths,
    memory_fn=mps_memory_snapshot,
    clock=time.perf_counter,
):
    root = Path(root)
    config = frozen(root)
    config_file_sha256 = sha256_file(root / FREEZE_PATH)
    slot = execution_slot(root, rerun_reason)
    m5 = read_json(root / "data/manifest/m5_config.json")
    preflight_indexes(client, m5, read_json(root / M5_INDEX_PATH))
    if questions is None:
        inputs = _load_inputs(root)
        questions = [*inputs.gold_test, *inputs.silver_test]
    if any(q.question.split != "test" for q in questions):
        raise PreflightError("M8 test 이외 문항 거부")
    records = collect_fn(client, embedder, questions, m5)
    local_fingerprint = verify_control(root, records, questions)
    # No outputs or reranker have been created before this point.
    lock = root / "data/interim/m8/test/.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lock.open("x"):
            pass
    except FileExistsError as exc:
        raise TestLockError("M8 test 실행 중") from exc
    try:
        if slot != execution_slot(root, rerun_reason):
            raise TestLockError("M8 실행 기록이 검사 중 변경됨")
        seq, reason, previous, result_path, raw_path, attempt_no = slot
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        with raw_path.open("x"):
            pass
        status, result_hash = "failed", None
        entry = {
            "seq": seq,
            "attempt": attempt_no,
            "ran_at": now_iso(),
            "rerun_reason": reason,
            "previous_result_sha256": previous["result_sha256"] if previous else None,
            "m5_local_results": local_fingerprint,
        }
        try:
            write_private_jsonl(root / CANDIDATES_PATH, records, root=root)
            start = clock()
            model = model_factory(
                snapshot_fn(),
                max_length=config["max_length"],
                device=config["device"],
                batch_size=config["batch_size"],
            )
            init_s = clock() - start
            memory = memory_fn()
            scores, lengths, rows = [], [], []
            by_id = {q.question.id: q for q in questions}
            for r in records:
                values = model.score(r["question"], [c["text"] for c in r["chunks"]])
                if len(values) != len(r["chunks"]) or not all(
                    math.isfinite(v) for v in values
                ):
                    raise PreflightError("리랭커 점수 개수/유한 값 위반")
                pair_values = lengths_fn(model, r)
                if len(pair_values) != len(r["chunks"]) or any(
                    type(n) is not int or n < 1 for n in pair_values
                ):
                    raise PreflightError("리랭커 쌍 길이 개수/값 위반")
                lengths.extend(pair_values)
                for c, score in zip(r["chunks"], values, strict=True):
                    scores.append(
                        {
                            "key": score_key(
                                question_id=r["question_id"],
                                chunk_id=c["chunk_id"],
                                question=r["question"],
                                text=c["text"],
                                max_length=config["max_length"],
                                revision=RERANKER_REVISION,
                            ),
                            "question_id": r["question_id"],
                            "chunk_id": c["chunk_id"],
                            "score": float(score),
                        }
                    )
                q = by_id[r["question_id"]]
                chunks = reranked_chunks(r, values, config)
                docs = list(dict.fromkeys(c["doc_id"] for c in chunks))
                control = control_docs(r)
                row = {
                    "question_id": r["question_id"],
                    "set": r["set"],
                    "source": q.question.source,
                    "type": q.question.type,
                    "control_docs": control,
                    "reranked_docs": docs,
                    "control_chunk_ids": [c["chunk_id"] for c in r["chunks"]],
                    "reranked_chunk_ids": [c["chunk_id"] for c in chunks],
                    "control": metric_row(q, r["chunks"], control),
                    "reranked": metric_row(q, chunks, docs),
                    "recall50": recall_at_k(control, q.rel_by_doc, k=50),
                }
                rows.append(row)
                append_record(raw_path, row)
            write_private_jsonl(root / SCORES_PATH, scores, root=root)
            if sha256_file(root / FREEZE_PATH) != config_file_sha256:
                raise PreflightError("M8 freeze 파일이 실행 중 변경됨")
            result = {
                **analyze(rows, root, questions),
                "selected": {
                    "depth": config["depth"],
                    "fusion": "단독" if config["fusion"] == "alone" else "RRF",
                },
                "max_length": config["max_length"],
                "truncated_ratio": sum(n > config["max_length"] for n in lengths)
                / len(lengths),
                "n_pairs": len(lengths),
                "device": model.describe()["device"],
                "init_s": init_s,
                "batch_size": config["batch_size"],
                "mps_memory": memory,
                "reranker": model.describe(),
                "model_id": RERANKER_MODEL_ID,
                "model_revision": RERANKER_REVISION,
                "license": RERANKER_LICENSE,
                "model_size_bytes": RERANKER_EXPECTED_FILES["model.safetensors"],
                "config_hash": config["config_hash"],
                "config_file_sha256": config_file_sha256,
                "m5_config_hash": M5_CONFIG_HASH,
                "rules_sha256": config["rules_sha256"],
                "code_commit": _git_commit(root),
                "ran_at": entry["ran_at"],
                "raw": fingerprint(raw_path),
            }
            validate_result(result)
            _write_json_exclusive(result_path, result)
            result_hash = sha256_file(result_path)
            status = "completed"
            return result
        except BaseException as exc:
            status = "failed" if isinstance(exc, Exception) else "stopped"
            entry["error_type"] = type(exc).__name__
            if result_path.exists() and not isinstance(exc, FileExistsError):
                result_path.unlink()
            # Failed attempts do not advance M5-style successful result numbering.
            # Keep each failure in its own immutable archive for a qualified retry.
            archived = (
                raw_path.parent / f"failed-attempt-{attempt_no:02d}" / "runs.jsonl"
            )
            archived.parent.mkdir()
            raw_path.rename(archived)
            raw_path = archived
            raise
        finally:
            append_record(
                root / RUNS_PATH,
                {
                    **entry,
                    "status": status,
                    "result_path": result_path.relative_to(root).as_posix(),
                    "result_sha256": result_hash,
                    "raw_path": raw_path.relative_to(root).as_posix(),
                    "raw": fingerprint(raw_path),
                },
            )
    finally:
        lock.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--rerun-reason")
    args = parser.parse_args(argv)
    try:
        frozen(args.root)
        execution_slot(args.root, args.rerun_reason)
        with OpenSearchClient() as client:
            # Reject index mismatches before embedding model initialization too.
            preflight_indexes(
                client,
                read_json(args.root / "data/manifest/m5_config.json"),
                read_json(args.root / M5_INDEX_PATH),
            )
            embedder = Bge3Embedder()
            embedder.prepare()
            run_test(
                root=args.root,
                client=client,
                embedder=embedder,
                rerun_reason=args.rerun_reason,
            )
        print("M8 test 완료")
        return 0
    except PreflightError as exc:
        print(f"M8 test 사전 검사 실패: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - no service/model text in logs
        print(f"M8 test 실패: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
