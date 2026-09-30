"""Deterministic document and answer-span metrics for M5 retrieval."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


def doc_relevance(qrels: Sequence[tuple[str, int]]) -> dict[str, int]:
    """Collapse qrels to the maximum linear relevance grade per document."""
    relevance: dict[str, int] = {}
    for doc_id, grade in qrels:
        _check_grade(grade)
        relevance[doc_id] = max(grade, relevance.get(doc_id, 0))
    return relevance


def recall_at_k(
    ranked_doc_ids: Sequence[str],
    rel_by_doc: Mapping[str, int],
    k: int = 10,
) -> float:
    """Return relevant-document recall among the first ``k`` unique documents.

    A document ranking must not contain duplicate ids; duplicates raise
    ``ValueError`` rather than being counted more than once.  Queries without a
    relevant truth document also raise ``ValueError`` and must be filtered by
    the caller before metric aggregation.
    """
    _check_k(k)
    _check_unique_ranking(ranked_doc_ids)
    relevant = _relevant_documents(rel_by_doc)
    hits = sum(doc_id in relevant for doc_id in ranked_doc_ids[:k])
    return hits / len(relevant)


def ndcg_at_k(
    ranked_doc_ids: Sequence[str],
    rel_by_doc: Mapping[str, int],
    k: int = 10,
) -> float:
    """Return NDCG using linear relevance grades and log-base-two discount.

    A document ranking must not contain duplicate ids; duplicates raise
    ``ValueError``.  Queries whose ideal DCG is zero also raise ``ValueError``
    and must be filtered by the caller before metric aggregation.
    """
    _check_k(k)
    _check_unique_ranking(ranked_doc_ids)
    _relevant_documents(rel_by_doc)
    dcg = _dcg([rel_by_doc.get(doc_id, 0) for doc_id in ranked_doc_ids[:k]])
    ideal_grades = sorted(rel_by_doc.values(), reverse=True)[:k]
    ideal_dcg = _dcg(ideal_grades)
    if ideal_dcg == 0:
        raise ValueError("at least one relevant document is required")
    return dcg / ideal_dcg


def top1_hit(ranked_doc_ids: Sequence[str], rel_by_doc: Mapping[str, int]) -> bool:
    """Return whether the first unique ranked document has positive relevance."""
    _check_unique_ranking(ranked_doc_ids)
    _check_relevance_mapping(rel_by_doc)
    return bool(ranked_doc_ids and rel_by_doc.get(ranked_doc_ids[0], 0) >= 1)


@dataclass(frozen=True)
class Span:
    """A half-open character range within one document."""

    doc_id: str
    start: int
    end: int


def span_hit_at_k(
    retrieved: Sequence[Span],
    answer_spans: Sequence[Span],
    k: int = 10,
) -> bool:
    """Return whether a top-k chunk overlaps an answer span by at least one char."""
    _check_k(k)
    return any(
        chunk.doc_id == answer.doc_id
        and max(chunk.start, answer.start) < min(chunk.end, answer.end)
        for chunk in retrieved[:k]
        for answer in answer_spans
    )


def _dcg(grades: Sequence[int]) -> float:
    return math.fsum(
        grade / math.log2(rank + 1) for rank, grade in enumerate(grades, start=1)
    )


def _relevant_documents(rel_by_doc: Mapping[str, int]) -> set[str]:
    _check_relevance_mapping(rel_by_doc)
    relevant = {doc_id for doc_id, grade in rel_by_doc.items() if grade >= 1}
    if not relevant:
        raise ValueError("at least one relevant document is required")
    return relevant


def _check_relevance_mapping(rel_by_doc: Mapping[str, int]) -> None:
    for grade in rel_by_doc.values():
        _check_grade(grade)


def _check_grade(grade: int) -> None:
    if isinstance(grade, bool) or not isinstance(grade, int) or grade not in {0, 1, 2}:
        raise ValueError("relevance grade must be one of 0, 1, or 2")


def _check_unique_ranking(ranked_doc_ids: Sequence[str]) -> None:
    if len(set(ranked_doc_ids)) != len(ranked_doc_ids):
        raise ValueError("document ranking contains a duplicate document id")


def _check_k(k: int) -> None:
    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
        raise ValueError("k must be a positive integer")
