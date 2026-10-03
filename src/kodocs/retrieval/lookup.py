"""Exact identifier extraction and authorized OpenSearch lookup."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Self

from kodocs.chunking.corpus import normalize_regulation_ref
from kodocs.retrieval.access import UserContext, access_filter
from kodocs.retrieval.opensearch import OpenSearchClient
from kodocs.retrieval.ordering import prefer_current_order
from kodocs.retrieval.search import DocHit
from kodocs.synth.identifiers import extract_identifiers, service_pattern

IdKind = Literal["ticket", "approval", "incident", "service", "regulation"]

_ID_KINDS = frozenset({"ticket", "approval", "incident", "service", "regulation"})
_SOURCE_FIELDS = (
    "chunk_id",
    "doc_id",
    "date",
    "title",
    "is_current",
    "rule_id",
)


class LookupDocuments(tuple):  # type: ignore[type-arg]
    """Grouped documents (a plain tuple of ``DocHit``) plus how the chunk window ended.

    ``chunk_limit_reached`` means OpenSearch returned exactly the requested chunk
    count, so documents further down the order may be missing.
    """

    chunks_returned: int
    chunk_limit_reached: bool

    def __new__(
        cls,
        documents: Sequence[DocHit],
        *,
        chunks_returned: int,
        chunk_limit_reached: bool,
    ) -> Self:
        self = super().__new__(cls, documents)
        self.chunks_returned = chunks_returned
        self.chunk_limit_reached = chunk_limit_reached
        return self


@dataclass(frozen=True)
class _LookupChunk:
    chunk_id: str
    doc_id: str
    score: float
    date: str | None
    title: str | None
    is_current: bool | None
    rule_id: str | None


def extract_query_identifiers(
    question: str, *, service_names: Sequence[str] = ()
) -> list[tuple[IdKind, str]]:
    """Extract registered identifiers in their source-text order."""
    if not isinstance(question, str):
        raise TypeError("question must be a string")

    extracted = extract_identifiers(question)
    occurrences: list[tuple[int, int, IdKind, str]] = []
    for kind in ("ticket", "approval", "incident", "regulation"):
        for value, start, end in extracted[kind]:
            normalized = (
                normalize_regulation_ref(value) if kind == "regulation" else value
            )
            occurrences.append((start, end, kind, normalized))
    for match in service_pattern(service_names).finditer(question):
        occurrences.append((match.start(), match.end(), "service", match.group()))

    occurrences.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return [(kind, value) for _, _, kind, value in occurrences]


def authorized_lookup_id(
    client: OpenSearchClient,
    index: str,
    ctx: UserContext,
    kind: IdKind,
    value: str,
    *,
    rank_query: str | None = None,
    size: int = 50,
    prefer_current: bool = False,
) -> tuple[DocHit, ...]:
    """Look up an exact identifier with authorization in the same query."""
    if kind not in _ID_KINDS:
        raise ValueError("unsupported identifier kind")
    if not isinstance(value, str) or not value:
        raise ValueError("identifier value must be a non-empty string")
    if rank_query is not None and not isinstance(rank_query, str):
        raise TypeError("rank_query must be a string or None")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError("size must be a positive integer")

    normalized = normalize_regulation_ref(value) if kind == "regulation" else value
    boolean: dict[str, object] = {
        "filter": [
            access_filter(ctx),
            {"term": {f"ids_{kind}": normalized}},
        ]
    }
    if rank_query is not None:
        boolean["should"] = [{"match": {"text_nori": rank_query}}]
        sort = [
            {"_score": {"order": "desc"}},
            {"doc_id": {"order": "asc"}},
            {"chunk_id": {"order": "asc"}},
        ]
    else:
        sort = [
            {"date": {"order": "desc", "missing": "_last"}},
            {"doc_id": {"order": "asc"}},
            {"chunk_id": {"order": "asc"}},
        ]

    response = client.request(
        "POST",
        f"/{index}/_search",
        json={
            "size": size,
            "track_total_hits": False,
            "track_scores": True,
            "_source": list(_SOURCE_FIELDS),
            "query": {"bool": boolean},
            "sort": sort,
        },
    )
    chunks = _lookup_chunks(response, ranked=rank_query is not None)
    documents = _group_documents(
        chunks,
        ranked=rank_query is not None,
        prefer_current=prefer_current,
    )
    return LookupDocuments(
        documents, chunks_returned=len(chunks), chunk_limit_reached=len(chunks) >= size
    )


def _lookup_chunks(
    response: Mapping[str, object], *, ranked: bool
) -> tuple[_LookupChunk, ...]:
    wrapper = response.get("hits")
    if not isinstance(wrapper, Mapping):
        raise TypeError("OpenSearch lookup response has invalid hits")
    raw_hits = wrapper.get("hits")
    if not isinstance(raw_hits, list):
        raise TypeError("OpenSearch lookup response has invalid hits")

    chunks: list[_LookupChunk] = []
    for raw_hit in raw_hits:
        if not isinstance(raw_hit, Mapping):
            raise TypeError("OpenSearch lookup response has an invalid hit")
        source = raw_hit.get("_source")
        if not isinstance(source, Mapping):
            raise TypeError("OpenSearch lookup response has an invalid hit")
        chunk_id = _required_string(source, "chunk_id")
        doc_id = _required_string(source, "doc_id")
        score_value = raw_hit.get("_score")
        if score_value is None and not ranked:
            score = 0.0
        elif (
            isinstance(score_value, bool)
            or not isinstance(score_value, int | float)
            or not math.isfinite(float(score_value))
        ):
            raise TypeError("OpenSearch lookup response has an invalid score")
        else:
            score = float(score_value)
        date = source.get("date")
        if date is not None and not isinstance(date, str):
            raise TypeError("OpenSearch lookup response has an invalid date")
        title = _optional_string(source, "title")
        is_current = _optional_bool(source, "is_current")
        rule_id = _optional_string(source, "rule_id")
        chunks.append(
            _LookupChunk(
                chunk_id,
                doc_id,
                score,
                date,
                title,
                is_current,
                rule_id,
            )
        )
    return tuple(chunks)


def _required_string(source: Mapping[str, object], field: str) -> str:
    value = source.get(field)
    if not isinstance(value, str) or not value:
        raise RuntimeError("OpenSearch lookup response has an invalid source field")
    return value


def _optional_string(source: Mapping[str, object], field: str) -> str | None:
    value = source.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("OpenSearch lookup response has an invalid source field")
    return value


def _optional_bool(source: Mapping[str, object], field: str) -> bool | None:
    value = source.get(field)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise TypeError("OpenSearch lookup response has an invalid source field")
    return value


def _group_documents(
    chunks: Sequence[_LookupChunk], *, ranked: bool, prefer_current: bool = False
) -> tuple[DocHit, ...]:
    best_by_doc: dict[str, _LookupChunk] = {}
    date_by_doc: dict[str, str | None] = {}
    for chunk in chunks:
        previous = best_by_doc.get(chunk.doc_id)
        if previous is None or (-chunk.score, chunk.chunk_id) < (
            -previous.score,
            previous.chunk_id,
        ):
            best_by_doc[chunk.doc_id] = chunk
        previous_date = date_by_doc.get(chunk.doc_id)
        if previous_date is None or (
            chunk.date is not None and chunk.date > previous_date
        ):
            date_by_doc[chunk.doc_id] = chunk.date

    docs = [
        DocHit(
            doc_id,
            chunk.score,
            chunk.chunk_id,
            title=chunk.title,
            is_current=chunk.is_current,
            rule_id=chunk.rule_id,
            date=date_by_doc.get(doc_id),
        )
        for doc_id, chunk in best_by_doc.items()
    ]
    if ranked:
        docs.sort(key=lambda hit: (-hit.score, hit.doc_id))
    else:
        docs.sort(key=lambda hit: hit.doc_id)
        docs.sort(key=lambda hit: date_by_doc[hit.doc_id] or "", reverse=True)
    if prefer_current:
        return prefer_current_order(
            docs,
            key_rule=lambda hit: hit.rule_id,
            key_is_current=lambda hit: hit.is_current,
        )
    return tuple(docs)
