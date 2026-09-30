"""M5 index and search-pipeline checks against local OpenSearch 3.8."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from kodocs.chunking.corpus import Access, CorpusDoc, Section
from kodocs.retrieval import index_build
from kodocs.retrieval.access import UserContext
from kodocs.retrieval.embedding import FakeEmbedder
from kodocs.retrieval.index_build import IndexSpec, build_index
from kodocs.retrieval.index_schema import (
    RRF_PIPELINE_NAME,
    ensure_pipelines,
    index_body,
    weighted_pipeline_name,
)
from kodocs.retrieval.opensearch import OpenSearchClient
from kodocs.retrieval.search import SearchParams, authorized_search

OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "http://127.0.0.1:9200")


@pytest.fixture
def m5_index() -> Iterator[tuple[OpenSearchClient, str]]:
    http_client = httpx.Client(timeout=10.0)
    client = OpenSearchClient(OPENSEARCH_URL, client=http_client)
    name = f"kodocs-m5-integration-{uuid.uuid4().hex}"
    body = index_body(dim=2)
    body["mappings"]["_meta"] = {
        "condition": "integration",
        "chunking": "structural",
        "embedder_settings_hash": "integration-fixture",
    }
    index_created = False

    try:
        created = client.request("PUT", f"/{name}", json=body)
        index_created = True
        assert created["acknowledged"] is True
        ensure_pipelines(client)
        result = client.bulk(
            name,
            [
                (
                    "doc-pay-1234",
                    {
                        "chunk_id": "doc-pay-1234#structural-0001",
                        "doc_id": "doc-pay-1234",
                        "chunking": "structural",
                        "text_std": "PAY-1234 지급 승인",
                        "text_nori": "PAY-1234 지급 승인",
                        "vector": [1.0, 0.0],
                        "ids_ticket": ["PAY-1234"],
                    },
                ),
                (
                    "doc-pay-1233",
                    {
                        "chunk_id": "doc-pay-1233#structural-0001",
                        "doc_id": "doc-pay-1233",
                        "chunking": "structural",
                        "text_std": "PAY-1233 지급 승인",
                        "text_nori": "PAY-1233 지급 승인",
                        "vector": [0.9, 0.1],
                        "ids_ticket": ["PAY-1233"],
                    },
                ),
            ],
        )
        assert result.error_count == 0
        refreshed = client.request("POST", f"/{name}/_refresh")
        assert refreshed["_shards"]["failed"] == 0
        yield client, name
    except httpx.TransportError as exc:
        pytest.fail(
            f"OpenSearch is unavailable at {OPENSEARCH_URL}. "
            f"Run `make up` and wait for startup before retrying: {exc}"
        )
    finally:
        try:
            if index_created:
                deleted = client.request("DELETE", f"/{name}")
                assert deleted["acknowledged"] is True
        finally:
            http_client.close()


@pytest.fixture
def authorized_m5_index() -> Iterator[tuple[OpenSearchClient, str, FakeEmbedder]]:
    """Index more forbidden nearest neighbors than k plus one public fallback."""
    http_client = httpx.Client(timeout=10.0)
    client = OpenSearchClient(OPENSEARCH_URL, client=http_client)
    name = f"kodocs-m5-authorized-{uuid.uuid4().hex}"
    embedder = FakeEmbedder(dim=8)
    query = "긴급 배포 승인"
    query_vector = embedder.embed([query], kind="query")[0]
    index_created = False
    try:
        body = index_body(dim=embedder.dim)
        body["mappings"]["_meta"] = {
            "condition": "integration",
            "chunking": "structural",
            "embedder_settings_hash": "authorized-fixture",
        }
        created = client.request("PUT", f"/{name}", json=body)
        index_created = True
        assert created["acknowledged"] is True
        ensure_pipelines(client)
        documents = [
            (
                "public-chunk",
                {
                    "chunk_id": "public-chunk",
                    "doc_id": "public-doc",
                    "text_std": query,
                    "text_nori": query,
                    "vector": [-value for value in query_vector],
                    "access_level": "public",
                    "access_teams": [],
                    "access_min_grade": 0,
                    "start": 0,
                    "end": len(query),
                },
            ),
            *[
                (
                    f"restricted-chunk-{number}",
                    {
                        "chunk_id": f"restricted-chunk-{number}",
                        "doc_id": f"restricted-doc-{number}",
                        "text_std": f"{query} {query} 기밀 {number}",
                        "text_nori": f"{query} {query} 기밀 {number}",
                        "vector": query_vector,
                        "access_level": "restricted",
                        "access_teams": ["platform"],
                        "access_min_grade": 3,
                        "start": 0,
                        "end": len(query),
                    },
                )
                for number in range(4)
            ],
        ]
        result = client.bulk(name, documents)
        assert result.error_count == 0
        refreshed = client.request("POST", f"/{name}/_refresh")
        assert refreshed["_shards"]["failed"] == 0
        yield client, name, embedder
    except httpx.TransportError as exc:
        pytest.fail(
            f"OpenSearch is unavailable at {OPENSEARCH_URL}. "
            f"Run `make up` and wait for startup before retrying: {exc}"
        )
    finally:
        try:
            if index_created:
                deleted = client.request("DELETE", f"/{name}")
                assert deleted["acknowledged"] is True
        finally:
            http_client.close()


def _search(
    client: OpenSearchClient,
    index: str,
    query: dict,
    *,
    pipeline: str | None = None,
) -> list[dict]:
    suffix = f"?search_pipeline={pipeline}" if pipeline else ""
    response = client.request(
        "POST",
        f"/{index}/_search{suffix}",
        json={"size": 10, "query": query},
    )
    assert response["_shards"]["failed"] == 0
    hits = response["hits"]["hits"]
    assert isinstance(hits, list)
    return hits


@pytest.mark.integration
def test_identifier_term_query_avoids_analyzed_field_false_positive(
    m5_index: tuple[OpenSearchClient, str],
) -> None:
    client, name = m5_index

    for field in ("text_std", "text_nori"):
        analyzed = _search(client, name, {"match": {field: "PAY-1234"}})
        assert {hit["_id"] for hit in analyzed} == {
            "doc-pay-1234",
            "doc-pay-1233",
        }

    exact = _search(client, name, {"term": {"ids_ticket": "PAY-1234"}})
    assert [hit["_id"] for hit in exact] == ["doc-pay-1234"]


@pytest.mark.integration
@pytest.mark.parametrize(
    "pipeline",
    [RRF_PIPELINE_NAME, weighted_pipeline_name(0.5)],
)
def test_hybrid_query_responds_through_registered_pipeline(
    m5_index: tuple[OpenSearchClient, str], pipeline: str
) -> None:
    client, name = m5_index
    hits = _search(
        client,
        name,
        {
            "hybrid": {
                "queries": [
                    {"match": {"text_nori": "지급 승인"}},
                    {"knn": {"vector": {"vector": [1.0, 0.0], "k": 2}}},
                ]
            }
        },
        pipeline=pipeline,
    )

    assert hits
    assert hits[0]["_id"] == "doc-pay-1234"


@pytest.mark.integration
def test_index_builder_indexes_small_fixture_and_count_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = CorpusDoc(
        doc_id="syn-integration",
        source="synthetic",
        doc_type="runbook",
        title="통합 테스트",
        text="PAY-1234 복구 절차",
        sections=(Section("heading_section", None, ("복구",), 0, 14),),
        access=Access("public", (), 0),
        team="platform",
        date="2026-09-30",
        rule_id=None,
        is_current=True,
        superseded_by=None,
        identifiers={
            "ticket": ("PAY-1234",),
            "approval": (),
            "incident": (),
            "service": (),
            "regulation": (),
        },
    )
    monkeypatch.setattr(
        index_build,
        "load_corpus",
        lambda condition, *, root: (
            [document],
            SimpleNamespace(composite_annex_fallback_count=0),
        ),
    )
    manifest = tmp_path / "data/manifest/m5_index.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        '{"chunking":{"fixed_size":200,"fixed_overlap":40}}',
        encoding="utf-8",
    )
    http_client = httpx.Client(timeout=10.0)
    client = OpenSearchClient(OPENSEARCH_URL, client=http_client)
    index = "kodocs-m5-composite-structural"
    try:
        report = build_index(
            IndexSpec("composite", "structural"),
            client=client,
            embedder=FakeEmbedder(dim=8),
            root=tmp_path,
            answer_ranges_by_set={"gold": {}, "silver": {}},
        )
        assert report.success is True
        counted = client.request("GET", f"/{index}/_count")
        assert counted["count"] == report.chunk_count == 1
    except httpx.TransportError as exc:
        pytest.fail(
            f"OpenSearch is unavailable at {OPENSEARCH_URL}. "
            f"Run `make up` and wait for startup before retrying: {exc}"
        )
    finally:
        try:
            client.request("DELETE", f"/{index}")
        except httpx.TransportError:
            pass
        finally:
            http_client.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    "params",
    [
        SearchParams("bm25", candidate_chunks=5),
        SearchParams("knn", candidate_chunks=5),
        SearchParams("hybrid_rrf", candidate_chunks=5),
        SearchParams(
            "hybrid_weighted",
            candidate_chunks=5,
            bm25_weight=0.5,
        ),
    ],
)
def test_authorized_search_never_returns_restricted_docs_to_unauthorized_users(
    authorized_m5_index: tuple[OpenSearchClient, str, FakeEmbedder],
    params: SearchParams,
) -> None:
    client, name, embedder = authorized_m5_index

    for ctx in (UserContext("finance", 9), UserContext("platform", 2)):
        result = authorized_search(
            client,
            name,
            ctx,
            "긴급 배포 승인",
            params,
            embedder,
        )
        assert {hit.doc_id for hit in result.chunks} == {"public-doc"}
        assert {hit.doc_id for hit in result.docs} == {"public-doc"}

    authorized = authorized_search(
        client,
        name,
        UserContext("platform", 3),
        "긴급 배포 승인",
        params,
        embedder,
    )
    assert any(hit.doc_id.startswith("restricted-doc-") for hit in authorized.chunks)
    assert any(hit.doc_id.startswith("restricted-doc-") for hit in authorized.docs)


@pytest.mark.integration
def test_knn_filter_is_applied_before_top_k_candidate_selection(
    authorized_m5_index: tuple[OpenSearchClient, str, FakeEmbedder],
) -> None:
    client, name, embedder = authorized_m5_index

    result = authorized_search(
        client,
        name,
        UserContext("finance", 9),
        "긴급 배포 승인",
        SearchParams("knn", candidate_chunks=1),
        embedder,
    )

    assert [hit.doc_id for hit in result.chunks] == ["public-doc"]
    assert [hit.doc_id for hit in result.docs] == ["public-doc"]
