"""Dependencies and frozen M5 configuration for shared tool execution."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from kodocs.retrieval.search import SearchParams

if TYPE_CHECKING:
    from kodocs.retrieval.embedding import Embedder
    from kodocs.retrieval.opensearch import OpenSearchClient

TOOL_INDEX = "kodocs-m5-composite-structural"
M5_CONFIG_PATH = Path("data/manifest/m5_config.json")
M5_CONFIG_HASH = "455f063edd2654326d62607d78bc6f0079a25f30d77b29c274ca345ba9ed4a7d"

SEARCH_CHUNK_LIMIT = 8
SEARCH_TEXT_LIMIT = 6_000
LOOKUP_DOCUMENT_LIMIT = 5
LOOKUP_DOCUMENT_TEXT_LIMIT = 1_200
TOOL_SERIALIZED_LIMIT = 6_000
DOCUMENT_TEXT_LIMIT = 6_000
DOCUMENT_CHUNK_LIMIT = 200


@dataclass(frozen=True)
class ToolDeps:
    """Runtime services injected independently from model-controlled arguments."""

    client: OpenSearchClient
    index: str
    embedder: Embedder
    db_path: Path
    search_params: SearchParams
    prefer_current: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.index, str) or not self.index:
            raise ValueError("index must be a non-empty string")
        if not isinstance(self.db_path, Path):
            raise TypeError("db_path must be a Path")
        if not isinstance(self.prefer_current, bool):
            raise TypeError("prefer_current must be a bool")


def load_tool_config(
    path: Path = M5_CONFIG_PATH,
) -> tuple[str, SearchParams]:
    """Load the frozen M5 weighted-hybrid settings used by every M6 tool."""
    if not isinstance(path, Path):
        raise TypeError("path must be a Path")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise TypeError("M5 config must be a JSON object")
    if payload.get("config_hash") != M5_CONFIG_HASH:
        raise ValueError("M5 config hash does not match the frozen M6 configuration")
    indexes = payload.get("indexes")
    if not isinstance(indexes, Mapping) or TOOL_INDEX not in indexes:
        raise ValueError("M5 config does not contain the structural composite index")
    if payload.get("hybrid_method") != "hybrid_weighted":
        raise ValueError("M6 requires the frozen hybrid_weighted method")

    candidate_chunks = _positive_int(
        payload.get("candidate_chunks"),
        label="candidate_chunks",
    )
    bm25_weight = _weight(payload.get("bm25_weight"), label="bm25_weight")
    rank_constant = _positive_int(payload.get("rank_constant"), label="rank_constant")
    return TOOL_INDEX, SearchParams(
        method="hybrid_weighted",
        analyzer="nori",
        candidate_chunks=candidate_chunks,
        bm25_weight=bm25_weight,
        rank_constant=rank_constant,
    )


def build_tool_deps(
    *,
    root: Path = Path("."),
    base_url: str = "http://127.0.0.1:9200",
    model_path_or_id: str | None = None,
) -> ToolDeps:
    """Create and prepare the real local client and BGE-M3 embedder lazily."""
    from kodocs.retrieval.embedding import Bge3Embedder, CachedEmbedder
    from kodocs.retrieval.opensearch import OpenSearchClient

    config_path = root / M5_CONFIG_PATH
    index, search_params = load_tool_config(config_path)
    inner = (
        Bge3Embedder()
        if model_path_or_id is None
        else Bge3Embedder(model_path_or_id=model_path_or_id)
    )
    embedder = CachedEmbedder(
        inner,
        root / "data" / "interim" / "m5" / "embedding_cache",
    )
    embedder.prepare()
    return ToolDeps(
        client=OpenSearchClient(base_url),
        index=index,
        embedder=embedder,
        db_path=root / "data" / "synthetic" / "ledger.sqlite",
        search_params=search_params,
        prefer_current=True,
    )


def _positive_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"M5 config {label} must be a positive integer")
    return value


def _weight(value: object, *, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not 0.0 <= float(value) <= 1.0
    ):
        raise ValueError(f"M5 config {label} must be between zero and one")
    return float(value)
