"""Integration checks for authorized document retrieval on OpenSearch 3.x."""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from kodocs.retrieval.access import UserContext
from kodocs.retrieval.document import authorized_get_document
from kodocs.retrieval.index_schema import index_body
from kodocs.retrieval.opensearch import OpenSearchClient

OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "http://127.0.0.1:9200")


@pytest.mark.integration
def test_get_document_sorts_nonindexed_start_and_prefilters_access() -> None:
    http_client = httpx.Client(timeout=10.0)
    client = OpenSearchClient(OPENSEARCH_URL, client=http_client)
    index = f"kodocs-m6-get-document-{uuid.uuid4().hex}"
    index_created = False
    try:
        body = index_body(dim=2)
        body["mappings"]["_meta"] = {
            "condition": "integration",
            "chunking": "structural",
            "embedder_settings_hash": "get-document-fixture",
        }
        created = client.request("PUT", f"/{index}", json=body)
        index_created = True
        assert created["acknowledged"] is True
        result = client.bulk(
            index,
            [
                (
                    "visible-z",
                    {
                        "chunk_id": "visible-z",
                        "doc_id": "visible-doc",
                        "title": "운영 지침",
                        "text_std": "셋째",
                        "text_nori": "셋째",
                        "vector": [1.0, 0.0],
                        "section_key": "section-b",
                        "section_kind": "heading_section",
                        "start": 10,
                        "end": 13,
                        "access_level": "public",
                        "access_teams": [],
                        "access_min_grade": 0,
                    },
                ),
                (
                    "visible-b",
                    {
                        "chunk_id": "visible-b",
                        "doc_id": "visible-doc",
                        "title": "운영 지침",
                        "text_std": "둘째",
                        "text_nori": "둘째",
                        "vector": [1.0, 0.0],
                        "section_key": "section-a",
                        "section_kind": "heading_section",
                        "start": 0,
                        "end": 3,
                        "access_level": "public",
                        "access_teams": [],
                        "access_min_grade": 0,
                    },
                ),
                (
                    "visible-a",
                    {
                        "chunk_id": "visible-a",
                        "doc_id": "visible-doc",
                        "title": "운영 지침",
                        "text_std": "첫째",
                        "text_nori": "첫째",
                        "vector": [1.0, 0.0],
                        "section_key": "section-a",
                        "section_kind": "heading_section",
                        "start": 0,
                        "end": 3,
                        "access_level": "public",
                        "access_teams": [],
                        "access_min_grade": 0,
                    },
                ),
                (
                    "restricted-a",
                    {
                        "chunk_id": "restricted-a",
                        "doc_id": "restricted-doc",
                        "title": "제한 지침",
                        "text_std": "제한 본문",
                        "text_nori": "제한 본문",
                        "vector": [1.0, 0.0],
                        "section_key": "section-a",
                        "section_kind": "heading_section",
                        "start": 0,
                        "end": 5,
                        "access_level": "restricted",
                        "access_teams": ["platform"],
                        "access_min_grade": 3,
                    },
                ),
            ],
        )
        assert result.error_count == 0
        client.request("POST", f"/{index}/_refresh")

        document = authorized_get_document(
            client,
            index,
            UserContext("finance", 1),
            "visible-doc",
        )
        section = authorized_get_document(
            client,
            index,
            UserContext("finance", 1),
            "visible-doc",
            section_key="section-a",
        )

        assert document is not None
        assert [chunk.chunk_id for chunk in document.chunks] == [
            "visible-a",
            "visible-b",
            "visible-z",
        ]
        assert section is not None
        assert [chunk.chunk_id for chunk in section.chunks] == [
            "visible-a",
            "visible-b",
        ]
        assert (
            authorized_get_document(
                client,
                index,
                UserContext("finance", 1),
                "restricted-doc",
            )
            is None
        )
        assert (
            authorized_get_document(
                client,
                index,
                UserContext("finance", 1),
                "missing-doc",
            )
            is None
        )
    except httpx.TransportError as exc:
        pytest.fail(
            f"OpenSearch is unavailable at {OPENSEARCH_URL}. "
            f"Run `make up` and wait for startup before retrying: {exc}"
        )
    finally:
        try:
            if index_created:
                deleted = client.request("DELETE", f"/{index}")
                assert deleted["acknowledged"] is True
        finally:
            http_client.close()
