"""Unit tests for pre-filtered M5 OpenSearch queries and result grouping."""

from __future__ import annotations

import json
from collections.abc import Mapping

import httpx
import pytest

from kodocs.retrieval.access import UserContext, access_filter
from kodocs.retrieval.embedding import FakeEmbedder
from kodocs.retrieval.index_schema import RRF_PIPELINE_NAME, weighted_pipeline_name
from kodocs.retrieval.opensearch import OpenSearchClient
from kodocs.retrieval.search import (
    SearchParams,
    authorized_search,
    search_body,
)


class RecordingEmbedder(FakeEmbedder):
    """Record the query-kind call made by authorized_search."""

    def __init__(self) -> None:
        super().__init__(dim=4)
        self.calls: list[tuple[tuple[str, ...], str]] = []

    def embed(
        self,
        texts: list[str] | tuple[str, ...],
        *,
        kind: str,
    ) -> list[list[float]]:
        self.calls.append((tuple(texts), kind))
        return super().embed(texts, kind=kind)  # type: ignore[arg-type]


def assert_every_subquery_prefiltered(body: Mapping[str, object]) -> None:
    """Recursively prove every text/vector search branch has a pre-filter."""
    expected_filter = access_filter(UserContext(team="platform", grade=2))
    found = 0

    def visit(query: object) -> None:
        nonlocal found
        assert isinstance(query, Mapping)
        if "hybrid" in query:
            hybrid = query["hybrid"]
            assert isinstance(hybrid, Mapping)
            queries = hybrid["queries"]
            assert isinstance(queries, list)
            for subquery in queries:
                visit(subquery)
            return
        if "bool" in query:
            boolean = query["bool"]
            assert isinstance(boolean, Mapping)
            assert boolean["filter"] == expected_filter
            must = boolean["must"]
            assert isinstance(must, Mapping) and "match" in must
            found += 1
            return
        if "knn" in query:
            knn = query["knn"]
            assert isinstance(knn, Mapping) and len(knn) == 1
            vector_query = next(iter(knn.values()))
            assert isinstance(vector_query, Mapping)
            assert vector_query["filter"] == expected_filter
            found += 1
            return
        raise AssertionError(f"unexpected search query: {query}")

    visit(body["query"])
    assert found >= 1


@pytest.mark.parametrize(
    ("params", "vector", "expected_subqueries"),
    [
        (SearchParams("bm25", analyzer="standard"), None, 1),
        (SearchParams("knn"), [1.0, 0.0], 1),
        (SearchParams("hybrid_rrf"), [1.0, 0.0], 2),
        (
            SearchParams("hybrid_weighted", bm25_weight=0.4),
            [1.0, 0.0],
            2,
        ),
    ],
)
def test_every_search_method_prefilters_every_subquery(
    params: SearchParams,
    vector: list[float] | None,
    expected_subqueries: int,
) -> None:
    body = search_body(
        UserContext(team="platform", grade=2),
        "배포 승인",
        params,
        vector=vector,
    )

    assert_every_subquery_prefiltered(body)
    assert body["size"] == params.candidate_chunks
    assert body["track_total_hits"] is False
    assert (
        json.dumps(body, ensure_ascii=False).count('"match"')
        + json.dumps(body, ensure_ascii=False).count('"knn"')
        == expected_subqueries
    )


def test_text_field_and_knn_candidate_count_follow_search_params() -> None:
    ctx = UserContext(team="platform", grade=2)
    bm25 = search_body(
        ctx,
        "검색어",
        SearchParams("bm25", analyzer="standard", candidate_chunks=7),
    )
    knn = search_body(
        ctx,
        "검색어",
        SearchParams("knn", analyzer="standard", candidate_chunks=7),
        vector=[0.25, 0.75],
    )

    assert bm25["query"]["bool"]["must"] == {  # type: ignore[index]
        "match": {"text_std": "검색어"}
    }
    assert knn["query"]["knn"]["vector"]["k"] == 7  # type: ignore[index]
    assert "text_std" not in json.dumps(knn)


@pytest.mark.parametrize(
    "params",
    [SearchParams("knn"), SearchParams("hybrid_rrf")],
)
def test_vector_search_body_requires_a_query_vector(params: SearchParams) -> None:
    with pytest.raises(ValueError, match="vector"):
        search_body(UserContext("platform", 2), "질의", params)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"method": "hybrid_weighted"},
        {"method": "hybrid_weighted", "bm25_weight": 0.0},
        {"method": "bm25", "bm25_weight": 0.5},
        {"method": "bm25", "candidate_chunks": 0},
        {"method": "hybrid_rrf", "rank_constant": 30},
    ],
)
def test_search_params_reject_invalid_or_unregistered_settings(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        SearchParams(**kwargs)  # type: ignore[arg-type]


def test_authorized_search_embeds_queries_selects_pipeline_and_groups_docs() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "hits": {
                    "hits": [
                        {
                            "_score": 0.5,
                            "_source": {
                                "chunk_id": "chunk-z",
                                "doc_id": "doc-b",
                                "start": 10,
                                "end": 20,
                                "section_key": None,
                                "section_kind": "heading_section",
                            },
                        },
                        {
                            "_score": 0.8,
                            "_source": {
                                "chunk_id": "chunk-b",
                                "doc_id": "doc-b",
                                "start": 0,
                                "end": 10,
                                "section_key": "제1조",
                                "section_kind": "article",
                            },
                        },
                        {
                            "_score": 0.8,
                            "_source": {
                                "chunk_id": "chunk-a",
                                "doc_id": "doc-a",
                                "start": 2,
                                "end": 8,
                                "section_key": None,
                                "section_kind": None,
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
    embedder = RecordingEmbedder()
    params = SearchParams(
        "hybrid_weighted",
        candidate_chunks=3,
        bm25_weight=0.4,
    )

    result = authorized_search(
        client,
        "kodocs-m5-composite-structural",
        UserContext("platform", 2),
        "배포 승인",
        params,
        embedder,
    )

    assert embedder.calls == [(("배포 승인",), "query")]
    assert result.embed_ms is not None and result.embed_ms >= 0.0
    assert result.search_ms >= 0.0
    assert [hit.chunk_id for hit in result.chunks] == [
        "chunk-a",
        "chunk-b",
        "chunk-z",
    ]
    assert [(hit.doc_id, hit.score, hit.best_chunk_id) for hit in result.docs] == [
        ("doc-a", 0.8, "chunk-a"),
        ("doc-b", 0.8, "chunk-b"),
    ]
    assert len(captured) == 1
    assert captured[0].method == "POST"
    assert captured[0].url.path == "/kodocs-m5-composite-structural/_search"
    assert captured[0].url.params["search_pipeline"] == weighted_pipeline_name(0.4)
    assert_every_subquery_prefiltered(json.loads(captured[0].content))


def test_prefer_current_search_reorders_chunks_and_docs_without_changing_request() -> (
    None
):
    captured: list[dict[str, object]] = []
    raw_hits = [
        {
            "_score": 3.0,
            "_source": {
                "chunk_id": "prior#1",
                "doc_id": "prior",
                "start": 0,
                "end": 10,
                "text_nori": "개정 전 본문",
                "title": "출장규칙",
                "is_current": False,
                "rule_id": "rule-a",
                "date": "2025-01-01",
            },
        },
        {
            "_score": 2.0,
            "_source": {
                "chunk_id": "other#1",
                "doc_id": "other",
                "start": 0,
                "end": 10,
                "text_nori": "다른 문서",
                "title": "다른 규정",
                "is_current": True,
                "rule_id": "rule-b",
                "date": "2026-01-01",
            },
        },
        {
            "_score": 1.0,
            "_source": {
                "chunk_id": "current#1",
                "doc_id": "current",
                "start": 0,
                "end": 10,
                "text_nori": "현행 본문",
                "title": "출장규칙",
                "is_current": True,
                "rule_id": "rule-a",
                "date": "2026-01-01",
            },
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"hits": {"hits": raw_hits}})

    client = OpenSearchClient(
        client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    ctx = UserContext("platform", 2)
    params = SearchParams("bm25", candidate_chunks=3)

    baseline = authorized_search(client, "index", ctx, "출장", params)
    preferred = authorized_search(
        client,
        "index",
        ctx,
        "출장",
        params,
        prefer_current=True,
    )

    assert [hit.chunk_id for hit in baseline.chunks] == [
        "prior#1",
        "other#1",
        "current#1",
    ]
    assert [hit.chunk_id for hit in preferred.chunks] == [
        "current#1",
        "other#1",
        "prior#1",
    ]
    assert [hit.doc_id for hit in preferred.docs] == ["current", "other", "prior"]
    assert preferred.chunks[0].text == "현행 본문"
    assert preferred.chunks[0].title == "출장규칙"
    assert preferred.chunks[0].is_current is True
    assert preferred.chunks[0].rule_id == "rule-a"
    assert preferred.docs[0].title == "출장규칙"
    assert preferred.docs[0].is_current is True
    assert preferred.docs[0].rule_id == "rule-a"
    assert captured[0] == captured[1]
    assert captured[0]["query"]["bool"]["filter"] == access_filter(ctx)  # type: ignore[index]
    assert captured[0]["_source"] == [
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
    ]


def test_bm25_skips_embedding_and_pipeline_and_empty_results_are_neutral() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"hits": {"hits": []}})

    client = OpenSearchClient(
        "http://opensearch.test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    embedder = RecordingEmbedder()

    result = authorized_search(
        client,
        "kodocs-m5-composite-fixed",
        UserContext("finance", 1),
        "존재 여부를 드러내지 않는 질의",
        SearchParams("bm25"),
        embedder,
    )

    assert embedder.calls == []
    assert result.chunks == ()
    assert result.docs == ()
    assert result.embed_ms is None
    assert captured[0].url.query == b""
    assert "권한" not in repr(result)


def test_rrf_uses_the_fixed_registered_pipeline() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"hits": {"hits": []}})

    client = OpenSearchClient(
        client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    authorized_search(
        client,
        "index",
        UserContext("platform", 2),
        "질의",
        SearchParams("hybrid_rrf"),
        RecordingEmbedder(),
    )

    assert captured[0].url.params["search_pipeline"] == RRF_PIPELINE_NAME


@pytest.mark.parametrize(
    "method",
    ["knn", "hybrid_rrf", "hybrid_weighted"],
)
def test_vector_methods_require_an_embedder(method: str) -> None:
    client = OpenSearchClient(
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"hits": {"hits": []}})
            )
        )
    )
    params = SearchParams(
        method,  # type: ignore[arg-type]
        bm25_weight=0.5 if method == "hybrid_weighted" else None,
    )

    with pytest.raises(ValueError, match="embedder"):
        authorized_search(
            client,
            "index",
            UserContext("platform", 2),
            "질의",
            params,
        )
