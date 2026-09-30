"""M5 index and search-pipeline checks against local OpenSearch 3.8."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import httpx
import pytest

from kodocs.retrieval.index_schema import (
    RRF_PIPELINE_NAME,
    ensure_pipelines,
    index_body,
    weighted_pipeline_name,
)
from kodocs.retrieval.opensearch import OpenSearchClient

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
