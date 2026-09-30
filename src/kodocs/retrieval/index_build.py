"""Build the four preregistered M5 OpenSearch indexes from local artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from kodocs.chunking.chunkers import Chunk, chunk_fixed, chunk_structural
from kodocs.chunking.corpus import (
    CorpusDoc,
    ParserCondition,
    load_corpus,
    normalize_regulation_ref,
)
from kodocs.ingest.manifest import now_iso
from kodocs.retrieval.embedding import (
    Bge3Embedder,
    CachedEmbedder,
    Embedder,
    FakeEmbedder,
    embedder_settings_hash,
)
from kodocs.retrieval.index_schema import (
    ensure_pipelines,
    index_body,
    index_name,
)
from kodocs.retrieval.opensearch import OpenSearchClient, OpenSearchError
from kodocs.synth.identifiers import extract_identifiers, service_pattern

Chunking = Literal["structural", "fixed"]

_INDEX_MANIFEST_PATH = Path("data/manifest/m5_index.json")
_PROGRESS_EVERY = 100
_EMBED_BATCH_SIZE = 100
_BULK_BATCH_SIZE = 100
_ANSWER_SETS = ("gold", "silver")


@dataclass(frozen=True)
class IndexSpec:
    """One parser-condition and chunking combination backed by one index."""

    condition: ParserCondition
    chunking: Chunking


@dataclass(frozen=True)
class BuildFailure:
    """One isolated build failure without source text."""

    stage: str
    chunk_id: str
    reason: str


@dataclass(frozen=True)
class BuildReport:
    """Result of one index build, including every item-level failure."""

    spec: IndexSpec
    index_name: str
    status: str
    document_count: int
    chunk_count: int
    indexed_count: int
    counted_count: int | None
    failures: tuple[BuildFailure, ...]
    manifest_path: Path
    max_seq_length: int
    over_limit_chunk_count: int
    gold_answer_chunks_over_limit: int
    silver_answer_chunks_over_limit: int

    @property
    def success(self) -> bool:
        """Return whether every target chunk was indexed and counted."""
        return self.status == "ok"

    @property
    def failure_count(self) -> int:
        """Return the number of recorded item or build failures."""
        return len(self.failures)

    @property
    def exit_code(self) -> int:
        """Map the build result to a CLI exit code."""
        return 0 if self.success else 1


@dataclass(frozen=True)
class _AnswerOverflow:
    set_name: str
    chunk_id: str
    doc_id: str
    query_ids: tuple[str, ...]
    token_count: int


@dataclass(frozen=True)
class _PreparedIndex:
    documents: tuple[CorpusDoc, ...]
    load_report: object
    chunks: tuple[Chunk, ...]
    document_by_id: Mapping[str, CorpusDoc]
    token_counts: tuple[int, ...]
    init_s: float
    max_seq_length: int
    over_limit_ids: tuple[str, ...]
    gold_overflows: tuple[_AnswerOverflow, ...]
    silver_overflows: tuple[_AnswerOverflow, ...]
    length_by_source: Mapping[str, Mapping[str, int | float]]
    length_by_doc_type: Mapping[str, Mapping[str, int | float]]
    failures: tuple[BuildFailure, ...]


def m5_index_specs() -> list[IndexSpec]:
    """Return the four index variants fixed by ADR-028."""
    return [
        IndexSpec("composite", "fixed"),
        IndexSpec("composite", "structural"),
        IndexSpec("pypdf", "structural"),
        IndexSpec("upstage", "structural"),
    ]


def build_index(
    spec: IndexSpec,
    *,
    client: OpenSearchClient,
    embedder: Embedder,
    root: Path = Path("."),
    recreate: bool = True,
    answer_ranges_by_set: Mapping[str, Mapping[str, Sequence[tuple[int, int]]]]
    | None = None,
) -> BuildReport:
    """Load, chunk, audit, embed, and bulk-index one deterministic variant."""
    _validate_spec(spec)
    prepared = _prepare_index(
        spec,
        embedder=embedder,
        root=root,
        answer_ranges_by_set=answer_ranges_by_set,
        tokenizer_only=False,
    )
    name = index_name(spec.condition, spec.chunking)
    failures = list(prepared.failures)

    if not prepared.chunks:
        failures.append(BuildFailure("chunking", name, "zero_chunks"))
        return _finish(
            spec,
            name=name,
            prepared=prepared,
            failures=failures,
            indexed_count=0,
            counted_count=None,
            status="failed",
            root=root,
            embedder=embedder,
        )

    if len(prepared.token_counts) != len(prepared.chunks):
        return _finish(
            spec,
            name=name,
            prepared=prepared,
            failures=failures,
            indexed_count=0,
            counted_count=None,
            status="failed_token_count",
            root=root,
            embedder=embedder,
        )

    if prepared.gold_overflows:
        for overflow in prepared.gold_overflows:
            questions = ",".join(overflow.query_ids) or "unknown"
            print(
                "ERROR: gold answer chunk exceeds max_seq_length "
                f"chunk_id={overflow.chunk_id} doc_id={overflow.doc_id} "
                f"query_ids={questions} tokens={overflow.token_count}",
                file=sys.stderr,
                flush=True,
            )
            failures.append(
                BuildFailure(
                    "token_limit",
                    overflow.chunk_id,
                    "gold_answer_chunk_over_limit",
                )
            )
        return _finish(
            spec,
            name=name,
            prepared=prepared,
            failures=failures,
            indexed_count=0,
            counted_count=None,
            status="failed_gold_answer_over_limit",
            root=root,
            embedder=embedder,
        )

    if prepared.silver_overflows:
        print(
            "WARNING: silver answer chunks exceed max_seq_length "
            f"index={name} count={len(prepared.silver_overflows)}",
            file=sys.stderr,
            flush=True,
        )

    body = _configured_index_body(spec, embedder)
    try:
        if recreate:
            try:
                client.request("DELETE", f"/{name}")
            except OpenSearchError as exc:
                if exc.status != 404:
                    raise
            client.request("PUT", f"/{name}", json=body)
        ensure_pipelines(client)
    except Exception as exc:  # noqa: BLE001 - record local service setup failure
        failures.append(BuildFailure("index_setup", name, _failure_reason(exc)))
        return _finish(
            spec,
            name=name,
            prepared=prepared,
            failures=failures,
            indexed_count=0,
            counted_count=None,
            status="failed",
            root=root,
            embedder=embedder,
        )

    embedded = _embed_chunks(
        prepared.chunks,
        prepared.token_counts,
        max_seq_length=prepared.max_seq_length,
        embedder=embedder,
        failures=failures,
    )
    indexed_count = _bulk_chunks(
        name,
        embedded,
        document_by_id=prepared.document_by_id,
        client=client,
        failures=failures,
    )
    counted_count: int | None = None
    try:
        client.request("POST", f"/{name}/_refresh")
        count_response = client.request("GET", f"/{name}/_count")
        raw_count = count_response.get("count")
        if not _is_int(raw_count) or raw_count < 0:
            raise TypeError(
                "OpenSearch _count response requires a non-negative integer"
            )
        counted_count = raw_count
    except Exception as exc:  # noqa: BLE001 - convert service failure into report
        failures.append(BuildFailure("count", name, _failure_reason(exc)))

    if indexed_count == 0:
        failures.append(BuildFailure("bulk", name, "zero_indexed_chunks"))
    if counted_count is not None and counted_count != len(prepared.chunks):
        failures.append(
            BuildFailure(
                "count",
                name,
                f"target_count_mismatch:{counted_count}/{len(prepared.chunks)}",
            )
        )
    status = "ok" if not failures else "failed"
    return _finish(
        spec,
        name=name,
        prepared=prepared,
        failures=failures,
        indexed_count=indexed_count,
        counted_count=counted_count,
        status=status,
        root=root,
        embedder=embedder,
    )


def _prepare_index(
    spec: IndexSpec,
    *,
    embedder: Embedder,
    root: Path,
    answer_ranges_by_set: Mapping[str, Mapping[str, Sequence[tuple[int, int]]]] | None,
    tokenizer_only: bool,
) -> _PreparedIndex:
    documents, load_report = load_corpus(spec.condition, root=root)
    chunks, chunk_failures = _chunk_documents(spec, documents, root=root)
    init_s = _prepare_embedder(embedder, tokenizer_only=tokenizer_only)
    settings = embedder.settings
    max_seq_length = settings.get("max_seq_length")
    if not _is_int(max_seq_length) or max_seq_length <= 0:
        raise ValueError("embedder settings require a positive max_seq_length")

    failures = list(chunk_failures)
    token_counts = _count_tokens(chunks, embedder=embedder, failures=failures)
    document_by_id = {document.doc_id: document for document in documents}
    answer_ranges = _validated_answer_ranges(answer_ranges_by_set)
    query_entries = _load_query_entries(root)
    over_limit_ids: list[str] = []
    gold_overflows: list[_AnswerOverflow] = []
    silver_overflows: list[_AnswerOverflow] = []
    if len(token_counts) == len(chunks):
        for chunk, token_count in zip(chunks, token_counts, strict=True):
            if token_count <= max_seq_length:
                continue
            over_limit_ids.append(chunk.chunk_id)
            for set_name, destination in (
                ("gold", gold_overflows),
                ("silver", silver_overflows),
            ):
                ranges = answer_ranges[set_name].get(chunk.doc_id, ())
                if not any(
                    _overlaps(chunk.start, chunk.end, start, end)
                    for start, end in ranges
                ):
                    continue
                destination.append(
                    _AnswerOverflow(
                        set_name=set_name,
                        chunk_id=chunk.chunk_id,
                        doc_id=chunk.doc_id,
                        query_ids=_query_ids_for_chunk(
                            set_name,
                            chunk,
                            document_by_id.get(chunk.doc_id),
                            query_entries,
                        ),
                        token_count=token_count,
                    )
                )

    length_by_source, length_by_doc_type = _chunk_length_stats(chunks, document_by_id)
    return _PreparedIndex(
        documents=tuple(documents),
        load_report=load_report,
        chunks=tuple(chunks),
        document_by_id=document_by_id,
        token_counts=tuple(token_counts),
        init_s=init_s,
        max_seq_length=max_seq_length,
        over_limit_ids=tuple(over_limit_ids),
        gold_overflows=tuple(gold_overflows),
        silver_overflows=tuple(silver_overflows),
        length_by_source=length_by_source,
        length_by_doc_type=length_by_doc_type,
        failures=tuple(failures),
    )


def _chunk_documents(
    spec: IndexSpec, documents: Sequence[CorpusDoc], *, root: Path
) -> tuple[list[Chunk], list[BuildFailure]]:
    fixed_size, fixed_overlap = _fixed_chunk_settings(root)
    chunks: list[Chunk] = []
    failures: list[BuildFailure] = []
    progress = _Progress("chunking", len(documents))
    for document in documents:
        try:
            produced = (
                chunk_structural(document)
                if spec.chunking == "structural"
                else chunk_fixed(document, size=fixed_size, overlap=fixed_overlap)
            )
            chunks.extend(produced)
        except Exception as exc:  # noqa: BLE001 - isolate malformed document
            failures.append(
                BuildFailure("chunking", document.doc_id, _failure_reason(exc))
            )
        progress.advance(1)
    progress.finish()
    return chunks, failures


def _fixed_chunk_settings(root: Path) -> tuple[int, int]:
    path = root / _INDEX_MANIFEST_PATH
    payload = _read_json_object(path)
    chunking = payload.get("chunking")
    if not isinstance(chunking, Mapping):
        raise TypeError(f"chunking object required: {path}")
    size = chunking.get("fixed_size")
    overlap = chunking.get("fixed_overlap")
    if not _is_int(size) or size <= 0:
        raise ValueError("m5_index chunking.fixed_size must be positive")
    if not _is_int(overlap) or overlap < 0 or overlap >= size:
        raise ValueError("m5_index chunking.fixed_overlap is invalid")
    return size, overlap


def _prepare_embedder(embedder: Embedder, *, tokenizer_only: bool) -> float:
    if tokenizer_only:
        prepare_tokenizer = getattr(embedder, "prepare_tokenizer", None)
        if callable(prepare_tokenizer):
            return float(prepare_tokenizer())
    return float(embedder.prepare())


def _count_tokens(
    chunks: Sequence[Chunk],
    *,
    embedder: Embedder,
    failures: list[BuildFailure],
) -> list[int]:
    if not chunks:
        return []
    texts = [chunk.text for chunk in chunks]
    try:
        counts = embedder.count_tokens(texts)
        return _checked_token_counts(counts, expected=len(chunks))
    except Exception:  # noqa: BLE001 - retry individually to identify the item
        counts: list[int] = []
        for chunk in chunks:
            try:
                item_counts = embedder.count_tokens([chunk.text])
                counts.extend(_checked_token_counts(item_counts, expected=1))
            except Exception as exc:  # noqa: BLE001 - identify failed item
                failures.append(
                    BuildFailure("token_count", chunk.chunk_id, _failure_reason(exc))
                )
        return counts if len(counts) == len(chunks) else []


def _checked_token_counts(values: Sequence[int], *, expected: int) -> list[int]:
    counts = list(values)
    if len(counts) != expected:
        raise RuntimeError(f"token count length mismatch:{len(counts)}/{expected}")
    if not all(_is_int(value) and value >= 0 for value in counts):
        raise TypeError("token counts must be non-negative integers")
    return counts


def _embed_chunks(
    chunks: Sequence[Chunk],
    token_counts: Sequence[int],
    *,
    max_seq_length: int,
    embedder: Embedder,
    failures: list[BuildFailure],
) -> list[tuple[Chunk, list[float]]]:
    results: dict[str, list[float]] = {}
    progress = _Progress("embedding", len(chunks))
    for batch_start in range(0, len(chunks), _EMBED_BATCH_SIZE):
        batch_chunks = chunks[batch_start : batch_start + _EMBED_BATCH_SIZE]
        batch_counts = token_counts[batch_start : batch_start + _EMBED_BATCH_SIZE]
        regular: list[Chunk] = []
        truncated: list[Chunk] = []
        for chunk, count in zip(batch_chunks, batch_counts, strict=True):
            (truncated if count > max_seq_length else regular).append(chunk)
        _embed_group(
            regular,
            embedder=embedder,
            allow_truncation=False,
            results=results,
            failures=failures,
        )
        _embed_group(
            truncated,
            embedder=embedder,
            allow_truncation=True,
            results=results,
            failures=failures,
        )
        progress.advance(len(batch_chunks))
    progress.finish()
    return [
        (chunk, results[chunk.chunk_id])
        for chunk in chunks
        if chunk.chunk_id in results
    ]


def _embed_group(
    chunks: Sequence[Chunk],
    *,
    embedder: Embedder,
    allow_truncation: bool,
    results: dict[str, list[float]],
    failures: list[BuildFailure],
) -> None:
    if not chunks:
        return
    try:
        vectors = _call_embedder(
            embedder,
            [chunk.text for chunk in chunks],
            allow_truncation=allow_truncation,
        )
        checked = _checked_vectors(vectors, expected=len(chunks), dim=embedder.dim)
        results.update(
            (chunk.chunk_id, vector)
            for chunk, vector in zip(chunks, checked, strict=True)
        )
        return
    except Exception as exc:  # noqa: BLE001 - retry a failed batch by item
        if len(chunks) == 1:
            failures.append(
                BuildFailure("embedding", chunks[0].chunk_id, _failure_reason(exc))
            )
            return

    for chunk in chunks:
        try:
            vectors = _call_embedder(
                embedder,
                [chunk.text],
                allow_truncation=allow_truncation,
            )
            results[chunk.chunk_id] = _checked_vectors(
                vectors, expected=1, dim=embedder.dim
            )[0]
        except Exception as exc:  # noqa: BLE001 - isolate one failed chunk
            failures.append(
                BuildFailure("embedding", chunk.chunk_id, _failure_reason(exc))
            )


def _call_embedder(
    embedder: Embedder,
    texts: Sequence[str],
    *,
    allow_truncation: bool,
) -> list[list[float]]:
    if allow_truncation:
        explicit = getattr(embedder, "embed_allowing_truncation", None)
        if callable(explicit):
            return explicit(texts, kind="passage")
    return embedder.embed(texts, kind="passage")


def _checked_vectors(
    vectors: Sequence[Sequence[float]], *, expected: int, dim: int
) -> list[list[float]]:
    values = [list(vector) for vector in vectors]
    if len(values) != expected:
        raise RuntimeError(f"embedding vector count mismatch:{len(values)}/{expected}")
    for vector in values:
        if len(vector) != dim or not all(math.isfinite(float(item)) for item in vector):
            raise ValueError(f"embedding vector must contain {dim} finite values")
    return [[float(item) for item in vector] for vector in values]


def _bulk_chunks(
    name: str,
    chunks_and_vectors: Sequence[tuple[Chunk, list[float]]],
    *,
    document_by_id: Mapping[str, CorpusDoc],
    client: OpenSearchClient,
    failures: list[BuildFailure],
) -> int:
    indexed_count = 0
    progress = _Progress("bulk", len(chunks_and_vectors))
    for start in range(0, len(chunks_and_vectors), _BULK_BATCH_SIZE):
        batch = chunks_and_vectors[start : start + _BULK_BATCH_SIZE]
        docs = [
            (
                chunk.chunk_id,
                _index_document(chunk, document_by_id[chunk.doc_id], vector),
            )
            for chunk, vector in batch
        ]
        try:
            result = client.bulk(name, docs)
        except Exception as exc:  # noqa: BLE001 - continue with later bulk batches
            reason = _failure_reason(exc)
            failures.extend(
                BuildFailure("bulk", chunk.chunk_id, reason) for chunk, _ in batch
            )
        else:
            indexed_count += result.succeeded_count
            failures.extend(
                BuildFailure(
                    "bulk",
                    error.doc_id or name,
                    _bulk_failure_reason(error.status, error.error),
                )
                for error in result.errors
            )
        progress.advance(len(batch))
    progress.finish()
    return indexed_count


def _index_document(
    chunk: Chunk, document: CorpusDoc, vector: Sequence[float]
) -> dict[str, object]:
    identifiers = _chunk_identifiers(chunk, document)
    return {
        "chunk_id": chunk.chunk_id,
        "doc_id": chunk.doc_id,
        "chunking": chunk.chunking,
        "text_std": chunk.text,
        "text_nori": chunk.text,
        "vector": list(vector),
        "title": document.title,
        "doc_type": document.doc_type,
        "source": document.source,
        "team": document.team,
        "rule_id": document.rule_id,
        "date": document.date,
        "is_current": document.is_current,
        "access_level": document.access.level,
        "access_teams": list(document.access.teams),
        "access_min_grade": document.access.min_grade,
        "section_key": chunk.section_key,
        "section_kind": chunk.section_kind,
        "start": chunk.start,
        "end": chunk.end,
        **{f"ids_{kind}": list(values) for kind, values in identifiers.items()},
    }


def _chunk_identifiers(chunk: Chunk, document: CorpusDoc) -> dict[str, tuple[str, ...]]:
    extracted = extract_identifiers(chunk.body)
    result: dict[str, tuple[str, ...]] = {
        kind: _unique(item[0] for item in extracted[kind])
        for kind in ("ticket", "approval", "incident")
    }

    service_names = document.identifiers.get("service", ())
    services = service_pattern(service_names)
    result["service"] = _unique(
        match.group() for match in services.finditer(chunk.body)
    )

    normalized_body = normalize_regulation_ref(chunk.body)
    regulation_refs = [
        reference
        for reference in document.identifiers.get("regulation", ())
        if reference and reference in normalized_body
    ]
    if document.doc_type == "regulation":
        regulation_refs.extend(
            normalize_regulation_ref(f"{document.title} {section.key}")
            for section in document.sections
            if section.kind == "article"
            and section.key is not None
            and _overlaps(chunk.start, chunk.end, section.start, section.end)
        )
    result["regulation"] = _unique(regulation_refs)
    return result


def _finish(
    spec: IndexSpec,
    *,
    name: str,
    prepared: _PreparedIndex,
    failures: Sequence[BuildFailure],
    indexed_count: int,
    counted_count: int | None,
    status: str,
    root: Path,
    embedder: Embedder,
) -> BuildReport:
    manifest_path = root / _INDEX_MANIFEST_PATH
    payload = _manifest_entry(
        spec,
        prepared=prepared,
        failures=failures,
        indexed_count=indexed_count,
        counted_count=counted_count,
        status=status,
        root=root,
        embedder=embedder,
    )
    _merge_index_manifest(manifest_path, name, payload)
    return BuildReport(
        spec=spec,
        index_name=name,
        status=status,
        document_count=len(prepared.documents),
        chunk_count=len(prepared.chunks),
        indexed_count=indexed_count,
        counted_count=counted_count,
        failures=tuple(failures),
        manifest_path=manifest_path,
        max_seq_length=prepared.max_seq_length,
        over_limit_chunk_count=len(prepared.over_limit_ids),
        gold_answer_chunks_over_limit=len(prepared.gold_overflows),
        silver_answer_chunks_over_limit=len(prepared.silver_overflows),
    )


def _manifest_entry(
    spec: IndexSpec,
    *,
    prepared: _PreparedIndex,
    failures: Sequence[BuildFailure],
    indexed_count: int,
    counted_count: int | None,
    status: str,
    root: Path,
    embedder: Embedder,
) -> dict[str, object]:
    token_counts = sorted(prepared.token_counts)
    settings = embedder.settings
    schema = _configured_index_body(spec, embedder)["mappings"]["properties"]
    document_counts = {
        "synthetic": sum(doc.source == "synthetic" for doc in prepared.documents),
        "real_current": sum(
            doc.source == "real" and doc.is_current for doc in prepared.documents
        ),
        "real_prior": sum(
            doc.source == "real" and not doc.is_current for doc in prepared.documents
        ),
        "total": len(prepared.documents),
    }
    return {
        "status": status,
        "condition": spec.condition,
        "chunking": spec.chunking,
        "document_counts": document_counts,
        "chunk_count": len(prepared.chunks),
        "indexed_chunk_count": indexed_count,
        "opensearch_count": counted_count,
        "failure_count": len(failures),
        "failures": [
            {
                "stage": failure.stage,
                "chunk_id": failure.chunk_id,
                "reason": failure.reason,
            }
            for failure in failures
        ],
        "embedder": {
            "model_id": embedder.model_id,
            "revision": embedder.revision,
            "settings_hash": embedder_settings_hash(embedder),
            "device": settings.get("device"),
            "init_s": prepared.init_s,
        },
        "annex_fallback_count": int(
            getattr(prepared.load_report, "composite_annex_fallback_count", 0)
        ),
        "built_at": now_iso(),
        "code_commit": _git_commit(root),
        "index_field_schema_hash": _canonical_hash(schema),
        "max_seq_length": prepared.max_seq_length,
        "over_limit_chunk_count": len(prepared.over_limit_ids),
        "over_limit_chunk_ids": list(prepared.over_limit_ids),
        "chunk_token_count_max": token_counts[-1] if token_counts else 0,
        "chunk_token_count_p99": _nearest_rank(token_counts, 0.99),
        "gold_answer_chunks_over_limit": len(prepared.gold_overflows),
        "silver_answer_chunks_over_limit": len(prepared.silver_overflows),
        "gold_answer_over_limit_chunks": [
            _overflow_payload(overflow) for overflow in prepared.gold_overflows
        ],
        "silver_answer_over_limit_chunks": [
            _overflow_payload(overflow) for overflow in prepared.silver_overflows
        ],
        "chunk_length_chars_by_source": prepared.length_by_source,
        "chunk_length_chars_by_doc_type": prepared.length_by_doc_type,
    }


def _overflow_payload(overflow: _AnswerOverflow) -> dict[str, object]:
    return {
        "chunk_id": overflow.chunk_id,
        "doc_id": overflow.doc_id,
        "query_ids": list(overflow.query_ids),
        "token_count": overflow.token_count,
    }


def _configured_index_body(spec: IndexSpec, embedder: Embedder) -> dict[str, object]:
    body = index_body(dim=embedder.dim)
    body["mappings"]["_meta"] = {
        "condition": spec.condition,
        "chunking": spec.chunking,
        "embedder_settings_hash": embedder_settings_hash(embedder),
    }
    return body


def _chunk_length_stats(
    chunks: Sequence[Chunk], document_by_id: Mapping[str, CorpusDoc]
) -> tuple[dict[str, dict[str, int | float]], dict[str, dict[str, int | float]]]:
    by_source: dict[str, list[int]] = defaultdict(list)
    by_doc_type: dict[str, list[int]] = defaultdict(list)
    for chunk in chunks:
        document = document_by_id[chunk.doc_id]
        length = len(chunk.body)
        by_source[document.source].append(length)
        by_doc_type[document.doc_type].append(length)
    return _length_groups(by_source), _length_groups(by_doc_type)


def _length_groups(
    groups: Mapping[str, Sequence[int]],
) -> dict[str, dict[str, int | float]]:
    return {
        key: {"count": len(values), "median": _median(sorted(values))}
        for key, values in sorted(groups.items())
    }


def _median(values: Sequence[int]) -> int | float:
    if not values:
        return 0
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    total = values[middle - 1] + values[middle]
    return total // 2 if total % 2 == 0 else total / 2


def _nearest_rank(values: Sequence[int], quantile: float) -> int:
    if not values:
        return 0
    rank = max(1, math.ceil(quantile * len(values)))
    return values[rank - 1]


def _validated_answer_ranges(
    value: Mapping[str, Mapping[str, Sequence[tuple[int, int]]]] | None,
) -> dict[str, dict[str, tuple[tuple[int, int], ...]]]:
    result: dict[str, dict[str, tuple[tuple[int, int], ...]]] = {
        "gold": {},
        "silver": {},
    }
    if value is None:
        return result
    for set_name in _ANSWER_SETS:
        raw_documents = value.get(set_name, {})
        if not isinstance(raw_documents, Mapping):
            raise TypeError(f"answer_ranges_by_set[{set_name}] must be a mapping")
        for doc_id, raw_ranges in raw_documents.items():
            if not isinstance(doc_id, str):
                raise TypeError("answer range document IDs must be strings")
            ranges: list[tuple[int, int]] = []
            for item in raw_ranges:
                if (
                    not isinstance(item, Sequence)
                    or isinstance(item, str | bytes)
                    or len(item) != 2
                    or not _is_int(item[0])
                    or not _is_int(item[1])
                    or item[0] < 0
                    or item[1] < item[0]
                ):
                    raise ValueError(f"invalid answer range for {set_name}:{doc_id}")
                ranges.append((item[0], item[1]))
            result[set_name][doc_id] = tuple(ranges)
    return result


def _load_query_entries(root: Path) -> dict[str, tuple[Mapping[str, object], ...]]:
    result: dict[str, tuple[Mapping[str, object], ...]] = {}
    for set_name in _ANSWER_SETS:
        path = root / f"data/golden/queries/{set_name}.jsonl"
        entries: list[Mapping[str, object]] = []
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, Mapping):
                    raise TypeError(f"JSONL object required: {path}")
                entries.append(value)
        result[set_name] = tuple(entries)
    return result


def _query_ids_for_chunk(
    set_name: str,
    chunk: Chunk,
    document: CorpusDoc | None,
    entries: Mapping[str, Sequence[Mapping[str, object]]],
) -> tuple[str, ...]:
    query_ids: list[str] = []
    for query in entries.get(set_name, ()):
        query_id = query.get("id")
        qrels = query.get("qrels", ())
        if not isinstance(query_id, str) or not isinstance(qrels, Sequence):
            continue
        if any(_qrel_matches_chunk(qrel, chunk, document) for qrel in qrels):
            query_ids.append(query_id)
    return _unique(query_ids)


def _qrel_matches_chunk(qrel: object, chunk: Chunk, document: CorpusDoc | None) -> bool:
    if not isinstance(qrel, Mapping) or qrel.get("doc_id") != chunk.doc_id:
        return False
    relevance = qrel.get("rel", 0)
    if not _is_int(relevance) or relevance < 1:
        return False
    start = qrel.get("start")
    end = qrel.get("end")
    if _is_int(start) and _is_int(end):
        return _overlaps(chunk.start, chunk.end, start, end)
    key = qrel.get("article_key") or qrel.get("annex_id")
    if isinstance(key, str) and document is not None:
        return any(
            section.key == key
            and _overlaps(chunk.start, chunk.end, section.start, section.end)
            for section in document.sections
        )
    return True


def _load_answer_ranges(
    condition: ParserCondition, root: Path
) -> Mapping[str, Mapping[str, Sequence[tuple[int, int]]]]:
    try:
        from kodocs.eval.m5_inputs import answer_ranges_by_doc
    except ImportError as exc:
        raise RuntimeError(
            "kodocs.eval.m5_inputs.answer_ranges_by_doc가 없습니다; "
            "M5 step 11을 완료한 뒤 색인을 실행하세요"
        ) from exc
    return answer_ranges_by_doc(condition, root)


def _dry_run_index(
    spec: IndexSpec,
    *,
    embedder: Embedder,
    root: Path,
    answer_ranges_by_set: Mapping[str, Mapping[str, Sequence[tuple[int, int]]]],
) -> bool:
    prepared = _prepare_index(
        spec,
        embedder=embedder,
        root=root,
        answer_ranges_by_set=answer_ranges_by_set,
        tokenizer_only=True,
    )
    payload = {
        "index": index_name(spec.condition, spec.chunking),
        "documents": len(prepared.documents),
        "chunks": len(prepared.chunks),
        "expected_embeddings": len(prepared.chunks),
        "max_seq_length": prepared.max_seq_length,
        "over_limit_chunk_count": len(prepared.over_limit_ids),
        "over_limit_chunk_ids": list(prepared.over_limit_ids),
        "gold_answer_chunks_over_limit": len(prepared.gold_overflows),
        "silver_answer_chunks_over_limit": len(prepared.silver_overflows),
        "chunk_token_count_max": max(prepared.token_counts, default=0),
        "chunk_token_count_p99": _nearest_rank(sorted(prepared.token_counts), 0.99),
        "chunk_length_chars_by_source": prepared.length_by_source,
        "chunk_length_chars_by_doc_type": prepared.length_by_doc_type,
        "failure_count": len(prepared.failures),
    }
    print(
        f"dry-run {json.dumps(payload, ensure_ascii=False, sort_keys=True)}", flush=True
    )
    if prepared.silver_overflows:
        print(
            "WARNING: silver answer chunks exceed max_seq_length "
            f"index={payload['index']} count={len(prepared.silver_overflows)}",
            file=sys.stderr,
            flush=True,
        )
    if prepared.gold_overflows:
        for overflow in prepared.gold_overflows:
            questions = ",".join(overflow.query_ids) or "unknown"
            print(
                "ERROR: gold answer chunk exceeds max_seq_length "
                f"chunk_id={overflow.chunk_id} doc_id={overflow.doc_id} "
                f"query_ids={questions} tokens={overflow.token_count}",
                file=sys.stderr,
                flush=True,
            )
    return (
        bool(prepared.chunks) and not prepared.failures and not prepared.gold_overflows
    )


def _merge_index_manifest(path: Path, name: str, payload: Mapping[str, object]) -> None:
    manifest = _read_json_object(path) if path.is_file() else {}
    indexes = manifest.get("indexes", {})
    if not isinstance(indexes, Mapping):
        raise TypeError(f"indexes object required: {path}")
    manifest["indexes"] = {**indexes, name: dict(payload)}
    _write_json_atomic(path, manifest)


def _write_json_atomic(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as destination:
            json.dump(value, destination, ensure_ascii=False, indent=2, sort_keys=True)
            destination.write("\n")
            temporary_path = Path(destination.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _read_json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON object required: {path}")
    return value


def _git_commit(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if value else None


def _canonical_hash(value: object) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def _bulk_failure_reason(status: int | None, error: object) -> str:
    error_type = error.get("type") if isinstance(error, Mapping) else None
    label = error_type if isinstance(error_type, str) else type(error).__name__
    return f"bulk_item_http_{status}:{label}"


def _failure_reason(exc: Exception) -> str:
    message = str(exc).splitlines()[0][:160]
    return f"{type(exc).__name__}:{message}" if message else type(exc).__name__


def _overlaps(left_start: int, left_end: int, right_start: int, right_end: int) -> bool:
    return max(left_start, right_start) < min(left_end, right_end)


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_spec(spec: IndexSpec) -> None:
    if spec.condition not in {"composite", "pypdf", "upstage"}:
        raise ValueError(f"unsupported parser condition: {spec.condition}")
    if spec.chunking not in {"structural", "fixed"}:
        raise ValueError(f"unsupported chunking: {spec.chunking}")


class _Progress:
    def __init__(self, stage: str, total: int) -> None:
        self.stage = stage
        self.total = total
        self.processed = 0
        self.last_printed = 0

    def advance(self, count: int) -> None:
        previous = self.processed
        self.processed += count
        next_mark = ((previous // _PROGRESS_EVERY) + 1) * _PROGRESS_EVERY
        while next_mark <= self.processed:
            self._print(next_mark)
            next_mark += _PROGRESS_EVERY

    def finish(self) -> None:
        if self.total and self.last_printed != self.total:
            self._print(self.total)

    def _print(self, processed: int) -> None:
        print(f"{self.stage} progress {processed}/{self.total}", flush=True)
        self.last_printed = processed


def _make_embedder(name: str, *, root: Path) -> Embedder:
    if name == "fake":
        return FakeEmbedder()
    return CachedEmbedder(
        Bge3Embedder(),
        root / "data/interim/m5_embedding_cache",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Build all or one M5 index, or audit it without OpenSearch access."""
    parser = argparse.ArgumentParser(description="M5 OpenSearch 색인을 빌드합니다.")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true")
    scope.add_argument("--condition", choices=("composite", "pypdf", "upstage"))
    parser.add_argument("--chunking", choices=("structural", "fixed"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--embedder", choices=("bge-m3", "fake"), default="bge-m3")
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    if args.all and args.chunking is not None:
        parser.error("--chunking cannot be used with --all")
    if args.condition is not None and args.chunking is None:
        parser.error("--condition requires --chunking")

    specs = m5_index_specs() if args.all else [IndexSpec(args.condition, args.chunking)]
    embedder = _make_embedder(args.embedder, root=args.root)
    try:
        answer_ranges = {
            condition: _load_answer_ranges(condition, args.root)
            for condition in dict.fromkeys(spec.condition for spec in specs)
        }
        if args.dry_run:
            succeeded = [
                _dry_run_index(
                    spec,
                    embedder=embedder,
                    root=args.root,
                    answer_ranges_by_set=answer_ranges[spec.condition],
                )
                for spec in specs
            ]
            return int(not all(succeeded))

        with OpenSearchClient() as client:
            reports = [
                build_index(
                    spec,
                    client=client,
                    embedder=embedder,
                    root=args.root,
                    answer_ranges_by_set=answer_ranges[spec.condition],
                )
                for spec in specs
            ]
        for report in reports:
            print(
                f"index={report.index_name} status={report.status} "
                f"chunks={report.chunk_count} indexed={report.indexed_count} "
                f"failures={report.failure_count}",
                flush=True,
            )
        return int(not all(report.success for report in reports))
    except Exception as exc:  # noqa: BLE001 - CLI reports one bounded local failure
        print(f"색인 빌드 실패: {_failure_reason(exc)}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
