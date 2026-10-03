"""Authorized BM25, vector, and hybrid search over M5 chunk indexes."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from kodocs.retrieval.access import UserContext, access_filter
from kodocs.retrieval.embedding import Embedder
from kodocs.retrieval.index_schema import (
    RRF_PIPELINE_NAME,
    weighted_pipeline_name,
)
from kodocs.retrieval.opensearch import OpenSearchClient
from kodocs.retrieval.ordering import prefer_current_order

SearchMethod = Literal["bm25", "knn", "hybrid_rrf", "hybrid_weighted"]
Analyzer = Literal["standard", "nori"]

_VECTOR_METHODS = frozenset({"knn", "hybrid_rrf", "hybrid_weighted"})
_SOURCE_FIELDS = (
    "chunk_id",
    "doc_id",
    "start",
    "end",
    "section_key",
    "section_kind",
    "text_nori",
    "title",
    "is_current",
    "rule_id",
    "date",
)


@dataclass(frozen=True)
class SearchParams:
    """One registered M5 search condition."""

    method: SearchMethod
    analyzer: Analyzer = "nori"
    candidate_chunks: int = 100
    bm25_weight: float | None = None
    rank_constant: int = 60

    def __post_init__(self) -> None:
        if self.method not in {"bm25", "knn", "hybrid_rrf", "hybrid_weighted"}:
            raise ValueError("unsupported search method")
        if self.analyzer not in {"standard", "nori"}:
            raise ValueError("unsupported analyzer")
        if (
            not isinstance(self.candidate_chunks, int)
            or isinstance(self.candidate_chunks, bool)
            or self.candidate_chunks <= 0
        ):
            raise ValueError("candidate_chunks must be a positive integer")
        if (
            not isinstance(self.rank_constant, int)
            or isinstance(self.rank_constant, bool)
            or self.rank_constant <= 0
        ):
            raise ValueError("rank_constant must be a positive integer")
        if self.method == "hybrid_rrf" and self.rank_constant != 60:
            raise ValueError("only the registered rank_constant=60 is available")
        if self.method == "hybrid_weighted":
            if self.bm25_weight is None:
                raise ValueError("hybrid_weighted requires bm25_weight")
            weighted_pipeline_name(self.bm25_weight)
        elif self.bm25_weight is not None:
            raise ValueError("bm25_weight is only valid for hybrid_weighted")


@dataclass(frozen=True)
class ChunkHit:
    """One ranked chunk returned by OpenSearch."""

    chunk_id: str
    doc_id: str
    score: float
    start: int
    end: int
    section_key: str | None
    section_kind: str | None
    text: str | None = None
    title: str | None = None
    is_current: bool | None = None
    rule_id: str | None = None


@dataclass(frozen=True)
class DocHit:
    """One document ranked by its highest-scoring returned chunk."""

    doc_id: str
    score: float
    best_chunk_id: str
    title: str | None = None
    is_current: bool | None = None
    rule_id: str | None = None
    date: str | None = None  # newest chunk date; filled by identifier lookup only


@dataclass(frozen=True)
class SearchResult:
    """Deterministically ranked chunk and document search results."""

    chunks: tuple[ChunkHit, ...]
    docs: tuple[DocHit, ...]
    embed_ms: float | None
    search_ms: float


def search_body(
    ctx: UserContext,
    query: str,
    params: SearchParams,
    *,
    vector: Sequence[float] | None = None,
) -> dict:
    """Build a pure OpenSearch request body with every branch pre-filtered."""
    if not isinstance(query, str):
        raise TypeError("query must be a string")

    text_query = _text_query(ctx, query, params)
    if params.method == "bm25":
        search_query = text_query
    else:
        knn_query = _knn_query(ctx, vector, params.candidate_chunks)
        search_query = (
            knn_query
            if params.method == "knn"
            else {"hybrid": {"queries": [text_query, knn_query]}}
        )

    return {
        "size": params.candidate_chunks,
        "track_total_hits": False,
        "_source": list(_SOURCE_FIELDS),
        "query": search_query,
    }


def authorized_search(
    client: OpenSearchClient,
    index: str,
    ctx: UserContext,
    query: str,
    params: SearchParams,
    embedder: Embedder | None = None,
    *,
    prefer_current: bool = False,
) -> SearchResult:
    """Run one search whose authorization is enforced inside every query branch."""
    vector: Sequence[float] | None = None
    embed_ms: float | None = None
    if params.method in _VECTOR_METHODS:
        if embedder is None:
            raise ValueError("embedder is required for vector search")
        embed_started = time.perf_counter()
        vectors = embedder.embed([query], kind="query")
        embed_ms = (time.perf_counter() - embed_started) * 1000.0
        if len(vectors) != 1:
            raise RuntimeError("embedder returned an unexpected vector count")
        vector = vectors[0]

    body = search_body(ctx, query, params, vector=vector)
    pipeline = _pipeline_name(params)
    path = f"/{index}/_search"
    if pipeline is not None:
        path = f"{path}?search_pipeline={pipeline}"

    search_started = time.perf_counter()
    response = client.request("POST", path, json=body)
    search_ms = (time.perf_counter() - search_started) * 1000.0
    chunks = _chunk_hits(response, limit=params.candidate_chunks)
    if prefer_current:
        chunks = prefer_current_order(
            chunks,
            key_rule=lambda hit: hit.rule_id,
            key_is_current=lambda hit: hit.is_current,
        )
    return SearchResult(
        chunks=chunks,
        docs=_doc_hits(chunks, prefer_current=prefer_current),
        embed_ms=embed_ms,
        search_ms=search_ms,
    )


def _text_query(ctx: UserContext, query: str, params: SearchParams) -> dict:
    field = "text_std" if params.analyzer == "standard" else "text_nori"
    return {
        "bool": {
            "must": {"match": {field: query}},
            "filter": access_filter(ctx),
        }
    }


def _knn_query(
    ctx: UserContext,
    vector: Sequence[float] | None,
    candidate_chunks: int,
) -> dict:
    if vector is None:
        raise ValueError("vector is required for vector search")
    checked_vector = _checked_vector(vector)
    return {
        "knn": {
            "vector": {
                "vector": checked_vector,
                "k": candidate_chunks,
                "filter": access_filter(ctx),
            }
        }
    }


def _checked_vector(vector: Sequence[float]) -> list[float]:
    if isinstance(vector, (str, bytes)) or not vector:
        raise ValueError("vector must be a non-empty numeric sequence")
    checked: list[float] = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError("vector values must be finite numbers")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("vector values must be finite numbers")
        checked.append(number)
    return checked


def _pipeline_name(params: SearchParams) -> str | None:
    if params.method == "hybrid_rrf":
        return RRF_PIPELINE_NAME
    if params.method == "hybrid_weighted":
        if params.bm25_weight is None:
            raise RuntimeError("validated weighted search has no weight")
        return weighted_pipeline_name(params.bm25_weight)
    return None


def _chunk_hits(response: Mapping[str, object], *, limit: int) -> tuple[ChunkHit, ...]:
    hits_wrapper = response.get("hits")
    if not isinstance(hits_wrapper, Mapping):
        raise TypeError("OpenSearch search response has invalid hits")
    raw_hits = hits_wrapper.get("hits")
    if not isinstance(raw_hits, list):
        raise TypeError("OpenSearch search response has invalid hits")

    chunks = [_chunk_hit(raw_hit) for raw_hit in raw_hits]
    chunks.sort(key=lambda hit: (-hit.score, hit.chunk_id))
    return tuple(chunks[:limit])


def _chunk_hit(raw_hit: object) -> ChunkHit:
    if not isinstance(raw_hit, Mapping):
        raise TypeError("OpenSearch search response has an invalid hit")
    source = raw_hit.get("_source")
    if not isinstance(source, Mapping):
        raise TypeError("OpenSearch search response has an invalid hit")
    score_value = raw_hit.get("_score")
    if isinstance(score_value, bool) or not isinstance(score_value, int | float):
        raise TypeError("OpenSearch search response has an invalid score")
    score = float(score_value)
    if not math.isfinite(score):
        raise RuntimeError("OpenSearch search response has an invalid score")

    chunk_id = _required_str(source, "chunk_id")
    doc_id = _required_str(source, "doc_id")
    start = _required_int(source, "start")
    end = _required_int(source, "end")
    if start < 0 or end < start:
        raise RuntimeError("OpenSearch search response has an invalid range")
    return ChunkHit(
        chunk_id=chunk_id,
        doc_id=doc_id,
        score=score,
        start=start,
        end=end,
        section_key=_optional_str(source, "section_key"),
        section_kind=_optional_str(source, "section_kind"),
        text=_optional_str(source, "text_nori"),
        title=_optional_str(source, "title"),
        is_current=_optional_bool(source, "is_current"),
        rule_id=_optional_str(source, "rule_id"),
    )


def _required_str(source: Mapping[str, object], field: str) -> str:
    value = source.get(field)
    if not isinstance(value, str) or not value:
        raise RuntimeError("OpenSearch search response has an invalid source field")
    return value


def _required_int(source: Mapping[str, object], field: str) -> int:
    value = source.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("OpenSearch search response has an invalid source field")
    return value


def _optional_str(source: Mapping[str, object], field: str) -> str | None:
    value = source.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("OpenSearch search response has an invalid source field")
    return value


def _optional_bool(source: Mapping[str, object], field: str) -> bool | None:
    value = source.get(field)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise TypeError("OpenSearch search response has an invalid source field")
    return value


def _doc_hits(
    chunks: Sequence[ChunkHit], *, prefer_current: bool = False
) -> tuple[DocHit, ...]:
    best_by_doc: dict[str, ChunkHit] = {}
    for chunk in chunks:
        if chunk.doc_id not in best_by_doc:
            best_by_doc[chunk.doc_id] = chunk
    docs = [
        DocHit(
            doc_id=doc_id,
            score=chunk.score,
            best_chunk_id=chunk.chunk_id,
            title=chunk.title,
            is_current=chunk.is_current,
            rule_id=chunk.rule_id,
        )
        for doc_id, chunk in best_by_doc.items()
    ]
    docs.sort(key=lambda hit: (-hit.score, hit.doc_id))
    if prefer_current:
        return prefer_current_order(
            docs,
            key_rule=lambda hit: hit.rule_id,
            key_is_current=lambda hit: hit.is_current,
        )
    return tuple(docs)
