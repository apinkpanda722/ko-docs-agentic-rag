"""Operator-run M8 dev candidate/token/score caches and immutable freeze.

Only authorized M5 candidates are reranked. Text remains in ignored interim
JSONL; committed manifests are explicit projections of hashes and metrics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from dataclasses import asdict
from pathlib import Path

from kodocs.eval.m5_dev import (
    ConfigCheck,
    _config_hash,
    _git_commit,
    _load_dev_questions,
    _read_json_object,
    _require_registered_dev_counts,
    _write_json_exclusive,
)
from kodocs.eval.m5_metrics import ndcg_at_k, recall_at_k
from kodocs.eval.m5_runner import RunConfig, _search_params
from kodocs.eval.m5_test import PreflightError, _check_indexes, _make_embedder
from kodocs.eval.m7_rawhash import fingerprint
from kodocs.eval.m8_contract import FREEZE_PATH
from kodocs.eval.m8_rules import rules_sha256
from kodocs.ingest.manifest import now_iso, sha256_file
from kodocs.retrieval.access import UserContext
from kodocs.retrieval.embedding import embedder_settings_hash
from kodocs.retrieval.index_build import _canonical_hash
from kodocs.retrieval.opensearch import OpenSearchClient
from kodocs.retrieval.rerank import (
    RANK_CONSTANT,
    RERANKER_MODEL_ID,
    RERANKER_REVISION,
    CrossEncoderReranker,
    RerankCandidate,
    choose_max_length,
    measure_pair_lengths,
    mps_memory_snapshot,
    reorder,
    resolve_local_reranker_snapshot,
)
from kodocs.retrieval.search import authorized_search

INDEX = "kodocs-m5-composite-structural"
M5_CONFIG_HASH = "455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d"
CANDIDATES_PATH = Path("data/interim/m8/candidates/dev.jsonl")
SCORES_PATH = Path("data/interim/m8/scores/dev.jsonl")
CANDIDATE_MANIFEST_PATH = Path("data/manifest/m8_candidates.json")
MAX_LENGTH_PATH = Path("data/manifest/m8_max_length.json")
DEV_PATH = Path("data/manifest/m8_dev.json")
CANDIDATE_MANIFEST_KEYS = frozenset({"splits", "m5_config_hash", "rules_sha256"})
BASELINE_NOTE = "M5 dev 격자는 고정 청킹 기준이라 구조 청킹 대조군의 dev 값은 M5에 기록이 없다. M8에서 처음 기록한다."


def current_rules_hash(root):
    return rules_sha256(root / "docs/results/hypotheses.md")


def preflight_indexes(client, m5_config):
    """Read actual properties/count; use the M5 builder hash and checker."""
    if m5_config.get("config_hash") != M5_CONFIG_HASH:
        raise PreflightError("M5 config_hash 불일치")
    indexes = m5_config.get("indexes", {})
    if INDEX not in indexes:
        raise PreflightError("M5 구조 청킹 인덱스 이름 불일치")
    try:
        mapping = client.request("GET", f"/{INDEX}/_mapping")[INDEX]["mappings"]
        count = client.request("GET", f"/{INDEX}/_count")["count"]
    except Exception:  # noqa: BLE001 - sanitize OpenSearch response bodies
        raise PreflightError("M5 구조 청킹 인덱스 조회 실패") from None
    if count != indexes[INDEX]["chunk_count"]:
        raise PreflightError("M5 chunk_count 불일치")
    # _check_indexes checks all four recorded entries. Only the structural
    # entry is replaced with a live observation; other indexes are not searched.
    entries = {
        name: {
            "status": "ok",
            "failure_count": 0,
            "failures": [],
            "index_field_schema_hash": entry["schema_hash"],
            "embedder": {"settings_hash": m5_config["embedder"]["settings_hash"]},
        }
        for name, entry in indexes.items()
    }
    entries[INDEX]["index_field_schema_hash"] = _canonical_hash(mapping["properties"])
    if "_meta" in mapping:
        entries[INDEX]["embedder"]["settings_hash"] = mapping["_meta"].get(
            "embedder_settings_hash"
        )
    _check_indexes(m5_config, {"indexes": entries})


def _not_frozen(root):
    if (root / FREEZE_PATH).exists():
        raise PreflightError("M8 freeze 이후 dev 변경 금지")


def _check_fingerprint(path, expected):
    try:
        actual = fingerprint(path)
    except (OSError, ValueError) as exc:
        raise PreflightError("M8 캐시 지문 확인 실패") from exc
    if actual != {key: expected.get(key) for key in ("sha256", "rows")}:
        raise PreflightError("M8 캐시 SHA-256/행 수 불일치")
    return actual


def write_private_jsonl(path, records, *, root):
    if not path.resolve().is_relative_to((root / "data/interim/m8").resolve()):
        raise PreflightError("M8 원문 캐시 경로 위반")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as out:
            for record in records:
                out.write(
                    json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
                )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_jsonl(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_candidate_manifest(root, payload):
    if set(payload) != CANDIDATE_MANIFEST_KEYS:
        raise PreflightError("후보 manifest 허용 키 위반")
    if payload["m5_config_hash"] != M5_CONFIG_HASH:
        raise PreflightError("후보 M5 config_hash 불일치")
    # Nested allowlists prevent arbitrary source fields escaping the projection.
    if set(payload["splits"]) != {"dev"}:
        raise PreflightError("후보 split 위반")
    split = payload["splits"]["dev"]
    if set(split) != {"sha256", "rows", "question_count", "questions"}:
        raise PreflightError("후보 manifest split 허용 키 위반")
    if any(
        set(q) != {"chunk_ids_sha256", "chunk_count"}
        for q in split["questions"].values()
    ):
        raise PreflightError("후보 manifest 문항 허용 키 위반")
    _write_json_exclusive(root / CANDIDATE_MANIFEST_PATH, payload)


def read_candidates_manifest(root):
    manifest = _read_json_object(root / CANDIDATE_MANIFEST_PATH)
    if manifest["m5_config_hash"] != M5_CONFIG_HASH or manifest[
        "rules_sha256"
    ] != current_rules_hash(root):
        raise PreflightError("후보 manifest 설정·규칙 해시 불일치")
    return manifest


def load_candidates(root):
    manifest = read_candidates_manifest(root)
    _check_fingerprint(root / CANDIDATES_PATH, manifest["splits"]["dev"])
    return _read_jsonl(root / CANDIDATES_PATH)


def write_candidates(records, *, root=Path(".")):
    _not_frozen(root)
    if (root / CANDIDATES_PATH).exists():
        load_candidates(root)
        return read_candidates_manifest(root)
    if (root / CANDIDATE_MANIFEST_PATH).exists():
        raise PreflightError("후보 manifest만 남아 있음; 캐시 재생성 거부")
    write_private_jsonl(root / CANDIDATES_PATH, records, root=root)
    payload = {
        "m5_config_hash": M5_CONFIG_HASH,
        "rules_sha256": current_rules_hash(root),
        "splits": {
            "dev": {
                **fingerprint(root / CANDIDATES_PATH),
                "question_count": len(records),
                "questions": {
                    r["question_id"]: {
                        "chunk_ids_sha256": _canonical_hash(
                            [c["chunk_id"] for c in r["chunks"]]
                        ),
                        "chunk_count": len(r["chunks"]),
                    }
                    for r in records
                },
            }
        },
    }
    write_candidate_manifest(root, payload)
    return payload


def collect_candidates(client, embedder, questions, config, *, root=Path(".")):
    _not_frozen(root)
    preflight_indexes(client, config)
    if (root / CANDIDATES_PATH).exists():
        return load_candidates(root)
    if embedder_settings_hash(embedder) != config["embedder"]["settings_hash"]:
        raise PreflightError("M5 임베더 설정 불일치")
    cfg = RunConfig(
        "composite",
        "structural",
        config["hybrid_method"],
        "nori",
        config["candidate_chunks"],
        config["bm25_weight"],
    )
    if (cfg.method, cfg.candidate_chunks, cfg.bm25_weight) != (
        "hybrid_weighted",
        50,
        0.7,
    ):
        raise PreflightError("M8-R1 대조군 설정 불일치")
    records = []
    for q in questions:
        if q.question.split != "dev":
            raise PreflightError("dev 이외 문항 거부")
        asker = q.question.asker
        # Reuse M5 runner params and its authorized search, with temporal off.
        result = authorized_search(
            client,
            INDEX,
            UserContext(asker.team, asker.grade),
            q.question.question,
            _search_params(cfg),
            embedder,
            prefer_current=False,
        )
        chunks = []
        for rank, hit in enumerate(result.chunks, 1):
            if hit.text is None:
                raise PreflightError("후보 청크 원문 없음")
            chunks.append(
                {
                    "chunk_id": hit.chunk_id,
                    "doc_id": hit.doc_id,
                    "section_key": hit.section_key,
                    "start": hit.start,
                    "end": hit.end,
                    "text": hit.text,
                    "base_rank": rank,
                    "base_score": hit.score,
                }
            )
        if len(chunks) < 50:
            raise PreflightError("M8 후보 50청크 미충족")
        records.append(
            {
                "question_id": q.question.id,
                "set": q.question.set,
                "question": q.question.question,
                "asker": asdict(asker),
                "chunks": chunks,
            }
        )
    write_candidates(records, root=root)
    return records


def measure_tokens(tokenizer, snapshot_path, *, root=Path(".")):
    _not_frozen(root)
    if (root / MAX_LENGTH_PATH).exists():
        raise FileExistsError("M8 max_length 이미 확정됨")
    records = load_candidates(root)
    lengths = [
        n
        for r in records
        for n in measure_pair_lengths(
            tokenizer, r["question"], [c["text"] for c in r["chunks"]]
        )
    ]
    choice = choose_max_length(lengths, cap=1024)
    ordered = sorted(lengths)
    payload = {
        "max_length": choice.value,
        "rule": "P99 이상 최소 2의 거듭제곱, 상한 1024 (nearest-rank)",
        "p50": ordered[math.ceil(0.50 * len(ordered)) - 1],
        "p90": ordered[math.ceil(0.90 * len(ordered)) - 1],
        "p99": choice.p99,
        "max": max(lengths),
        "n_pairs": len(lengths),
        "capped": choice.capped,
        "truncated_pairs": choice.truncated_pairs,
        "truncated_ratio": choice.truncated_ratio,
        "tokenizer_revision": RERANKER_REVISION,
        "tokenizer_path_sha256": hashlib.sha256(
            str(snapshot_path).encode()
        ).hexdigest(),
        "candidate_cache": fingerprint(root / CANDIDATES_PATH),
        "candidate_manifest_sha256": sha256_file(root / CANDIDATE_MANIFEST_PATH),
        "rules_sha256": current_rules_hash(root),
        "measured_at": now_iso(),
        "code_commit": _git_commit(root),
    }
    _write_json_exclusive(root / MAX_LENGTH_PATH, payload)
    return payload


def dev_inputs(root):
    _not_frozen(root)
    if not (root / MAX_LENGTH_PATH).exists():
        raise PreflightError("먼저 `make m8-tokens` 실행")
    lengths = _read_json_object(root / MAX_LENGTH_PATH)
    records = load_candidates(root)
    if (
        lengths["candidate_manifest_sha256"]
        != sha256_file(root / CANDIDATE_MANIFEST_PATH)
        or lengths["rules_sha256"] != current_rules_hash(root)
        or lengths["tokenizer_revision"] != RERANKER_REVISION
    ):
        raise PreflightError("max_length 후보 manifest·규칙·revision 해시 불일치")
    _check_fingerprint(root / CANDIDATES_PATH, lengths["candidate_cache"])
    value = lengths["max_length"]
    if type(value) is not int or value < 1 or value > 1024 or value & (value - 1):
        raise PreflightError("max_length 형식 위반")
    return records, lengths


def score_key(*, question_id, chunk_id, question, text, max_length, revision):
    return _canonical_hash(
        {
            "question_id": question_id,
            "chunk_id": chunk_id,
            "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "max_length": max_length,
            "revision": revision,
        }
    )


def score_candidates(
    records, scorer, *, max_length, root=Path("."), revision=RERANKER_REVISION
):
    _not_frozen(root)
    path = root / SCORES_PATH
    if (root / DEV_PATH).exists():
        dev = _read_json_object(root / DEV_PATH)
        _check_fingerprint(path, dev["score_cache"])
    cached = _read_jsonl(path) if path.exists() else []
    by_key = {r["key"]: r["score"] for r in cached}
    output = []
    for r in records:
        keys = [
            score_key(
                question_id=r["question_id"],
                chunk_id=c["chunk_id"],
                question=r["question"],
                text=c["text"],
                max_length=max_length,
                revision=revision,
            )
            for c in r["chunks"]
        ]
        missing = [i for i, key in enumerate(keys) if key not in by_key]
        values = (
            scorer.score(r["question"], [r["chunks"][i]["text"] for i in missing])
            if missing
            else []
        )
        if len(values) != len(missing) or not all(math.isfinite(v) for v in values):
            raise PreflightError("점수 개수·유한 값 위반")
        for i, value in zip(missing, values, strict=True):
            by_key[keys[i]] = float(value)
            cached.append(
                {
                    "key": keys[i],
                    "question_id": r["question_id"],
                    "chunk_id": r["chunks"][i]["chunk_id"],
                    "score": float(value),
                }
            )
        write_private_jsonl(path, cached, root=root)
        output.append([by_key[key] for key in keys])
    return output


def tuning_grid():
    return [(depth, fusion) for depth in (20, 30, 50) for fusion in ("alone", "rrf")]


def select_setting(rows):
    if not rows or any(not math.isfinite(r["selection_score"]) for r in rows):
        raise PreflightError("선택 점수 위반")
    return min(
        rows, key=lambda r: (-r["selection_score"], r["fusion"] != "alone", r["depth"])
    )


def evaluate_grid(records, scores, questions):
    if any(q.question.split != "dev" for q in questions):
        raise PreflightError("dev 격자는 test 문항을 거부함")
    truths = {q.question.id: q.rel_by_doc for q in questions}
    if set(truths) != {r["question_id"] for r in records}:
        raise PreflightError("dev 문항 집합 불일치")

    def means(setting):
        metrics = {"gold": [], "silver": []}
        for r, values in zip(records, scores, strict=True):
            chunks = r["chunks"]
            if setting is None:
                # M5 document ties use doc_id, independently of chunk_id ties.
                best = {}
                for c in chunks:
                    best[c["doc_id"]] = max(
                        best.get(c["doc_id"], -math.inf), c["base_score"]
                    )
                docs = sorted(best, key=lambda d: (-best[d], d))
            else:
                candidates = [
                    RerankCandidate(
                        c["chunk_id"], c["doc_id"], c["text"], c["base_rank"]
                    )
                    for c in chunks
                ]
                ids = reorder(candidates, values, depth=setting[0], fusion=setting[1])
                by_id = {c["chunk_id"]: c for c in chunks}
                docs = list(dict.fromkeys(by_id[cid]["doc_id"] for cid in ids))
            rel = truths[r["question_id"]]
            metrics[r["set"]].append((ndcg_at_k(docs, rel), recall_at_k(docs, rel)))
        if any(not v for v in metrics.values()):
            raise PreflightError("골드·실버 dev 문항 필요")
        return {
            scope: {
                "ndcg10": math.fsum(v[0] for v in values) / len(values),
                "recall10": math.fsum(v[1] for v in values) / len(values),
            }
            for scope, values in metrics.items()
        }

    rows = []
    for setting in tuning_grid():
        result = means(setting)
        rows.append(
            {
                "depth": setting[0],
                "fusion": setting[1],
                "gold_dev_mean_ndcg10": result["gold"]["ndcg10"],
                "silver_dev_mean_ndcg10": result["silver"]["ndcg10"],
                "selection_score": (
                    result["gold"]["ndcg10"] + result["silver"]["ndcg10"]
                )
                / 2,
            }
        )
    return {
        "grid": rows,
        "selected": select_setting(rows),
        "baseline": means(None),
        "baseline_note": BASELINE_NOTE,
    }


def run_dev(
    client,
    embedder,
    questions,
    config,
    *,
    root=Path("."),
    device="mps",
    batch_size=4,
    model_factory=CrossEncoderReranker,
    snapshot_fn=resolve_local_reranker_snapshot,
    memory_fn=mps_memory_snapshot,
    clock=time.perf_counter,
):
    records, lengths = dev_inputs(root)
    if (root / DEV_PATH).exists():
        raise FileExistsError("M8 dev manifest 이미 존재함")
    preflight_indexes(client, config)
    embedder.prepare()
    if embedder_settings_hash(embedder) != config["embedder"]["settings_hash"]:
        raise PreflightError("M5 임베더 설정 불일치")
    path = snapshot_fn()
    start = clock()
    model = model_factory(
        path, max_length=lengths["max_length"], device=device, batch_size=batch_size
    )
    init_s = clock() - start
    memory = memory_fn()  # Both embedding and reranking models remain resident.
    start = clock()
    scores = score_candidates(
        records, model, max_length=lengths["max_length"], root=root
    )
    score_s = clock() - start
    payload = {
        **evaluate_grid(records, scores, questions),
        "max_length": lengths["max_length"],
        "model_id": RERANKER_MODEL_ID,
        "model_revision": RERANKER_REVISION,
        "init_s": init_s,
        "score_s": score_s,
        "device": model.describe()["device"],
        "batch_size": batch_size,
        "mps_memory": memory,
        "reranker": model.describe(),
        "score_cache": fingerprint(root / SCORES_PATH),
        "candidate_manifest_sha256": sha256_file(root / CANDIDATE_MANIFEST_PATH),
        "max_length_sha256": sha256_file(root / MAX_LENGTH_PATH),
        "rules_sha256": current_rules_hash(root),
        "code_commit": _git_commit(root),
    }
    _write_json_exclusive(root / DEV_PATH, payload)
    return payload


def freeze_config(*, root=Path(".")):
    if (root / FREEZE_PATH).exists():
        raise FileExistsError("M8 설정 이미 고정됨")
    dev = _read_json_object(root / DEV_PATH)
    if dev["rules_sha256"] != current_rules_hash(root):
        raise PreflightError("M8 규칙 절 해시 불일치")
    for field, path in (
        ("candidate_manifest_sha256", CANDIDATE_MANIFEST_PATH),
        ("max_length_sha256", MAX_LENGTH_PATH),
    ):
        if dev[field] != sha256_file(root / path):
            raise PreflightError("M8 dev 입력 해시 불일치")
    _records, lengths = dev_inputs(root)
    if dev["max_length"] != lengths["max_length"]:
        raise PreflightError("dev max_length 불일치")
    grid = dev.get("grid", [])
    if (
        len(grid) != 6
        or {(r["depth"], r["fusion"]) for r in grid} != set(tuning_grid())
        or dev["selected"] != select_setting(grid)
    ):
        raise PreflightError("dev 격자·선택 규칙 불일치")
    for r in grid:
        if (
            r["selection_score"]
            != (r["gold_dev_mean_ndcg10"] + r["silver_dev_mean_ndcg10"]) / 2
        ):
            raise PreflightError("dev 선택 점수 불일치")
    _check_fingerprint(root / SCORES_PATH, dev["score_cache"])
    selected = dev["selected"]
    if (selected["depth"], selected["fusion"]) not in tuning_grid():
        raise PreflightError("M8 격자 밖 선택")
    payload = {
        "depth": selected["depth"],
        "fusion": selected["fusion"],
        "max_length": dev["max_length"],
        "model_id": RERANKER_MODEL_ID,
        "model_revision": RERANKER_REVISION,
        "rerank_rank_constant": RANK_CONSTANT,
        "m5_config_hash": M5_CONFIG_HASH,
        "m8_dev_sha256": sha256_file(root / DEV_PATH),
        "m8_max_length_sha256": sha256_file(root / MAX_LENGTH_PATH),
        "score_cache": dev["score_cache"],
        "candidate_cache": fingerprint(root / CANDIDATES_PATH),
        "rules_sha256": current_rules_hash(root),
        "device": dev["device"],
        "batch_size": dev["batch_size"],
        "frozen_at": now_iso(),
        "code_commit": _git_commit(root),
    }
    payload["config_hash"] = _config_hash(payload)
    _write_json_exclusive(root / FREEZE_PATH, payload)
    return payload


def verify_config(path):
    path = Path(path)
    root = path.parent.parent.parent
    mismatches = []
    try:
        value = _read_json_object(path)
        if value["config_hash"] != _config_hash(
            {k: v for k, v in value.items() if k != "config_hash"}
        ):
            mismatches.append("config_hash")
        if value["rules_sha256"] != current_rules_hash(root):
            mismatches.append("rules_sha256")
        if value["m5_config_hash"] != M5_CONFIG_HASH:
            mismatches.append("m5_config_hash")
        for field, artifact in (
            ("m8_dev_sha256", DEV_PATH),
            ("m8_max_length_sha256", MAX_LENGTH_PATH),
        ):
            if value[field] != sha256_file(root / artifact):
                mismatches.append(field)
        for field, artifact in (
            ("candidate_cache", CANDIDATES_PATH),
            ("score_cache", SCORES_PATH),
        ):
            if (root / artifact).exists():
                _check_fingerprint(root / artifact, value[field])
    except (OSError, ValueError, KeyError, TypeError, PreflightError):
        mismatches.append("artifact")
    return ConfigCheck(tuple(mismatches))


def main(argv=None):
    parser = argparse.ArgumentParser(description="M8 dev 리랭커 실험 (사용자 실행)")
    group = parser.add_mutually_exclusive_group()
    for flag in ("candidates", "tokens", "score-grid", "freeze"):
        group.add_argument("--" + flag, action="store_true")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--device", default="mps")
    parser.add_argument("--batch-size", type=int, default=4)
    args = parser.parse_args(argv)
    try:
        root = args.root
        if args.freeze:
            freeze_config(root=root)
        elif args.tokens:
            from transformers import AutoTokenizer

            path = resolve_local_reranker_snapshot()
            measure_tokens(
                AutoTokenizer.from_pretrained(str(path), local_files_only=True),
                path,
                root=root,
            )
        else:
            _not_frozen(root)
            if not args.candidates:
                dev_inputs(root)  # Reject before any model initialization.
            gold, silver = _load_dev_questions(root)
            _require_registered_dev_counts(gold, silver)
            config = _read_json_object(root / "data/manifest/m5_config.json")
            embedder = _make_embedder("bge-m3", cache_root=root)
            with OpenSearchClient() as client:
                preflight_indexes(client, config)
                if args.candidates:
                    if not (root / CANDIDATES_PATH).exists():
                        embedder.prepare()
                    collect_candidates(
                        client, embedder, gold + silver, config, root=root
                    )
                else:
                    run_dev(
                        client,
                        embedder,
                        gold + silver,
                        config,
                        root=root,
                        device=args.device,
                        batch_size=args.batch_size,
                    )
        print("M8 단계 완료", flush=True)
        return 0
    except PreflightError as exc:
        print(f"M8 사전 검사 실패: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - never print service/model payloads
        print(f"M8 실행 실패: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
