"""Tests for deterministic M5 retrieval metrics."""

import math

import pytest

from kodocs.eval.m5_metrics import (
    Span,
    doc_relevance,
    ndcg_at_k,
    recall_at_k,
    span_hit_at_k,
    top1_hit,
)


def test_doc_relevance_keeps_maximum_grade_for_each_document() -> None:
    assert doc_relevance(
        [("high", 1), ("irrelevant", 0), ("high", 2), ("medium", 1)]
    ) == {"high": 2, "irrelevant": 0, "medium": 1}


def test_recall_and_ndcg_for_perfect_reversed_and_partial_rankings() -> None:
    relevance = {"high": 2, "medium": 1, "irrelevant": 0}
    ideal_dcg = 2.0 + 1.0 / math.log2(3)

    assert recall_at_k(["high", "medium"], relevance, k=2) == 1.0
    assert ndcg_at_k(["high", "medium"], relevance, k=2) == 1.0

    reversed_dcg = 1.0 + 2.0 / math.log2(3)
    assert recall_at_k(["medium", "high"], relevance, k=2) == 1.0
    assert ndcg_at_k(["medium", "high"], relevance, k=2) == pytest.approx(
        reversed_dcg / ideal_dcg
    )

    assert recall_at_k(["high", "missing"], relevance, k=2) == 0.5
    assert ndcg_at_k(["high", "missing"], relevance, k=2) == pytest.approx(
        2.0 / ideal_dcg
    )


@pytest.mark.parametrize("metric", [recall_at_k, ndcg_at_k])
def test_document_rank_metrics_reject_duplicate_document_ids(metric) -> None:
    with pytest.raises(ValueError, match="duplicate document id"):
        metric(["doc-1", "doc-1"], {"doc-1": 2})


@pytest.mark.parametrize("metric", [recall_at_k, ndcg_at_k])
def test_document_rank_metrics_reject_queries_without_relevant_truth(metric) -> None:
    with pytest.raises(ValueError, match="relevant document"):
        metric(["doc-1"], {"doc-1": 0})


def test_top1_hit_uses_positive_relevance_and_handles_an_empty_ranking() -> None:
    relevance = {"high": 2, "medium": 1, "irrelevant": 0}

    assert top1_hit(["high", "irrelevant"], relevance) is True
    assert top1_hit(["irrelevant", "high"], relevance) is False
    assert top1_hit([], relevance) is False


def test_span_hit_requires_positive_half_open_overlap_in_the_same_document() -> None:
    answer = [Span("doc-1", 10, 20)]

    assert span_hit_at_k([Span("doc-1", 0, 10)], answer) is False
    assert span_hit_at_k([Span("doc-1", 19, 25)], answer) is True
    assert span_hit_at_k([Span("doc-2", 10, 20)], answer) is False


def test_span_hit_only_considers_the_first_k_chunks() -> None:
    retrieved = [Span("other", 0, 1), Span("doc-1", 10, 11)]

    assert span_hit_at_k(retrieved, [Span("doc-1", 10, 20)], k=1) is False
    assert span_hit_at_k(retrieved, [Span("doc-1", 10, 20)], k=2) is True


def test_recall_and_ndcg_use_only_the_supplied_visible_relevance_mapping() -> None:
    visible_relevance = {"visible-high": 2, "visible-low": 1}

    assert recall_at_k(["visible-high", "visible-low"], visible_relevance, k=10) == 1.0
    assert ndcg_at_k(["visible-high", "visible-low"], visible_relevance, k=10) == 1.0
