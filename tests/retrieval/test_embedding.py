"""Unit tests for local deterministic and BGE-M3 embedding adapters."""

from __future__ import annotations

import builtins
import hashlib
import importlib
import json
import math
import sys
import warnings
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from kodocs.retrieval import embedding
from kodocs.retrieval.embedding import (
    Bge3Embedder,
    CachedEmbedder,
    FakeEmbedder,
    embedder_settings_hash,
)


class RecordingEmbedder(FakeEmbedder):
    """Fake embedder that exposes calls made through the cache wrapper."""

    def __init__(
        self,
        *,
        model_id: str = "recording",
        revision: str = "rev-1",
    ) -> None:
        super().__init__(dim=8, model_id=model_id, revision=revision)
        self.calls: list[tuple[tuple[str, ...], str]] = []

    def embed(
        self,
        texts: list[str] | tuple[str, ...],
        *,
        kind: str,
    ) -> list[list[float]]:
        self.calls.append((tuple(texts), kind))
        return super().embed(texts, kind=kind)  # type: ignore[arg-type]


class FakeTokenizer:
    def encode(
        self,
        text: str,
        *,
        add_special_tokens: bool,
        truncation: bool,
    ) -> list[int]:
        assert add_special_tokens is True
        assert truncation is False
        return list(range(len(text) + 2))


class FakeSentenceTransformer:
    def __init__(self) -> None:
        self.max_seq_length = 1
        self.tokenizer = FakeTokenizer()
        self.batches: list[tuple[str, ...]] = []

    def get_sentence_embedding_dimension(self) -> int:
        return 1024

    def encode(self, texts: list[str], **kwargs: object) -> list[list[float]]:
        assert kwargs["normalize_embeddings"] is True
        assert kwargs["convert_to_numpy"] is True
        assert kwargs["show_progress_bar"] is False
        self.batches.append(tuple(texts))
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * 1024
            vector[len(text)] = 3.0
            vector[-1] = 4.0
            vectors.append(vector)
        return vectors


class NewDimensionOnlyModel:
    def get_embedding_dimension(self) -> int:
        return 1024


class LegacyDimensionOnlyModel:
    def get_sentence_embedding_dimension(self) -> int:
        return 1024


class BothDimensionMethodsModel:
    def __init__(self) -> None:
        self.legacy_called = False

    def get_embedding_dimension(self) -> int:
        return 1024

    def get_sentence_embedding_dimension(self) -> int:
        self.legacy_called = True
        warnings.warn("legacy dimension method called", FutureWarning, stacklevel=2)
        return 1


def _snapshot(tmp_path: Path, *, max_seq_length: int = 12) -> tuple[Path, str]:
    revision = "a" * 40
    path = tmp_path / "models--BAAI--bge-m3" / "snapshots" / revision
    path.mkdir(parents=True)
    (path / "sentence_bert_config.json").write_text(
        json.dumps({"max_seq_length": max_seq_length}),
        encoding="utf-8",
    )
    (path / "config.json").write_text(
        json.dumps({"max_position_embeddings": max_seq_length + 2}),
        encoding="utf-8",
    )
    return path, revision


def _install_fake_optional_modules(
    monkeypatch: pytest.MonkeyPatch,
    model: FakeSentenceTransformer,
) -> None:
    sentence_transformers = ModuleType("sentence_transformers")

    def factory(*args: object, **kwargs: object) -> FakeSentenceTransformer:
        assert kwargs["local_files_only"] is True
        return model

    sentence_transformers.SentenceTransformer = factory  # type: ignore[attr-defined]
    torch = ModuleType("torch")
    torch.cuda = SimpleNamespace(is_available=lambda: False)  # type: ignore[attr-defined]
    torch.backends = SimpleNamespace(  # type: ignore[attr-defined]
        mps=SimpleNamespace(is_available=lambda: False)
    )
    monkeypatch.setitem(sys.modules, "sentence_transformers", sentence_transformers)
    monkeypatch.setitem(sys.modules, "torch", torch)


def test_fake_embedder_is_deterministic_normalized_and_similarity_aware() -> None:
    embedder = FakeEmbedder(dim=64, max_seq_length=7)

    first = embedder.embed(
        ["검색 품질", "검색 품질 개선", "완전히 다른 문장"], kind="passage"
    )
    second = embedder.embed(["검색 품질"], kind="query")

    assert first[0] == second[0]
    assert all(len(vector) == 64 for vector in first)
    assert all(
        math.sqrt(sum(value * value for value in vector)) == pytest.approx(1.0)
        for vector in first
    )
    similar = sum(a * b for a, b in zip(first[0], first[1], strict=True))
    different = sum(a * b for a, b in zip(first[0], first[2], strict=True))
    assert similar > different
    assert embedder.count_tokens(["", "가나다라마바사가"]) == [2, 6]
    assert embedder.settings["max_seq_length"] == 7


def test_embedder_settings_hash_matches_parser_canonical_hash_style() -> None:
    embedder = FakeEmbedder(dim=8, max_seq_length=10)
    serialized = json.dumps(
        dict(embedder.settings),
        ensure_ascii=False,
        sort_keys=True,
    )

    assert (
        embedder_settings_hash(embedder)
        == hashlib.sha256(serialized.encode()).hexdigest()[:12]
    )


def test_cache_hits_skip_inner_and_only_misses_are_embedded(tmp_path: Path) -> None:
    inner = RecordingEmbedder()
    cached = CachedEmbedder(inner, tmp_path / "embeddings")

    initial = cached.embed(["하나", "둘"], kind="passage")
    repeated = cached.embed(["둘", "하나", "셋"], kind="passage")

    assert repeated[:2] == [initial[1], initial[0]]
    assert inner.calls == [(("하나", "둘"), "passage"), (("셋",), "passage")]
    assert cached.stats == {"hits": 2, "misses": 3}


@pytest.mark.parametrize(
    ("changed", "kind"),
    [
        ({"model_id": "other"}, "passage"),
        ({"revision": "rev-2"}, "passage"),
        ({}, "query"),
    ],
)
def test_cache_key_separates_model_revision_and_kind(
    tmp_path: Path,
    changed: dict[str, str],
    kind: str,
) -> None:
    cache_dir = tmp_path / "embeddings"
    first = RecordingEmbedder()
    CachedEmbedder(first, cache_dir).embed(["같은 본문"], kind="passage")
    other = RecordingEmbedder(**changed)

    CachedEmbedder(other, cache_dir).embed(["같은 본문"], kind=kind)  # type: ignore[arg-type]

    assert len(other.calls) == 1
    assert len(list(cache_dir.glob("*.json"))) == 2


def test_cache_writes_vectors_atomically_without_leftover_temp_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_dir = tmp_path / "embeddings"
    replacements: list[tuple[Path, Path]] = []
    real_replace = embedding.os.replace

    def recording_replace(source: str | Path, target: str | Path) -> None:
        replacements.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(embedding.os, "replace", recording_replace)

    CachedEmbedder(RecordingEmbedder(), cache_dir).embed(
        ["원자적 저장"], kind="passage"
    )

    assert len(replacements) == 1
    source, target = replacements[0]
    assert source.parent == target.parent == cache_dir
    assert target.suffix == ".json"
    assert not source.exists()
    assert [path.suffix for path in cache_dir.iterdir()] == [".json"]
    assert isinstance(json.loads(target.read_text(encoding="utf-8")), list)


def test_module_import_does_not_import_optional_embedding_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def guarded_import(name: str, *args: object, **kwargs: object) -> object:
        if name.split(".", 1)[0] in {"sentence_transformers", "torch"}:
            raise AssertionError(f"eager optional import: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    importlib.reload(embedding)


def test_prepare_reports_missing_embed_group(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def missing_sentence_transformers(
        name: str, *args: object, **kwargs: object
    ) -> object:
        if name == "sentence_transformers":
            raise ImportError("not installed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_sentence_transformers)

    with pytest.raises(RuntimeError, match=r"uv sync --group embed.*필요"):
        Bge3Embedder().prepare()


def test_prepare_reads_snapshot_limit_revision_and_records_device(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, revision = _snapshot(tmp_path, max_seq_length=12)
    model = FakeSentenceTransformer()
    _install_fake_optional_modules(monkeypatch, model)
    embedder = Bge3Embedder(model_path_or_id=str(snapshot), max_seq_length=10)

    init_s = embedder.prepare()

    assert init_s >= 0.0
    assert embedder.prepare() == init_s
    assert model.max_seq_length == 10
    assert embedder.revision == revision
    assert embedder.settings["revision"] == revision
    assert embedder.settings["device"] == "cpu"
    assert embedder.settings["max_seq_length"] == 10
    assert embedder.settings["normalize"] is True


def test_embedding_dimension_uses_new_method_when_it_is_the_only_method() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        actual = embedding._sentence_embedding_dimension(NewDimensionOnlyModel())

    assert actual == 1024


def test_embedding_dimension_falls_back_to_legacy_method_without_warning() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        actual = embedding._sentence_embedding_dimension(LegacyDimensionOnlyModel())

    assert actual == 1024


def test_embedding_dimension_prefers_new_method_without_calling_legacy() -> None:
    model = BothDimensionMethodsModel()

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        actual = embedding._sentence_embedding_dimension(model)

    assert actual == 1024
    assert model.legacy_called is False


def test_prepare_uses_supported_max_and_rejects_missing_or_excess_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, _revision = _snapshot(tmp_path / "valid", max_seq_length=12)
    model = FakeSentenceTransformer()
    _install_fake_optional_modules(monkeypatch, model)
    embedder = Bge3Embedder(model_path_or_id=str(snapshot))
    embedder.prepare()
    assert model.max_seq_length == 12

    missing = tmp_path / "missing" / "snapshots" / ("b" * 40)
    missing.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="max_seq_length"):
        Bge3Embedder(model_path_or_id=str(missing)).prepare()

    with pytest.raises(ValueError, match="지원 최대값 12"):
        Bge3Embedder(
            model_path_or_id=str(snapshot),
            max_seq_length=13,
        ).prepare()


def test_prepare_rejects_snapshot_without_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "sentence_bert_config.json").write_text(
        json.dumps({"max_seq_length": 12}),
        encoding="utf-8",
    )
    _install_fake_optional_modules(monkeypatch, FakeSentenceTransformer())

    with pytest.raises(RuntimeError, match="revision"):
        Bge3Embedder(model_path_or_id=str(tmp_path)).prepare()


def test_bge_batches_longest_first_respects_token_budget_and_restores_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, _revision = _snapshot(tmp_path, max_seq_length=20)
    model = FakeSentenceTransformer()
    _install_fake_optional_modules(monkeypatch, model)
    embedder = Bge3Embedder(
        model_path_or_id=str(snapshot),
        batch_size=3,
        max_batch_tokens=12,
    )
    embedder.prepare()
    texts = ["a", "bbbbbb", "ccc", "ddddd"]

    vectors = embedder.embed(texts, kind="passage")

    flattened = [text for batch in model.batches for text in batch]
    assert flattened == ["bbbbbb", "ddddd", "ccc", "a"]
    assert all(
        len(batch) == 1 or max(len(text) + 2 for text in batch) * len(batch) <= 12
        for batch in model.batches
    )
    assert [vector.index(0.6) for vector in vectors] == [1, 6, 3, 5]
    assert all(
        math.sqrt(sum(value * value for value in vector)) == pytest.approx(1.0)
        for vector in vectors
    )


def test_count_tokens_exposes_over_limit_text_without_truncating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot, _revision = _snapshot(tmp_path, max_seq_length=5)
    _install_fake_optional_modules(monkeypatch, FakeSentenceTransformer())
    embedder = Bge3Embedder(model_path_or_id=str(snapshot))
    embedder.prepare()

    counts = embedder.count_tokens(["가", "가나다라"])

    assert counts == [3, 6]
    assert counts[1] > embedder.settings["max_seq_length"]
    with pytest.raises(ValueError, match="max_seq_length"):
        embedder.embed(["가나다라"], kind="query")


def _fake_hub(monkeypatch: pytest.MonkeyPatch, fake) -> None:
    import sys
    import types

    monkeypatch.setitem(
        sys.modules, "huggingface_hub", types.SimpleNamespace(snapshot_download=fake)
    )


def test_resolve_local_snapshot_pins_revision_and_patterns_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kodocs.retrieval import embedding

    calls: list[dict] = []

    def fake_snapshot_download(**kwargs):
        calls.append(kwargs)
        return str(tmp_path)

    _fake_hub(monkeypatch, fake_snapshot_download)

    assert embedding._resolve_local_snapshot("BAAI/bge-m3") == tmp_path.resolve()
    assert calls == [
        {
            "repo_id": "BAAI/bge-m3",
            "revision": embedding.BGE_M3_REVISION,
            "allow_patterns": list(embedding.BGE_M3_ALLOW_PATTERNS),
            "ignore_patterns": list(embedding.BGE_M3_IGNORE_PATTERNS),
            "local_files_only": True,
        }
    ]
    assert embedding.BGE_M3_REVISION == "5617a9f61b028005a4858fdac845db406aefb181"
    assert list(embedding.BGE_M3_ALLOW_PATTERNS) == [
        "*.json",
        "*.model",
        "*.pt",
        "pytorch_model.bin",
        "1_Pooling/*",
    ]
    # `*.json`·`*.model`은 onnx/ 하위 파일에도 맞으므로 제외 패턴이 함께 필요하다
    assert list(embedding.BGE_M3_IGNORE_PATTERNS) == ["onnx/*"]


def test_resolve_local_snapshot_failure_keeps_original_cause_in_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kodocs.retrieval import embedding

    def failing(**_kwargs):
        raise OSError("IncompleteSnapshotError: 18 files missing")

    _fake_hub(monkeypatch, failing)

    with pytest.raises(RuntimeError) as excinfo:
        embedding._resolve_local_snapshot("BAAI/bge-m3")
    assert "IncompleteSnapshotError: 18 files missing" in str(excinfo.value)
    assert "로컬 BGE-M3 스냅샷을 찾을 수 없습니다" in str(excinfo.value)
