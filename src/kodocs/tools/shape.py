"""Deterministic result shaping and context-size limits for shared tools."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from kodocs.retrieval.document import DocumentResult
from kodocs.retrieval.search import SearchResult
from kodocs.retrieval.sql import SqlResult
from kodocs.tools.deps import (
    DOCUMENT_TEXT_LIMIT,
    LOOKUP_DOCUMENT_LIMIT,
    LOOKUP_DOCUMENT_TEXT_LIMIT,
    SEARCH_CHUNK_LIMIT,
    SEARCH_TEXT_LIMIT,
    TOOL_SERIALIZED_LIMIT,
)


def shape_search_result(
    result: SearchResult,
) -> tuple[dict[str, object], tuple[dict, ...]]:
    """Keep at most eight whole chunks under the registered text budget."""
    items: list[dict[str, object]] = []
    refs: list[dict] = []
    used = 0
    truncated = False
    for hit in result.chunks:
        if len(items) >= SEARCH_CHUNK_LIMIT:
            truncated = True
            break
        text = hit.text or ""
        remaining = SEARCH_TEXT_LIMIT - used
        if len(text) > remaining:
            if not items:
                text = text[:SEARCH_TEXT_LIMIT]
            else:
                truncated = True
                break
            truncated = True
        items.append(
            {
                "doc_id": hit.doc_id,
                "title": hit.title,
                "section_key": hit.section_key,
                "section_kind": hit.section_kind,
                "is_current": hit.is_current,
                "text": text,
            }
        )
        refs.append(_doc_ref(hit.doc_id, hit.section_key))
        used += len(text)
        if truncated:
            break
    if len(items) < len(result.chunks):
        truncated = True
    return {"results": items, "truncated": truncated}, tuple(refs)


def shape_lookup_documents(
    documents: Sequence[Mapping[str, object]],
    *,
    source_truncated: bool = False,
) -> tuple[dict[str, object], tuple[dict, ...]]:
    """Apply document count, per-document text, and serialized-size limits."""
    truncated = source_truncated or len(documents) > LOOKUP_DOCUMENT_LIMIT
    items: list[dict[str, object]] = []
    for document in documents[:LOOKUP_DOCUMENT_LIMIT]:
        raw_text = document.get("text")
        if not isinstance(raw_text, str):
            raise TypeError("lookup document text must be a string")
        text = raw_text[:LOOKUP_DOCUMENT_TEXT_LIMIT]
        truncated = truncated or len(raw_text) > len(text)
        items.append(
            {
                "doc_id": document.get("doc_id"),
                "title": document.get("title"),
                "is_current": document.get("is_current"),
                "date": document.get("date"),
                "text": text,
            }
        )

    payload: dict[str, object] = {"documents": items, "truncated": truncated}
    while items and _serialized_length(payload) > TOOL_SERIALIZED_LIMIT:
        items.pop()
        payload["truncated"] = True
    if _serialized_length(payload) > TOOL_SERIALIZED_LIMIT:
        raise RuntimeError("lookup payload metadata exceeds the serialized limit")
    refs = tuple(_doc_ref(_required_string(item, "doc_id"), None) for item in items)
    return payload, refs


def shape_sql_result(result: SqlResult) -> dict[str, object]:
    """Drop trailing rows until the serialized SQL payload fits its budget."""
    rows = [list(row) for row in result.rows]
    payload: dict[str, object] = {
        "columns": list(result.columns),
        "rows": rows,
        "truncated": result.truncated,
    }
    while rows and _serialized_length(payload) > TOOL_SERIALIZED_LIMIT:
        rows.pop()
        payload["truncated"] = True
    if _serialized_length(payload) > TOOL_SERIALIZED_LIMIT:
        raise RuntimeError("SQL payload columns exceed the serialized limit")
    return payload


def shape_document_result(
    result: DocumentResult,
) -> tuple[dict[str, object], tuple[dict, ...]]:
    """Join document chunks in source order and cap the body at 6,000 chars."""
    chunks = sorted(result.chunks, key=lambda chunk: chunk.start)
    full_text = "".join(chunk.text for chunk in chunks)
    sections: list[str | None] = []
    for chunk in chunks:
        if chunk.section_key not in sections:
            sections.append(chunk.section_key)
    payload: dict[str, object] = {
        "doc_id": result.doc_id,
        "title": result.title,
        "sections": sections,
        "text": full_text[:DOCUMENT_TEXT_LIMIT],
        "truncated": len(full_text) > DOCUMENT_TEXT_LIMIT,
    }
    section_key = sections[0] if len(sections) == 1 else None
    return payload, (_doc_ref(result.doc_id, section_key),)


def document_text(result: DocumentResult) -> str:
    """Return one document's chunks in source order for lookup shaping."""
    chunks = sorted(result.chunks, key=lambda chunk: chunk.start)
    return "".join(chunk.text for chunk in chunks)


def _doc_ref(doc_id: str, section_key: str | None) -> dict:
    return {"kind": "doc", "doc_id": doc_id, "section_key": section_key}


def _serialized_length(payload: Mapping[str, object]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _required_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise TypeError(f"{key} must be a non-empty string")
    return item
