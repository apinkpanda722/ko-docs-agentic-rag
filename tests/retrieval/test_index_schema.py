"""Unit tests for the M5 OpenSearch schema and thin HTTP client."""

from __future__ import annotations

import json

import httpx
import pytest

from kodocs.retrieval.index_schema import (
    INDEX_PREFIX,
    RRF_PIPELINE_NAME,
    WEIGHTED_BM25_WEIGHTS,
    ensure_pipelines,
    index_body,
    index_name,
    rrf_pipeline_body,
    weighted_pipeline_body,
    weighted_pipeline_name,
)
from kodocs.retrieval.opensearch import OpenSearchClient, OpenSearchError


def test_index_body_defines_analysis_vectors_metadata_and_fixed_fields() -> None:
    body = index_body()

    settings = body["settings"]
    assert settings["index"] == {
        "knn": True,
        "number_of_replicas": 0,
        "number_of_shards": 1,
    }
    analysis = settings["analysis"]
    assert analysis["tokenizer"]["nori_tokenizer"] == {
        "type": "nori_tokenizer",
        "decompound_mode": "mixed",
    }
    assert analysis["filter"]["nori_part_of_speech"] == {"type": "nori_part_of_speech"}
    assert analysis["analyzer"]["nori_analyzer"]["filter"] == [
        "nori_part_of_speech",
        "nori_readingform",
        "lowercase",
    ]

    mappings = body["mappings"]
    assert mappings["_meta"] == {
        "condition": None,
        "chunking": None,
        "embedder_settings_hash": None,
    }
    properties = mappings["properties"]
    assert properties["text_std"] == {"type": "text", "analyzer": "standard"}
    assert properties["text_nori"] == {
        "type": "text",
        "analyzer": "nori_analyzer",
    }
    assert properties["vector"] == {
        "type": "knn_vector",
        "dimension": 1024,
        "method": {
            "name": "hnsw",
            "engine": "lucene",
            "space_type": "cosinesimil",
        },
    }

    keyword_fields = {
        "chunk_id",
        "doc_id",
        "chunking",
        "doc_type",
        "source",
        "team",
        "rule_id",
        "section_key",
        "section_kind",
        "ids_ticket",
        "ids_approval",
        "ids_incident",
        "ids_service",
        "ids_regulation",
        "access_level",
        "access_teams",
        "date",
    }
    assert all(properties[field] == {"type": "keyword"} for field in keyword_fields)
    assert properties["access_min_grade"] == {"type": "integer"}
    assert properties["is_current"] == {"type": "boolean"}
    assert properties["start"] == {"type": "integer", "index": False}
    assert properties["end"] == {"type": "integer", "index": False}
    assert properties["title"] == {"type": "keyword", "index": False}
    assert set(properties) == keyword_fields | {
        "text_std",
        "text_nori",
        "vector",
        "access_min_grade",
        "is_current",
        "start",
        "end",
        "title",
    }


def test_index_name_is_deterministic_and_rejects_invalid_components() -> None:
    assert index_name("composite", "structural") == (
        f"{INDEX_PREFIX}-composite-structural"
    )

    with pytest.raises(ValueError, match="condition"):
        index_name("Composite", "structural")
    with pytest.raises(ValueError, match="chunking"):
        index_name("composite", "fixed/other")


def test_pipeline_bodies_match_registered_processors() -> None:
    rrf = rrf_pipeline_body()
    rrf_processor = rrf["phase_results_processors"][0]["score-ranker-processor"]
    assert rrf_processor["combination"] == {
        "technique": "rrf",
        "rank_constant": 60,
    }

    weighted = weighted_pipeline_body(0.3)
    processor = weighted["phase_results_processors"][0]["normalization-processor"]
    assert processor["normalization"] == {"technique": "min_max"}
    assert processor["combination"]["technique"] == "arithmetic_mean"
    weights = processor["combination"]["parameters"]["weights"]
    assert weights == pytest.approx([0.3, 0.7])
    assert sum(weights) == pytest.approx(1.0)
    assert weighted_pipeline_name(0.3) == "kodocs-m5-weighted-w030"


def test_index_and_all_tuning_pipelines_are_sent_through_mock_transport() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"acknowledged": True})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = OpenSearchClient("http://opensearch.test", client=http_client)

    created = client.request(
        "PUT",
        f"/{index_name('composite', 'structural')}",
        json=index_body(dim=3),
    )
    ensure_pipelines(client)

    assert created == {"acknowledged": True}
    assert [request.method for request in requests] == ["PUT"] * 7
    assert requests[0].url.path == "/kodocs-m5-composite-structural"
    assert (
        json.loads(requests[0].content)["mappings"]["properties"]["vector"]["dimension"]
        == 3
    )
    expected_pipeline_names = [
        RRF_PIPELINE_NAME,
        *(weighted_pipeline_name(weight) for weight in WEIGHTED_BM25_WEIGHTS),
    ]
    assert [request.url.path for request in requests[1:]] == [
        f"/_search/pipeline/{name}" for name in expected_pipeline_names
    ]
    pipeline_payloads = [json.loads(request.content) for request in requests[1:]]
    assert pipeline_payloads[0] == rrf_pipeline_body()
    assert pipeline_payloads[1:] == [
        weighted_pipeline_body(weight) for weight in WEIGHTED_BM25_WEIGHTS
    ]


def test_request_raises_bounded_opensearch_error_for_http_failure() -> None:
    secret_tail = "x" * 300

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=f"invalid request: {secret_tail}")

    client = OpenSearchClient(
        client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(OpenSearchError) as caught:
        client.request("GET", "/broken")

    assert caught.value.status == 400
    assert len(caught.value.body) == 200
    assert secret_tail not in str(caught.value)


def test_bulk_writes_ndjson_with_terminal_newline_and_collects_item_errors() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "took": 7,
                "errors": True,
                "items": [
                    {"index": {"_id": "ok", "status": 201}},
                    {
                        "index": {
                            "_id": "bad",
                            "status": 400,
                            "error": {
                                "type": "mapper_parsing_exception",
                                "reason": "bad vector",
                            },
                        }
                    },
                ],
            },
        )

    client = OpenSearchClient(
        "http://opensearch.test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = client.bulk(
        "kodocs-m5-composite-fixed",
        [("ok", {"text_std": "첫 문서"}), ("bad", {"text_std": "둘째 문서"})],
    )

    assert len(captured) == 1
    assert captured[0].url.path == "/_bulk"
    assert captured[0].headers["content-type"] == "application/x-ndjson"
    expected_lines = [
        {
            "index": {
                "_index": "kodocs-m5-composite-fixed",
                "_id": "ok",
            }
        },
        {"text_std": "첫 문서"},
        {
            "index": {
                "_index": "kodocs-m5-composite-fixed",
                "_id": "bad",
            }
        },
        {"text_std": "둘째 문서"},
    ]
    expected = "".join(
        json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n"
        for line in expected_lines
    ).encode()
    assert captured[0].content == expected
    assert result.took_ms == 7
    assert result.item_count == 2
    assert result.succeeded_count == 1
    assert result.error_count == 1
    assert result.errors[0].doc_id == "bad"
    assert result.errors[0].status == 400
    assert result.errors[0].error["type"] == "mapper_parsing_exception"
