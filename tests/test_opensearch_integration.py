"""Integration checks for a running local OpenSearch container."""

import json
import os
import urllib.error
import urllib.request

import pytest

OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "http://127.0.0.1:9200").rstrip("/")


def _request_json(path: str, payload: dict | None = None) -> object:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{OPENSEARCH_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError) as error:
        pytest.fail(
            f"OpenSearch is unavailable at {OPENSEARCH_URL}. "
            f"Run `make up` and wait for startup before retrying: {error}"
        )


@pytest.mark.integration
def test_cluster_health_is_acceptable_for_a_single_node() -> None:
    health = _request_json("/_cluster/health")

    assert isinstance(health, dict), "cluster health response must be a JSON object"
    assert health.get("status") in {"green", "yellow"}, (
        "single-node cluster health must be green or yellow"
    )


@pytest.mark.integration
def test_analysis_nori_plugin_is_installed() -> None:
    plugins = _request_json("/_cat/plugins?format=json")

    assert isinstance(plugins, list), "_cat/plugins response must be a JSON list"
    assert any(plugin.get("component") == "analysis-nori" for plugin in plugins), (
        "analysis-nori must be present in _cat/plugins"
    )


@pytest.mark.integration
def test_nori_tokenizer_splits_korean_into_morphemes() -> None:
    analysis = _request_json(
        "/_analyze",
        {
            "tokenizer": "nori_tokenizer",
            "text": "여비규정 제8조의 출장비 지급 기준",
        },
    )

    assert isinstance(analysis, dict), "_analyze response must be a JSON object"
    tokens = [token["token"] for token in analysis.get("tokens", [])]
    assert len(tokens) > 2, "nori_tokenizer must produce more than two morphemes"
    assert "여비" in tokens, "nori_tokenizer must extract the morpheme '여비'"
