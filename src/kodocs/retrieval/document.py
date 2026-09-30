"""Authorized document chunk retrieval from OpenSearch."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from kodocs.retrieval.access import UserContext, access_filter
from kodocs.retrieval.opensearch import OpenSearchClient

_SOURCE_FIELDS = (
    "chunk_id",
    "doc_id",
    "title",
    "section_key",
    "section_kind",
    "start",
    "end",
    "text_nori",
)


@dataclass(frozen=True)
class DocumentChunk:
    """One structural chunk belonging to an authorized document."""

    chunk_id: str
    section_key: str | None
    section_kind: str | None
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class DocumentResult:
    """An authorized document and its ordered chunks."""

    doc_id: str
    title: str
    chunks: tuple[DocumentChunk, ...]


def authorized_get_document(
    client: OpenSearchClient,
    index: str,
    ctx: UserContext,
    doc_id: str,
    *,
    section_key: str | None = None,
    size: int = 200,
) -> DocumentResult | None:
    """Return authorized chunks without revealing whether filtered documents exist."""
    if not isinstance(doc_id, str) or not doc_id:
        raise ValueError("doc_id must be a non-empty string")
    if section_key is not None and (
        not isinstance(section_key, str) or not section_key
    ):
        raise ValueError("section_key must be a non-empty string or None")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError("size must be a positive integer")

    filters = [
        access_filter(ctx),
        {"term": {"doc_id": doc_id}},
    ]
    if section_key is not None:
        filters.append({"term": {"section_key": section_key}})

    response = client.request(
        "POST",
        f"/{index}/_search",
        json={
            "size": size,
            "track_total_hits": False,
            "_source": list(_SOURCE_FIELDS),
            "query": {"bool": {"filter": filters}},
            "sort": [
                {"start": {"order": "asc"}},
                {"chunk_id": {"order": "asc"}},
            ],
        },
    )
    return _document_result(response)


def _document_result(response: Mapping[str, object]) -> DocumentResult | None:
    wrapper = response.get("hits")
    if not isinstance(wrapper, Mapping):
        raise TypeError("OpenSearch document response has invalid hits")
    raw_hits = wrapper.get("hits")
    if not isinstance(raw_hits, list):
        raise TypeError("OpenSearch document response has invalid hits")
    if not raw_hits:
        return None

    result_doc_id: str | None = None
    result_title: str | None = None
    chunks: list[DocumentChunk] = []
    for raw_hit in raw_hits:
        if not isinstance(raw_hit, Mapping):
            raise TypeError("OpenSearch document response has an invalid hit")
        source = raw_hit.get("_source")
        if not isinstance(source, Mapping):
            raise TypeError("OpenSearch document response has an invalid hit")

        chunk_id = _required_string(source, "chunk_id")
        doc_id = _required_string(source, "doc_id")
        title = _required_string(source, "title")
        start = _required_int(source, "start")
        end = _required_int(source, "end")
        if start < 0 or end < start:
            raise RuntimeError("OpenSearch document response has an invalid range")
        if result_doc_id is None:
            result_doc_id = doc_id
            result_title = title
        elif doc_id != result_doc_id or title != result_title:
            raise RuntimeError("OpenSearch document response mixes documents")
        chunks.append(
            DocumentChunk(
                chunk_id=chunk_id,
                section_key=_optional_string(source, "section_key"),
                section_kind=_optional_string(source, "section_kind"),
                start=start,
                end=end,
                text=_required_string(source, "text_nori"),
            )
        )

    if result_doc_id is None or result_title is None:
        raise RuntimeError("OpenSearch document response is inconsistent")
    return DocumentResult(result_doc_id, result_title, tuple(chunks))


def _required_string(source: Mapping[str, object], field: str) -> str:
    value = source.get(field)
    if not isinstance(value, str) or not value:
        raise RuntimeError("OpenSearch document response has an invalid source field")
    return value


def _optional_string(source: Mapping[str, object], field: str) -> str | None:
    value = source.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("OpenSearch document response has an invalid source field")
    return value


def _required_int(source: Mapping[str, object], field: str) -> int:
    value = source.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("OpenSearch document response has an invalid source field")
    return value
