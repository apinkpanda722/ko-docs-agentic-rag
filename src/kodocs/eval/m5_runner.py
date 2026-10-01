"""Authorization-aware runner for deterministic M5 retrieval experiments."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from kodocs.eval.m5_inputs import EvalQuestion
from kodocs.eval.m5_metrics import (
    Span,
    ndcg_at_k,
    recall_at_k,
    span_hit_at_k,
    top1_hit,
)
from kodocs.retrieval.access import UserContext
from kodocs.retrieval.embedding import Embedder
from kodocs.retrieval.index_schema import index_name
from kodocs.retrieval.opensearch import OpenSearchClient
from kodocs.retrieval.search import SearchParams, authorized_search

Condition = Literal["composite", "pypdf", "upstage"]
Chunking = Literal["structural", "fixed"]
Method = Literal["bm25", "knn", "hybrid_rrf", "hybrid_weighted"]
Analyzer = Literal["standard", "nori"]

_PROGRESS_EVERY = 10


@dataclass(frozen=True)
class HybridSetting:
    """Shared candidate count and weighted-fusion weight for the 14-run matrix.

    RRF ignores ``bm25_weight`` while weighted hybrid uses it.  Keeping both
    values in one object lets the full matrix report RRF and weighted hybrid at
    one fixed candidate count without duplicating the analyzer-independent kNN
    rows.
    """

    candidate_chunks: int
    bm25_weight: float
    rank_constant: int = 60

    def __post_init__(self) -> None:
        SearchParams(
            "hybrid_rrf",
            candidate_chunks=self.candidate_chunks,
            rank_constant=self.rank_constant,
        )
        SearchParams(
            "hybrid_weighted",
            candidate_chunks=self.candidate_chunks,
            bm25_weight=self.bm25_weight,
        )


@dataclass(frozen=True)
class RunConfig:
    """One condition in the preregistered M5 retrieval matrix."""

    condition: Condition
    chunking: Chunking
    method: Method
    analyzer: Analyzer
    candidate_chunks: int
    bm25_weight: float | None
    merged_analyzers: bool = field(init=False)

    def __post_init__(self) -> None:
        if self.condition not in {"composite", "pypdf", "upstage"}:
            raise ValueError("unsupported parser condition")
        if self.chunking not in {"structural", "fixed"}:
            raise ValueError("unsupported chunking")
        if self.condition != "composite" and self.chunking != "structural":
            raise ValueError("parser comparison indexes use structural chunking")
        SearchParams(
            self.method,
            analyzer=self.analyzer,
            candidate_chunks=self.candidate_chunks,
            bm25_weight=self.bm25_weight,
        )
        object.__setattr__(self, "merged_analyzers", self.method == "knn")


@dataclass(frozen=True)
class QuestionRun:
    """Metrics and ranked evidence for one evaluated question."""

    qid: str
    ranked_docs: tuple[str, ...]
    top10_chunks: tuple[Span, ...]
    recall10: float
    ndcg10: float
    span_hit: bool | None
    top1: bool


class QuestionRunError(RuntimeError):
    """One question failed, invalidating the entire configuration run."""

    def __init__(self, qid: str, reason: str) -> None:
        self.qid = qid
        self.reason = reason
        super().__init__(f"{qid}: {reason}")


def all_combo_configs(
    hybrid: HybridSetting, *, condition: Condition = "composite"
) -> list[RunConfig]:
    """Return BM25 4 + RRF 4 + weighted 4 + merged-kNN 2 = 14 runs."""
    if condition != "composite":
        raise ValueError("the 14-run matrix is defined only for composite indexes")
    configs: list[RunConfig] = []
    for method in ("bm25", "hybrid_rrf", "hybrid_weighted"):
        for chunking in ("fixed", "structural"):
            for analyzer in ("standard", "nori"):
                configs.append(
                    RunConfig(
                        condition=condition,
                        chunking=chunking,
                        method=method,
                        analyzer=analyzer,
                        candidate_chunks=hybrid.candidate_chunks,
                        bm25_weight=(
                            hybrid.bm25_weight if method == "hybrid_weighted" else None
                        ),
                    )
                )
    configs.extend(
        RunConfig(
            condition=condition,
            chunking=chunking,
            method="knn",
            analyzer="nori",
            candidate_chunks=hybrid.candidate_chunks,
            bm25_weight=None,
        )
        for chunking in ("fixed", "structural")
    )
    return configs


def run_questions(
    client: OpenSearchClient,
    embedder: Embedder,
    questions: Sequence[EvalQuestion],
    cfg: RunConfig,
    *,
    prefer_current: bool = False,
) -> list[QuestionRun]:
    """Search every question with its asker context or fail the whole run."""
    results: list[QuestionRun] = []
    total = len(questions)
    for number, question in enumerate(questions, start=1):
        qid = question.question.id
        try:
            asker = question.question.asker
            search_kwargs = {"prefer_current": True} if prefer_current else {}
            search_result = authorized_search(
                client,
                index_name(cfg.condition, cfg.chunking),
                UserContext(team=asker.team, grade=asker.grade),
                question.question.question,
                _search_params(cfg),
                embedder,
                **search_kwargs,
            )
            ranked_docs = tuple(hit.doc_id for hit in search_result.docs)
            chunks = tuple(
                Span(hit.doc_id, hit.start, hit.end)
                for hit in search_result.chunks[:10]
            )
            results.append(
                QuestionRun(
                    qid=qid,
                    ranked_docs=ranked_docs,
                    top10_chunks=chunks,
                    recall10=recall_at_k(ranked_docs, question.rel_by_doc),
                    ndcg10=ndcg_at_k(ranked_docs, question.rel_by_doc),
                    span_hit=(
                        span_hit_at_k(chunks, question.answer_ranges)
                        if question.answer_ranges
                        else None
                    ),
                    top1=top1_hit(ranked_docs, question.rel_by_doc),
                )
            )
        except Exception as exc:
            reason = _failure_reason(exc)
            print(
                f"M5 검색 실패 qid={qid} reason={reason}",
                file=sys.stderr,
                flush=True,
            )
            raise QuestionRunError(qid, reason) from exc
        if number % _PROGRESS_EVERY == 0 or number == total:
            print(
                f"M5 검색 진행 {number}/{total} condition={cfg.condition} "
                f"chunking={cfg.chunking} method={cfg.method} "
                f"analyzer={cfg.analyzer}",
                flush=True,
            )
    return results


def _search_params(config: RunConfig) -> SearchParams:
    return SearchParams(
        method=config.method,
        analyzer=config.analyzer,
        candidate_chunks=config.candidate_chunks,
        bm25_weight=config.bm25_weight,
        rank_constant=60,
    )


def _failure_reason(exc: Exception) -> str:
    detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return f"{type(exc).__name__}: {detail}"[:300]
