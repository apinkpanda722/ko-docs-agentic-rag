"""Deterministic structural and fixed-size chunkers for the M5 corpus."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from kodocs.chunking.corpus import CorpusDoc, CorpusLoadError, load_corpus
from kodocs.ingest.manifest import now_iso

_INDEX_MANIFEST_PATH = Path("data/manifest/m5_index.json")


@dataclass(frozen=True)
class Chunk:
    """One retrieval unit whose body remains an exact document slice."""

    chunk_id: str
    doc_id: str
    chunking: str
    seq: int
    prefix: str
    body: str
    start: int
    end: int
    section_kind: str | None
    section_key: str | None

    @property
    def text(self) -> str:
        """Return the index and embedding text, including structural context."""
        return f"{self.prefix}\n{self.body}" if self.prefix else self.body


def chunk_structural(doc: CorpusDoc) -> list[Chunk]:
    """Project the corpus loader's sections into chunks without changing bodies."""
    chunks: list[Chunk] = []
    for section in doc.sections:
        if (
            not _is_int(section.start)
            or not _is_int(section.end)
            or section.start < 0
            or section.end < section.start
            or section.end > len(doc.text)
        ):
            raise ValueError(
                f"{doc.doc_id}: invalid section range "
                f"[{section.start}, {section.end}) for text length {len(doc.text)}"
            )
        body = doc.text[section.start : section.end]
        if not body.strip():
            continue
        seq = len(chunks) + 1
        prefix = " > ".join(section.path) if doc.doc_type == "regulation" else ""
        chunks.append(
            Chunk(
                chunk_id=f"{doc.doc_id}#structural-{seq:04d}",
                doc_id=doc.doc_id,
                chunking="structural",
                seq=seq,
                prefix=prefix,
                body=body,
                start=section.start,
                end=section.end,
                section_kind=section.kind,
                section_key=section.key,
            )
        )
    return chunks


def chunk_fixed(doc: CorpusDoc, *, size: int, overlap: int) -> list[Chunk]:
    """Split the verbatim document text with character windows and fixed stride."""
    if not _is_int(size) or size <= 0:
        raise ValueError("size must be a positive integer")
    if not _is_int(overlap) or overlap < 0 or overlap >= size:
        raise ValueError("overlap must be an integer in the range [0, size)")
    if not doc.text:
        return []

    text_length = len(doc.text)
    if text_length <= size:
        spans = [(0, text_length)]
    else:
        spans: list[tuple[int, int]] = []
        stride = size - overlap
        start = 0
        while True:
            end = start + size
            if end >= text_length:
                candidate = (text_length - size, text_length)
                if not spans or not _is_contained(candidate, spans[-1]):
                    spans.append(candidate)
                break
            spans.append((start, end))
            start += stride

    return [
        Chunk(
            chunk_id=f"{doc.doc_id}#fixed-{seq:04d}",
            doc_id=doc.doc_id,
            chunking="fixed",
            seq=seq,
            prefix="",
            body=doc.text[start:end],
            start=start,
            end=end,
            section_kind=None,
            section_key=None,
        )
        for seq, (start, end) in enumerate(spans, start=1)
    ]


def fixed_size_from_structural(chunks: Sequence[Chunk]) -> tuple[int, int]:
    """Derive the fixed character size from structural body-length median."""
    if not chunks:
        raise ValueError("structural chunks must not be empty")
    lengths = sorted(len(chunk.body) for chunk in chunks)
    if not lengths[0]:
        raise ValueError("structural chunks must not contain empty bodies")
    twice_median = _twice_median(lengths)
    size = max(50, ((twice_median + 50) // 100) * 50)
    return size, size * 20 // 100


def _twice_median(sorted_values: Sequence[int]) -> int:
    middle = len(sorted_values) // 2
    if len(sorted_values) % 2:
        return sorted_values[middle] * 2
    return sorted_values[middle - 1] + sorted_values[middle]


def _median_value(sorted_values: Sequence[int]) -> int | float:
    twice_median = _twice_median(sorted_values)
    return twice_median // 2 if twice_median % 2 == 0 else twice_median / 2


def _nearest_rank_p90(sorted_values: Sequence[int]) -> int:
    rank = (9 * len(sorted_values) + 9) // 10
    return sorted_values[rank - 1]


def _is_contained(candidate: tuple[int, int], previous: tuple[int, int]) -> bool:
    return candidate[0] >= previous[0] and candidate[1] <= previous[1]


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _chunking_manifest(
    documents: Sequence[CorpusDoc],
    structural_chunks: Sequence[Chunk],
    fixed_chunks: Sequence[Chunk],
    *,
    size: int,
    overlap: int,
) -> dict[str, object]:
    lengths = sorted(len(chunk.body) for chunk in structural_chunks)
    document_by_id = {document.doc_id: document for document in documents}
    by_document_type: Counter[str] = Counter()
    by_source: Counter[str] = Counter()
    for chunk in structural_chunks:
        document = document_by_id[chunk.doc_id]
        by_document_type[document.doc_type] += 1
        by_source[document.source] += 1
    return {
        "calculated_at": now_iso(),
        "composite_corpus_document_count": len(documents),
        "fixed_chunk_count": len(fixed_chunks),
        "fixed_overlap": overlap,
        "fixed_size": size,
        "structural_body_length_chars": {
            "max": lengths[-1],
            "median": _median_value(lengths),
            "min": lengths[0],
            "p90": _nearest_rank_p90(lengths),
        },
        "structural_chunk_count": len(structural_chunks),
        "structural_chunk_count_by_document_type": dict(
            sorted(by_document_type.items())
        ),
        "structural_chunk_count_by_source": dict(sorted(by_source.items())),
    }


def _merge_manifest(path: Path, chunking_payload: Mapping[str, object]) -> None:
    manifest: dict[str, object] = {}
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise TypeError(f"JSON object required: {path}")
        manifest.update(loaded)
    existing_chunking = manifest.get("chunking", {})
    if not isinstance(existing_chunking, Mapping):
        raise TypeError(f"chunking object required: {path}")
    manifest["chunking"] = {**existing_chunking, **chunking_payload}
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


def _print_failures(failures: Sequence[tuple[str, str, str]], *, out: TextIO) -> None:
    for stage, doc_id, reason in failures:
        print(
            f"ERROR: stage={stage} doc_id={doc_id} reason={reason}",
            file=out,
            flush=True,
        )


def _failure_reason(exc: Exception) -> str:
    message = str(exc).splitlines()[0][:160]
    return f"{type(exc).__name__}:{message}" if message else type(exc).__name__


def main(argv: list[str] | None = None) -> int:
    """Chunk the local composite corpus and record its fixed-size decision."""
    parser = argparse.ArgumentParser(
        description="M5 구조·고정 청킹 통계를 결정론적으로 계산합니다."
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)

    try:
        documents, _report = load_corpus("composite", root=args.root)
    except (
        CorpusLoadError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        sqlite3.Error,
    ) as exc:
        print(f"코퍼스 로드 실패: {_failure_reason(exc)}", file=sys.stderr, flush=True)
        return 1
    if not documents:
        print("코퍼스 로드 실패: 문서가 0건입니다.", file=sys.stderr, flush=True)
        return 1

    structural_chunks: list[Chunk] = []
    failures: list[tuple[str, str, str]] = []
    total = len(documents)
    for index, document in enumerate(documents, start=1):
        print(
            f"structural {index}/{total} doc_id={document.doc_id}",
            flush=True,
        )
        try:
            structural_chunks.extend(chunk_structural(document))
        except Exception as exc:  # noqa: BLE001 - isolate each corpus document
            failures.append(("structural", document.doc_id, _failure_reason(exc)))
    if failures:
        _print_failures(failures, out=sys.stderr)
        return 1

    try:
        size, overlap = fixed_size_from_structural(structural_chunks)
    except ValueError as exc:
        print(
            f"고정 청크 크기 계산 실패: {_failure_reason(exc)}",
            file=sys.stderr,
            flush=True,
        )
        return 1

    fixed_chunks: list[Chunk] = []
    for index, document in enumerate(documents, start=1):
        print(f"fixed {index}/{total} doc_id={document.doc_id}", flush=True)
        try:
            fixed_chunks.extend(chunk_fixed(document, size=size, overlap=overlap))
        except Exception as exc:  # noqa: BLE001 - isolate each corpus document
            failures.append(("fixed", document.doc_id, _failure_reason(exc)))
    if failures:
        _print_failures(failures, out=sys.stderr)
        return 1

    try:
        chunking_payload = _chunking_manifest(
            documents,
            structural_chunks,
            fixed_chunks,
            size=size,
            overlap=overlap,
        )
        destination = args.root / _INDEX_MANIFEST_PATH
        _merge_manifest(destination, chunking_payload)
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        print(
            f"manifest 기록 실패: {_failure_reason(exc)}",
            file=sys.stderr,
            flush=True,
        )
        return 1

    print(
        f"fixed_size={size} fixed_overlap={overlap} "
        f"structural_chunks={len(structural_chunks)} "
        f"fixed_chunks={len(fixed_chunks)} manifest={destination}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
