"""Tests for the deterministic M5 structural and fixed-size chunkers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodocs.chunking import chunkers
from kodocs.chunking.chunkers import (
    Chunk,
    chunk_fixed,
    chunk_structural,
    fixed_size_from_structural,
)
from kodocs.chunking.corpus import Access, CorpusDoc, Section, load_corpus


def _document(
    text: str,
    sections: tuple[Section, ...],
    *,
    doc_id: str = "doc-1",
    source: str = "synthetic",
    doc_type: str = "guide",
) -> CorpusDoc:
    return CorpusDoc(
        doc_id=doc_id,
        source=source,
        doc_type=doc_type,
        title="테스트 문서",
        text=text,
        sections=sections,
        access=Access("public", (), 0),
        team=None,
        date="2026-01-01",
        rule_id=None,
        is_current=True,
        superseded_by=None,
        identifiers={
            "ticket": (),
            "approval": (),
            "incident": (),
            "service": (),
            "regulation": (),
        },
    )


def _single_section_document(
    text: str,
    *,
    doc_id: str,
    source: str,
    doc_type: str,
) -> CorpusDoc:
    return _document(
        text,
        (Section("heading_section", None, ("테스트 문서",), 0, len(text)),),
        doc_id=doc_id,
        source=source,
        doc_type=doc_type,
    )


def _chunk_with_body(body: str, seq: int) -> Chunk:
    return Chunk(
        chunk_id=f"doc#structural-{seq:04d}",
        doc_id="doc",
        chunking="structural",
        seq=seq,
        prefix="",
        body=body,
        start=0,
        end=len(body),
        section_kind="article",
        section_key=f"제{seq}조",
    )


def test_structural_chunks_project_sections_verbatim_and_prefix_regulations() -> None:
    text = "색인하지 않는 머리\n제1조(목적)  원문\n\n[별표 1]  표 원문  "
    article_start = text.index("제1조")
    annex_start = text.index("[별표")
    document = _document(
        text,
        (
            Section(
                "article",
                "제1조",
                ("이사회 운영규정", "제1장 총칙", "제1조(목적)"),
                article_start,
                annex_start,
            ),
            Section(
                "annex",
                "별표1",
                ("이사회 운영규정", "[별표 1]"),
                annex_start,
                len(text),
            ),
        ),
        doc_id="law-1",
        source="real",
        doc_type="regulation",
    )

    chunks = chunk_structural(document)

    assert [chunk.chunk_id for chunk in chunks] == [
        "law-1#structural-0001",
        "law-1#structural-0002",
    ]
    assert [chunk.seq for chunk in chunks] == [1, 2]
    assert chunks[0].prefix == "이사회 운영규정 > 제1장 총칙 > 제1조(목적)"
    assert chunks[0].text == f"{chunks[0].prefix}\n{chunks[0].body}"
    assert chunks[1].section_kind == "annex"
    assert chunks[1].section_key == "별표1"
    assert text[:article_start] not in [chunk.body for chunk in chunks]
    assert all(chunk.body == text[chunk.start : chunk.end] for chunk in chunks)


def test_structural_chunks_use_explicit_preamble_and_skip_empty_section() -> None:
    text = "명시된 서문\n\n본문"
    body_start = text.index("본문")
    document = _document(
        text,
        (
            Section("preamble", None, ("테스트 문서",), 0, body_start),
            Section(
                "heading_section", None, ("테스트 문서", "본문"), body_start, len(text)
            ),
            Section(
                "heading_section",
                None,
                ("테스트 문서", "빈 구간"),
                len(text),
                len(text),
            ),
        ),
    )

    chunks = chunk_structural(document)

    assert [chunk.section_kind for chunk in chunks] == [
        "preamble",
        "heading_section",
    ]
    assert [chunk.prefix for chunk in chunks] == ["", ""]
    assert [chunk.body for chunk in chunks] == [text[:body_start], text[body_start:]]
    assert [chunk.seq for chunk in chunks] == [1, 2]


def test_structural_chunk_rejects_out_of_bounds_section() -> None:
    document = _document(
        "본문",
        (Section("heading_section", None, ("테스트",), -1, 2),),
    )

    with pytest.raises(ValueError, match="doc-1.*section range"):
        chunk_structural(document)


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[2] / "data/interim/law").is_dir(),
    reason="비커밋 로컬 원문(data/interim/law)이 없는 clone에서는 건너뜀",
)
def test_all_loaded_structural_chunk_bodies_are_exact_document_slices() -> None:
    documents, _ = load_corpus("composite")

    chunks = [chunk for document in documents for chunk in chunk_structural(document)]

    assert chunks
    by_id = {document.doc_id: document for document in documents}
    assert all(
        chunk.body == by_id[chunk.doc_id].text[chunk.start : chunk.end]
        for chunk in chunks
    )


@pytest.mark.parametrize(
    ("text", "expected_spans"),
    [
        ("가나다라", [(0, 4)]),
        ("0123456789", [(0, 10)]),
        ("0123456789abcdefghijklmno", [(0, 10), (8, 18), (15, 25)]),
    ],
)
def test_fixed_chunks_slide_without_boundary_alignment_and_end_at_document_end(
    text: str, expected_spans: list[tuple[int, int]]
) -> None:
    document = _document(text, ())

    chunks = chunk_fixed(document, size=10, overlap=2)

    assert [(chunk.start, chunk.end) for chunk in chunks] == expected_spans
    assert [chunk.body for chunk in chunks] == [
        text[start:end] for start, end in expected_spans
    ]
    assert [chunk.chunk_id for chunk in chunks] == [
        f"doc-1#fixed-{seq:04d}" for seq in range(1, len(chunks) + 1)
    ]
    assert all(chunk.prefix == "" for chunk in chunks)
    assert all(chunk.section_kind is None for chunk in chunks)
    assert all(chunk.section_key is None for chunk in chunks)


def test_fixed_chunks_do_not_emit_empty_chunks() -> None:
    assert chunk_fixed(_document("", ()), size=10, overlap=2) == []


@pytest.mark.parametrize(
    ("size", "overlap"),
    [(0, 0), (-1, 0), (10, -1), (10, 10), (10, 11)],
)
def test_fixed_chunks_reject_invalid_window_settings(size: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        chunk_fixed(_document("본문", ()), size=size, overlap=overlap)


def test_fixed_size_uses_character_median_with_half_up_rounding() -> None:
    chunks = [
        _chunk_with_body("가" * length, seq)
        for seq, length in enumerate((50, 100, 150, 200), start=1)
    ]

    assert fixed_size_from_structural(chunks) == (150, 30)


def test_fixed_size_has_one_unit_floor_and_rejects_an_empty_corpus() -> None:
    assert fixed_size_from_structural([_chunk_with_body("가", 1)]) == (50, 10)
    with pytest.raises(ValueError, match="structural chunks"):
        fixed_size_from_structural([])


def test_cli_merges_chunking_statistics_into_existing_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = tmp_path / "data/manifest/m5_index.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "index_build": {"preserved": True},
                "chunking": {"future_field": "preserved"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    documents = [
        _single_section_document(
            "가" * 100,
            doc_id="syn-1",
            source="synthetic",
            doc_type="meeting",
        ),
        _document(
            "나" * 150,
            (
                Section("article", "제1조", ("규정", "제1조"), 0, 50),
                Section("article", "제2조", ("규정", "제2조"), 50, 150),
            ),
            doc_id="law-1",
            source="real",
            doc_type="regulation",
        ),
    ]
    monkeypatch.setattr(
        chunkers, "load_corpus", lambda *_args, **_kwargs: (documents, object())
    )
    monkeypatch.setattr(chunkers, "now_iso", lambda: "2026-09-30T12:34:56+09:00")

    assert chunkers.main(["--root", str(tmp_path)]) == 0

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["index_build"] == {"preserved": True}
    assert manifest["chunking"] == {
        "calculated_at": "2026-09-30T12:34:56+09:00",
        "composite_corpus_document_count": 2,
        "fixed_chunk_count": 3,
        "fixed_overlap": 20,
        "fixed_size": 100,
        "future_field": "preserved",
        "structural_body_length_chars": {
            "max": 100,
            "median": 100,
            "min": 50,
            "p90": 100,
        },
        "structural_chunk_count": 3,
        "structural_chunk_count_by_document_type": {
            "meeting": 1,
            "regulation": 2,
        },
        "structural_chunk_count_by_source": {"real": 2, "synthetic": 1},
    }


def test_cli_continues_after_document_chunking_failure_and_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    documents = [
        _single_section_document(
            "실패",
            doc_id="bad-doc",
            source="synthetic",
            doc_type="guide",
        ),
        _single_section_document(
            "성공",
            doc_id="good-doc",
            source="synthetic",
            doc_type="guide",
        ),
    ]
    original = chunkers.chunk_structural
    visited: list[str] = []

    def fail_one(document: CorpusDoc) -> list[Chunk]:
        visited.append(document.doc_id)
        if document.doc_id == "bad-doc":
            raise RuntimeError("의도한 실패")
        return original(document)

    monkeypatch.setattr(
        chunkers, "load_corpus", lambda *_args, **_kwargs: (documents, object())
    )
    monkeypatch.setattr(chunkers, "chunk_structural", fail_one)

    assert chunkers.main(["--root", str(tmp_path)]) == 1
    assert visited == ["bad-doc", "good-doc"]
    assert "bad-doc" in capsys.readouterr().err
    assert not (tmp_path / "data/manifest/m5_index.json").exists()
