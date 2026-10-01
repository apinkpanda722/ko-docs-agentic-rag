"""Tests for exact, pre-filtered identifier lookup."""

from __future__ import annotations

import json

import httpx
import pytest

from kodocs.retrieval.access import UserContext, access_filter
from kodocs.retrieval.lookup import (
    authorized_lookup_id,
    extract_query_identifiers,
)
from kodocs.retrieval.opensearch import OpenSearchClient


def test_extract_query_identifiers_preserves_source_order_and_normalizes_rules() -> (
    None
):
    question = (
        "PAY-1234와 cloud-pay, EXP-2026-0412, INC-2025-007, "
        "출장 규칙 제8조를 확인해 주세요. PAY-1234도 재확인해 주세요."
    )

    assert extract_query_identifiers(
        question,
        service_names=("cloud", "cloud-pay"),
    ) == [
        ("ticket", "PAY-1234"),
        ("service", "cloud-pay"),
        ("approval", "EXP-2026-0412"),
        ("incident", "INC-2025-007"),
        ("regulation", "출장규칙제8조"),
        ("ticket", "PAY-1234"),
    ]


def test_extract_query_identifiers_respects_exact_boundaries() -> None:
    assert (
        extract_query_identifiers(
            "XPAY-1234 PAY-12345 cloud-payments",
            service_names=("cloud-pay",),
        )
        == []
    )


def test_ranked_lookup_uses_term_and_access_filters_and_groups_documents() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "hits": {
                    "hits": [
                        {
                            "_score": 0.7,
                            "_source": {
                                "chunk_id": "doc-b#2",
                                "doc_id": "doc-b",
                                "date": "2026-01-01",
                            },
                        },
                        {
                            "_score": 0.9,
                            "_source": {
                                "chunk_id": "doc-b#1",
                                "doc_id": "doc-b",
                                "date": "2026-01-01",
                            },
                        },
                        {
                            "_score": 0.9,
                            "_source": {
                                "chunk_id": "doc-a#1",
                                "doc_id": "doc-a",
                                "date": "2025-01-01",
                            },
                        },
                    ]
                }
            },
        )

    client = OpenSearchClient(
        "http://opensearch.test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    ctx = UserContext("payments", 3)

    hits = authorized_lookup_id(
        client,
        "kodocs-m5-composite-structural",
        ctx,
        "ticket",
        "PAY-1234",
        rank_query="PAY-1234 지급 승인",
        size=3,
    )

    assert [(hit.doc_id, hit.score, hit.best_chunk_id) for hit in hits] == [
        ("doc-a", 0.9, "doc-a#1"),
        ("doc-b", 0.9, "doc-b#1"),
    ]
    body = json.loads(captured[0].content)
    assert body["query"] == {
        "bool": {
            "filter": [
                access_filter(ctx),
                {"term": {"ids_ticket": "PAY-1234"}},
            ],
            "should": [{"match": {"text_nori": "PAY-1234 지급 승인"}}],
        }
    }
    assert body["sort"] == [
        {"_score": {"order": "desc"}},
        {"doc_id": {"order": "asc"}},
        {"chunk_id": {"order": "asc"}},
    ]
    assert body["size"] == 3


def test_unranked_lookup_normalizes_regulation_and_orders_by_date_then_doc() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "hits": {
                    "hits": [
                        {
                            "_score": 0.0,
                            "_source": {
                                "chunk_id": "older#1",
                                "doc_id": "older",
                                "date": "2025-03-01",
                            },
                        },
                        {
                            "_score": 0.0,
                            "_source": {
                                "chunk_id": "newer-b#1",
                                "doc_id": "newer-b",
                                "date": "2026-03-01",
                            },
                        },
                        {
                            "_score": 0.0,
                            "_source": {
                                "chunk_id": "newer-a#1",
                                "doc_id": "newer-a",
                                "date": "2026-03-01",
                            },
                        },
                    ]
                }
            },
        )

    client = OpenSearchClient(
        "http://opensearch.test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    hits = authorized_lookup_id(
        client,
        "index",
        UserContext("legal", 1),
        "regulation",
        "출장 규칙 제 8 조",
    )

    assert [hit.doc_id for hit in hits] == ["newer-a", "newer-b", "older"]
    body = json.loads(captured[0].content)
    assert body["query"]["bool"]["filter"][1] == {
        "term": {"ids_regulation": "출장규칙제8조"}
    }
    assert body["sort"] == [
        {"date": {"order": "desc", "missing": "_last"}},
        {"doc_id": {"order": "asc"}},
        {"chunk_id": {"order": "asc"}},
    ]


def test_prefer_current_lookup_reorders_same_rule_and_keeps_access_filter() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "hits": {
                    "hits": [
                        {
                            "_score": 3.0,
                            "_source": {
                                "chunk_id": "prior#1",
                                "doc_id": "prior",
                                "date": "2025-01-01",
                                "title": "출장규칙",
                                "is_current": False,
                                "rule_id": "rule-a",
                            },
                        },
                        {
                            "_score": 2.0,
                            "_source": {
                                "chunk_id": "other#1",
                                "doc_id": "other",
                                "date": "2026-01-01",
                                "title": "다른 규정",
                                "is_current": True,
                                "rule_id": "rule-b",
                            },
                        },
                        {
                            "_score": 1.0,
                            "_source": {
                                "chunk_id": "current#1",
                                "doc_id": "current",
                                "date": "2026-01-01",
                                "title": "출장규칙",
                                "is_current": True,
                                "rule_id": "rule-a",
                            },
                        },
                    ]
                }
            },
        )

    client = OpenSearchClient(
        client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    ctx = UserContext("legal", 2)

    baseline = authorized_lookup_id(
        client,
        "index",
        ctx,
        "regulation",
        "출장 규칙 제8조",
        rank_query="출장 규칙",
    )
    preferred = authorized_lookup_id(
        client,
        "index",
        ctx,
        "regulation",
        "출장 규칙 제8조",
        rank_query="출장 규칙",
        prefer_current=True,
    )

    assert [hit.doc_id for hit in baseline] == ["prior", "other", "current"]
    assert [hit.doc_id for hit in preferred] == ["current", "other", "prior"]
    assert preferred[0].title == "출장규칙"
    assert preferred[0].is_current is True
    assert preferred[0].rule_id == "rule-a"
    assert captured[0] == captured[1]
    assert captured[0]["query"]["bool"]["filter"][0] == access_filter(ctx)  # type: ignore[index]


def test_missing_and_unauthorized_lookups_have_the_same_empty_result() -> None:
    client = OpenSearchClient(
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"hits": {"hits": []}})
            )
        )
    )
    ctx = UserContext("finance", 1)

    missing = authorized_lookup_id(client, "index", ctx, "ticket", "PAY-9999")
    unauthorized = authorized_lookup_id(client, "index", ctx, "ticket", "PAY-1234")

    assert missing == unauthorized == ()
    assert "권한" not in repr(unauthorized)


@pytest.mark.parametrize("kind", ["unknown", "tickets", ""])
def test_lookup_rejects_unknown_identifier_kinds(kind: str) -> None:
    client = OpenSearchClient()
    with pytest.raises(ValueError, match="kind"):
        authorized_lookup_id(
            client,
            "index",
            UserContext("platform", 1),
            kind,  # type: ignore[arg-type]
            "PAY-1234",
        )
