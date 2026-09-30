"""Tests for authorized whole-document chunk retrieval."""

from __future__ import annotations

import json

import httpx
import pytest

from kodocs.retrieval.access import UserContext, access_filter
from kodocs.retrieval.document import (
    DocumentChunk,
    DocumentResult,
    authorized_get_document,
)
from kodocs.retrieval.opensearch import OpenSearchClient


def test_get_document_prefilters_and_returns_chunks_in_opensearch_order() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "hits": {
                    "hits": [
                        {
                            "_source": {
                                "chunk_id": "doc-a#structural-0001",
                                "doc_id": "doc-a",
                                "title": "배포 운영 지침",
                                "section_key": "section-1",
                                "section_kind": "heading_section",
                                "start": 0,
                                "end": 8,
                                "text_nori": "첫 번째 청크",
                            }
                        },
                        {
                            "_source": {
                                "chunk_id": "doc-a#structural-0002",
                                "doc_id": "doc-a",
                                "title": "배포 운영 지침",
                                "section_key": None,
                                "section_kind": None,
                                "start": 8,
                                "end": 16,
                                "text_nori": "두 번째 청크",
                            }
                        },
                    ]
                }
            },
        )

    client = OpenSearchClient(
        "http://opensearch.test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    ctx = UserContext("platform", 2)

    result = authorized_get_document(client, "index", ctx, "doc-a", size=12)

    assert result == DocumentResult(
        doc_id="doc-a",
        title="배포 운영 지침",
        chunks=(
            DocumentChunk(
                chunk_id="doc-a#structural-0001",
                section_key="section-1",
                section_kind="heading_section",
                start=0,
                end=8,
                text="첫 번째 청크",
            ),
            DocumentChunk(
                chunk_id="doc-a#structural-0002",
                section_key=None,
                section_kind=None,
                start=8,
                end=16,
                text="두 번째 청크",
            ),
        ),
    )
    assert len(captured) == 1
    body = json.loads(captured[0].content)
    assert body == {
        "size": 12,
        "track_total_hits": False,
        "_source": [
            "chunk_id",
            "doc_id",
            "title",
            "section_key",
            "section_kind",
            "start",
            "end",
            "text_nori",
        ],
        "query": {
            "bool": {
                "filter": [
                    access_filter(ctx),
                    {"term": {"doc_id": "doc-a"}},
                ]
            }
        },
        "sort": [
            {"start": {"order": "asc"}},
            {"chunk_id": {"order": "asc"}},
        ],
    }


def test_get_document_adds_section_key_to_the_same_filter_list() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"hits": {"hits": []}})

    client = OpenSearchClient(
        client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    ctx = UserContext("legal", 3)

    result = authorized_get_document(
        client,
        "index",
        ctx,
        "doc-a",
        section_key="제8조",
    )

    assert result is None
    assert captured[0]["query"] == {
        "bool": {
            "filter": [
                access_filter(ctx),
                {"term": {"doc_id": "doc-a"}},
                {"term": {"section_key": "제8조"}},
            ]
        }
    }


def test_missing_and_unauthorized_documents_are_indistinguishable() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"hits": {"hits": []}})

    client = OpenSearchClient(
        client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    ctx = UserContext("finance", 1)

    missing = authorized_get_document(client, "index", ctx, "missing-doc")
    unauthorized = authorized_get_document(client, "index", ctx, "restricted-doc")

    assert missing is unauthorized is None
    assert len(captured) == 2
    for body in captured:
        filters = body["query"]["bool"]["filter"]  # type: ignore[index]
        assert filters[0] == access_filter(ctx)
        assert list(filters[1]["term"]) == ["doc_id"]
        filters[1]["term"]["doc_id"] = "normalized"
    assert captured[0] == captured[1]


@pytest.mark.parametrize(
    ("kwargs", "secret"),
    [
        ({"doc_id": ""}, ""),
        ({"doc_id": 123456789}, "123456789"),
        ({"doc_id": "doc-a", "section_key": ""}, ""),
        ({"doc_id": "doc-a", "section_key": 987654321}, "987654321"),
        ({"doc_id": "doc-a", "size": 0}, "0"),
        ({"doc_id": "doc-a", "size": True}, "True"),
    ],
)
def test_get_document_rejects_invalid_inputs_without_echoing_values(
    kwargs: dict[str, object], secret: str
) -> None:
    client = OpenSearchClient()

    with pytest.raises(ValueError) as caught:
        authorized_get_document(
            client,
            "index",
            UserContext("platform", 2),
            **kwargs,  # type: ignore[arg-type]
        )

    if secret:
        assert secret not in str(caught.value)
