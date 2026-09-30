"""Unit tests for the deterministic M5 corpus-to-OpenSearch builder."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest

from kodocs.chunking.corpus import Access, CorpusDoc, Section
from kodocs.retrieval import index_build
from kodocs.retrieval.embedding import Bge3Embedder, FakeEmbedder
from kodocs.retrieval.index_build import IndexSpec, build_index, m5_index_specs
from kodocs.retrieval.opensearch import OpenSearchClient


class RecordingEmbedder(FakeEmbedder):
    """Fake embedder exposing whether vector generation was attempted."""

    def __init__(self, *, max_seq_length: int = 1000) -> None:
        super().__init__(dim=8, max_seq_length=max_seq_length)
        self.embedded: list[str] = []

    def embed(self, texts: Sequence[str], *, kind: str) -> list[list[float]]:
        self.embedded.extend(texts)
        return super().embed(texts, kind=kind)  # type: ignore[arg-type]

    def embed_allowing_truncation(
        self, texts: Sequence[str], *, kind: str
    ) -> list[list[float]]:
        self.embedded.extend(texts)
        return super().embed_allowing_truncation(texts, kind=kind)  # type: ignore[arg-type]


class SelectiveFailureEmbedder(RecordingEmbedder):
    def embed(self, texts: Sequence[str], *, kind: str) -> list[list[float]]:
        if any("FAIL" in text for text in texts):
            raise RuntimeError("selected embedding failure")
        return super().embed(texts, kind=kind)


class MockOpenSearch:
    """Small stateful OpenSearch HTTP surface used by unit tests."""

    def __init__(
        self,
        *,
        failed_bulk_ids: frozenset[str] = frozenset(),
        count_override: int | None = None,
    ) -> None:
        self.failed_bulk_ids = failed_bulk_ids
        self.count_override = count_override
        self.requests: list[httpx.Request] = []
        self.indexed: dict[str, dict[str, object]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "DELETE":
            self.indexed.clear()
            return httpx.Response(200, json={"acknowledged": True})
        if request.method == "PUT":
            return httpx.Response(200, json={"acknowledged": True})
        if path == "/_bulk":
            lines = [
                json.loads(line)
                for line in request.content.decode().splitlines()
                if line
            ]
            items: list[dict[str, object]] = []
            for offset in range(0, len(lines), 2):
                doc_id = lines[offset]["index"]["_id"]
                if doc_id in self.failed_bulk_ids:
                    items.append(
                        {
                            "index": {
                                "_id": doc_id,
                                "status": 400,
                                "error": {"type": "mapper_parsing_exception"},
                            }
                        }
                    )
                else:
                    self.indexed[doc_id] = lines[offset + 1]
                    items.append({"index": {"_id": doc_id, "status": 201}})
            return httpx.Response(
                200,
                json={"took": 1, "errors": bool(self.failed_bulk_ids), "items": items},
            )
        if path.endswith("/_refresh"):
            return httpx.Response(200, json={"_shards": {"failed": 0}})
        if path.endswith("/_count"):
            count = (
                self.count_override
                if self.count_override is not None
                else len(self.indexed)
            )
            return httpx.Response(200, json={"count": count})
        raise AssertionError(f"unexpected request: {request.method} {path}")


def _client(server: MockOpenSearch) -> OpenSearchClient:
    return OpenSearchClient(
        "http://opensearch.test",
        client=httpx.Client(transport=httpx.MockTransport(server.handler)),
    )


def _doc(
    doc_id: str,
    text: str,
    *,
    source: str = "synthetic",
    doc_type: str = "meeting",
    title: str = "테스트 문서",
    sections: tuple[Section, ...] | None = None,
    identifiers: dict[str, tuple[str, ...]] | None = None,
    current: bool = True,
) -> CorpusDoc:
    return CorpusDoc(
        doc_id=doc_id,
        source=source,
        doc_type=doc_type,
        title=title,
        text=text,
        sections=sections
        or (Section("heading_section", None, (title,), 0, len(text)),),
        access=Access("team", ("platform",), 2),
        team="platform" if source == "synthetic" else None,
        date="2026-09-30",
        rule_id="rule-1" if doc_type == "regulation" else None,
        is_current=current,
        superseded_by=None,
        identifiers=identifiers
        or {
            "ticket": (),
            "approval": (),
            "incident": (),
            "service": (),
            "regulation": (),
        },
    )


def _install_corpus(
    monkeypatch: pytest.MonkeyPatch,
    documents: Sequence[CorpusDoc],
    *,
    fallback: int = 0,
) -> None:
    report = SimpleNamespace(composite_annex_fallback_count=fallback)
    monkeypatch.setattr(
        index_build,
        "load_corpus",
        lambda condition, *, root: (list(documents), report),
    )


def _manifest_root(tmp_path: Path) -> Path:
    path = tmp_path / "data/manifest/m5_index.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"chunking": {"fixed_size": 8, "fixed_overlap": 2}}),
        encoding="utf-8",
    )
    return tmp_path


def _manifest(root: Path) -> dict[str, object]:
    return json.loads((root / "data/manifest/m5_index.json").read_text())


def test_m5_index_specs_are_the_four_preregistered_combinations() -> None:
    assert m5_index_specs() == [
        IndexSpec("composite", "fixed"),
        IndexSpec("composite", "structural"),
        IndexSpec("pypdf", "structural"),
        IndexSpec("upstage", "structural"),
    ]


def test_gold_answer_over_limit_stops_before_opensearch_and_embedding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _manifest_root(tmp_path)
    document = _doc("syn-long", "가나다라마바사아자차")
    _install_corpus(monkeypatch, [document])
    queries = root / "data/golden/queries"
    queries.mkdir(parents=True)
    (queries / "gold.jsonl").write_text(
        json.dumps(
            {
                "id": "g-over-limit",
                "qrels": [{"doc_id": "syn-long", "start": 0, "end": 1, "rel": 2}],
            }
        ),
        encoding="utf-8",
    )
    server = MockOpenSearch()
    embedder = RecordingEmbedder(max_seq_length=4)

    report = build_index(
        IndexSpec("composite", "structural"),
        client=_client(server),
        embedder=embedder,
        root=root,
        answer_ranges_by_set={"gold": {"syn-long": [(0, 1)]}, "silver": {}},
    )

    assert report.success is False
    assert report.gold_answer_chunks_over_limit == 1
    assert embedder.embedded == []
    assert server.requests == []
    manifest = _manifest(root)
    assert manifest["chunking"]["fixed_size"] == 8
    entry = manifest["indexes"][report.index_name]
    assert entry["status"] == "failed_gold_answer_over_limit"
    assert entry["over_limit_chunk_count"] == 1
    assert entry["over_limit_chunk_ids"] == ["syn-long#structural-0001"]
    assert entry["gold_answer_chunks_over_limit"] == 1
    assert entry["gold_answer_over_limit_chunks"][0]["doc_id"] == "syn-long"
    assert entry["gold_answer_over_limit_chunks"][0]["query_ids"] == ["g-over-limit"]
    assert "text_std" not in json.dumps(entry)


@pytest.mark.parametrize(
    ("ranges", "expected_silver"),
    [
        ({"gold": {}, "silver": {"syn-long": [(0, 2)]}}, 1),
        ({"gold": {"syn-long": [(50, 60)]}, "silver": {}}, 0),
    ],
)
def test_non_gold_over_limit_is_recorded_and_indexing_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ranges: dict[str, dict[str, list[tuple[int, int]]]],
    expected_silver: int,
) -> None:
    root = _manifest_root(tmp_path)
    _install_corpus(monkeypatch, [_doc("syn-long", "가나다라마바사아자차")])
    server = MockOpenSearch()
    embedder = RecordingEmbedder(max_seq_length=4)

    report = build_index(
        IndexSpec("composite", "structural"),
        client=_client(server),
        embedder=embedder,
        root=root,
        answer_ranges_by_set=ranges,
    )

    assert report.success is True
    assert report.over_limit_chunk_count == 1
    assert report.silver_answer_chunks_over_limit == expected_silver
    assert embedder.embedded
    entry = _manifest(root)["indexes"][report.index_name]
    assert entry["over_limit_chunk_count"] == 1
    assert entry["silver_answer_chunks_over_limit"] == expected_silver


def test_index_documents_use_chunk_fields_and_chunk_local_identifiers_and_stats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _manifest_root(tmp_path)
    first = "PAY-1234 feature-store 출장규칙 제8조"
    second = "EXP-2026-0001 INC-2026-001"
    synthetic_text = f"{first}\n{second}"
    synthetic = _doc(
        "syn-fields",
        synthetic_text,
        sections=(
            Section("agenda", "a", ("회의",), 0, len(first)),
            Section("agenda", "b", ("회의",), len(first) + 1, len(synthetic_text)),
        ),
        identifiers={
            "ticket": ("PAY-1234",),
            "approval": ("EXP-2026-0001",),
            "incident": ("INC-2026-001",),
            "service": ("feature-store",),
            "regulation": ("출장규칙제8조",),
        },
    )
    regulation = _doc(
        "law-1",
        "제8조 본문",
        source="real",
        doc_type="regulation",
        title="출장규칙",
        sections=(Section("article", "제8조", ("출장규칙", "제8조"), 0, 6),),
        identifiers={
            "ticket": (),
            "approval": (),
            "incident": (),
            "service": (),
            "regulation": ("출장규칙제8조",),
        },
    )
    _install_corpus(monkeypatch, [synthetic, regulation], fallback=3)
    server = MockOpenSearch()

    report = build_index(
        IndexSpec("composite", "structural"),
        client=_client(server),
        embedder=RecordingEmbedder(),
        root=root,
        answer_ranges_by_set={"gold": {}, "silver": {}},
    )

    assert report.success is True
    assert set(server.indexed) == {
        "syn-fields#structural-0001",
        "syn-fields#structural-0002",
        "law-1#structural-0001",
    }
    first_source = server.indexed["syn-fields#structural-0001"]
    assert first_source["text_std"] == first_source["text_nori"] == first
    assert first_source["chunk_id"] == "syn-fields#structural-0001"
    assert first_source["ids_ticket"] == ["PAY-1234"]
    assert first_source["ids_approval"] == []
    assert first_source["ids_incident"] == []
    assert first_source["ids_service"] == ["feature-store"]
    assert first_source["ids_regulation"] == ["출장규칙제8조"]
    assert first_source["access_level"] == "team"
    assert first_source["access_teams"] == ["platform"]
    assert first_source["access_min_grade"] == 2
    regulation_source = server.indexed["law-1#structural-0001"]
    assert regulation_source["text_std"] == "출장규칙 > 제8조\n제8조 본문"
    assert regulation_source["ids_regulation"] == ["출장규칙제8조"]

    entry = _manifest(root)["indexes"][report.index_name]
    assert entry["document_counts"] == {
        "real_current": 1,
        "real_prior": 0,
        "synthetic": 1,
        "total": 2,
    }
    assert entry["annex_fallback_count"] == 3
    assert entry["chunk_length_chars_by_source"]["synthetic"] == {
        "count": 2,
        "median": (len(first) + len(second)) / 2,
    }
    assert entry["chunk_length_chars_by_doc_type"]["regulation"] == {
        "count": 1,
        "median": 6,
    }
    assert len(entry["index_field_schema_hash"]) == 64


def test_recreate_is_idempotent_with_same_chunk_ids_and_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _manifest_root(tmp_path)
    _install_corpus(monkeypatch, [_doc("syn-repeat", "같은 입력")])
    server = MockOpenSearch()
    client = _client(server)
    spec = IndexSpec("composite", "structural")

    first = build_index(
        spec,
        client=client,
        embedder=RecordingEmbedder(),
        root=root,
        answer_ranges_by_set={"gold": {}, "silver": {}},
    )
    first_ids = set(server.indexed)
    second = build_index(
        spec,
        client=client,
        embedder=RecordingEmbedder(),
        root=root,
        answer_ranges_by_set={"gold": {}, "silver": {}},
    )

    assert first.success and second.success
    assert first.indexed_count == second.indexed_count == 1
    assert set(server.indexed) == first_ids == {"syn-repeat#structural-0001"}


@pytest.mark.parametrize("failure_kind", ["embedding", "bulk"])
def test_item_failure_continues_but_returns_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    root = _manifest_root(tmp_path)
    documents = [_doc("syn-ok", "정상"), _doc("syn-bad", "FAIL")]
    _install_corpus(monkeypatch, documents)
    bad_id = "syn-bad#structural-0001"
    server = MockOpenSearch(
        failed_bulk_ids=frozenset({bad_id}) if failure_kind == "bulk" else frozenset()
    )
    embedder = (
        SelectiveFailureEmbedder()
        if failure_kind == "embedding"
        else RecordingEmbedder()
    )

    report = build_index(
        IndexSpec("composite", "structural"),
        client=_client(server),
        embedder=embedder,
        root=root,
        answer_ranges_by_set={"gold": {}, "silver": {}},
    )

    assert report.success is False
    assert report.indexed_count == 1
    assert "syn-ok#structural-0001" in server.indexed
    assert any(failure.chunk_id == bad_id for failure in report.failures)
    entry = _manifest(root)["indexes"][report.index_name]
    assert entry["failure_count"] >= 1
    assert entry["status"] == "failed"


def test_zero_chunks_fails_without_creating_an_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _manifest_root(tmp_path)
    _install_corpus(monkeypatch, [_doc("syn-empty", "   ")])
    server = MockOpenSearch()

    report = build_index(
        IndexSpec("composite", "structural"),
        client=_client(server),
        embedder=RecordingEmbedder(),
        root=root,
        answer_ranges_by_set={"gold": {}, "silver": {}},
    )

    assert report.success is False
    assert report.chunk_count == report.indexed_count == 0
    assert server.requests == []
    assert _manifest(root)["indexes"][report.index_name]["failure_count"] == 1


def test_count_mismatch_is_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _manifest_root(tmp_path)
    _install_corpus(monkeypatch, [_doc("syn-count", "본문")])
    report = build_index(
        IndexSpec("composite", "structural"),
        client=_client(MockOpenSearch(count_override=0)),
        embedder=RecordingEmbedder(),
        root=root,
        answer_ranges_by_set={"gold": {}, "silver": {}},
    )

    assert report.success is False
    assert any(failure.stage == "count" for failure in report.failures)


def test_dry_run_never_constructs_or_calls_opensearch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _manifest_root(tmp_path)
    _install_corpus(monkeypatch, [_doc("syn-dry", "dry run 본문")])
    monkeypatch.setattr(
        index_build,
        "_load_answer_ranges",
        lambda condition, root: {"gold": {}, "silver": {}},
    )

    class ForbiddenClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("dry-run constructed OpenSearch client")

    monkeypatch.setattr(index_build, "OpenSearchClient", ForbiddenClient)

    result = index_build.main(
        [
            "--condition",
            "composite",
            "--chunking",
            "structural",
            "--dry-run",
            "--embedder",
            "fake",
            "--root",
            str(root),
        ]
    )

    assert result == 0
    output = capsys.readouterr().out
    assert "dry-run" in output
    assert '"chunks": 1' in output
    assert "chunk_length_chars_by_source" in output


def test_bge_dry_run_preparation_loads_only_the_cached_tokenizer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = "a" * 40
    snapshot = tmp_path / "models--BAAI--bge-m3" / "snapshots" / revision
    snapshot.mkdir(parents=True)
    (snapshot / "sentence_bert_config.json").write_text(
        json.dumps({"max_seq_length": 12}), encoding="utf-8"
    )
    (snapshot / "config.json").write_text(
        json.dumps({"max_position_embeddings": 16}), encoding="utf-8"
    )
    calls: list[tuple[str, bool]] = []

    class Tokenizer:
        def encode(
            self, text: str, *, add_special_tokens: bool, truncation: bool
        ) -> list[int]:
            assert add_special_tokens is True
            assert truncation is False
            return list(range(len(text) + 2))

    class AutoTokenizer:
        @staticmethod
        def from_pretrained(path: str, *, local_files_only: bool) -> Tokenizer:
            calls.append((path, local_files_only))
            return Tokenizer()

    transformers = ModuleType("transformers")
    transformers.AutoTokenizer = AutoTokenizer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    embedder = Bge3Embedder(model_path_or_id=str(snapshot))

    init_s = embedder.prepare_tokenizer()

    assert init_s >= 0.0
    assert calls == [(str(snapshot.resolve()), True)]
    assert embedder.count_tokens(["가나다"]) == [5]
    assert embedder.settings["device"] == "tokenizer-only"
    assert embedder.settings["revision"] == revision


def test_index_name_override_isolates_all_requests_from_the_real_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """통합 테스트가 실측 인덱스를 지운 사고(FP-046)의 회귀 방지."""
    root = _manifest_root(tmp_path)
    _install_corpus(monkeypatch, [_doc("syn-iso", "가나다라마바사아자차")])
    server = MockOpenSearch()
    override = "kodocs-m5-integration-abc123"

    report = build_index(
        IndexSpec("composite", "structural"),
        client=_client(server),
        embedder=RecordingEmbedder(max_seq_length=64),
        root=root,
        index_name_override=override,
    )

    assert report.index_name == override
    requests = [
        f"{request.method} {request.url.path} {request.content.decode()}"
        for request in server.requests
    ]
    assert any(f"/{override}" in line for line in requests)
    assert not any("kodocs-m5-composite-structural" in line for line in requests)
    assert override in _manifest(root)["indexes"]
    assert "kodocs-m5-composite-structural" not in _manifest(root)["indexes"]
