"""OpenSearch index mapping and hybrid search pipelines for M5."""

from __future__ import annotations

import math
import re

from kodocs.retrieval.opensearch import OpenSearchClient

INDEX_PREFIX = "kodocs-m5"
RRF_PIPELINE_NAME = f"{INDEX_PREFIX}-rrf"
WEIGHTED_BM25_WEIGHTS = (0.3, 0.4, 0.5, 0.6, 0.7)

_INDEX_COMPONENT_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_KEYWORD_FIELDS = (
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
)


def index_name(condition: str, chunking: str) -> str:
    """Return the deterministic index name for one parser/corpus condition."""
    _validate_index_component(condition, label="condition")
    _validate_index_component(chunking, label="chunking")
    return f"{INDEX_PREFIX}-{condition}-{chunking}"


def index_body(*, dim: int = 1024) -> dict:
    """Build the shared M5 mapping; index builders fill the `_meta` slots."""
    if not isinstance(dim, int) or isinstance(dim, bool) or dim <= 0:
        raise ValueError("dim must be a positive integer")

    properties = {field: {"type": "keyword"} for field in _KEYWORD_FIELDS}
    properties.update(
        {
            "text_std": {"type": "text", "analyzer": "standard"},
            "text_nori": {"type": "text", "analyzer": "nori_analyzer"},
            "vector": {
                "type": "knn_vector",
                "dimension": dim,
                "method": {
                    "name": "hnsw",
                    # Lucene HNSW supports efficient filtering for authorization filters.
                    "engine": "lucene",
                    "space_type": "cosinesimil",
                },
            },
            "access_min_grade": {"type": "integer"},
            "is_current": {"type": "boolean"},
            "start": {"type": "integer", "index": False},
            "end": {"type": "integer", "index": False},
            "title": {"type": "keyword", "index": False},
        }
    )
    return {
        "settings": {
            "index": {
                "knn": True,
                "number_of_shards": 1,
                "number_of_replicas": 0,
            },
            "analysis": {
                "tokenizer": {
                    "nori_tokenizer": {
                        "type": "nori_tokenizer",
                        "decompound_mode": "mixed",
                    }
                },
                "filter": {
                    # Omitting stoptags deliberately selects the plugin defaults.
                    "nori_part_of_speech": {"type": "nori_part_of_speech"}
                },
                "analyzer": {
                    "nori_analyzer": {
                        "type": "custom",
                        "tokenizer": "nori_tokenizer",
                        "filter": [
                            "nori_part_of_speech",
                            "nori_readingform",
                            "lowercase",
                        ],
                    }
                },
            },
        },
        "mappings": {
            "_meta": {
                "condition": None,
                "chunking": None,
                "embedder_settings_hash": None,
            },
            "properties": properties,
        },
    }


def rrf_pipeline_body(rank_constant: int = 60) -> dict:
    """Build a score-ranker search pipeline using reciprocal-rank fusion."""
    if (
        not isinstance(rank_constant, int)
        or isinstance(rank_constant, bool)
        or rank_constant <= 0
    ):
        raise ValueError("rank_constant must be a positive integer")
    return {
        "phase_results_processors": [
            {
                "score-ranker-processor": {
                    "combination": {
                        "technique": "rrf",
                        "rank_constant": rank_constant,
                    }
                }
            }
        ]
    }


def weighted_pipeline_body(bm25_weight: float) -> dict:
    """Build min-max normalization and BM25-first arithmetic combination."""
    weight = _validate_weight(bm25_weight)
    return {
        "phase_results_processors": [
            {
                "normalization-processor": {
                    "normalization": {"technique": "min_max"},
                    "combination": {
                        "technique": "arithmetic_mean",
                        "parameters": {"weights": [weight, 1.0 - weight]},
                    },
                }
            }
        ]
    }


def weighted_pipeline_name(bm25_weight: float) -> str:
    """Return the deterministic pipeline name for a BM25 weight."""
    weight = _validate_weight(bm25_weight)
    return f"{INDEX_PREFIX}-weighted-w{round(weight * 100):03d}"


def ensure_pipelines(client: OpenSearchClient) -> None:
    """Idempotently create the RRF pipeline and the registered tuning grid."""
    client.request(
        "PUT",
        f"/_search/pipeline/{RRF_PIPELINE_NAME}",
        json=rrf_pipeline_body(),
    )
    for weight in WEIGHTED_BM25_WEIGHTS:
        client.request(
            "PUT",
            f"/_search/pipeline/{weighted_pipeline_name(weight)}",
            json=weighted_pipeline_body(weight),
        )


def _validate_index_component(value: str, *, label: str) -> None:
    if not isinstance(value, str) or _INDEX_COMPONENT_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must contain lowercase letters, digits, or hyphens")


def _validate_weight(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("bm25_weight must be a finite number between 0 and 1")
    weight = float(value)
    if not math.isfinite(weight) or not 0.0 < weight < 1.0:
        raise ValueError("bm25_weight must be a finite number between 0 and 1")
    return weight
