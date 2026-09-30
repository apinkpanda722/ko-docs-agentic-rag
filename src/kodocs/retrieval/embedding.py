"""Local embedding adapters and a content-addressed on-disk vector cache.

Optional embedding packages are imported only by :meth:`Bge3Embedder.prepare`,
so importing this module remains safe in the default development environment.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol

EmbeddingKind = Literal["passage", "query"]

BGE_M3_MODEL_ID = "BAAI/bge-m3"
# 조회(_resolve_local_snapshot)와 다운로드(make embed-models)가 같은 값을 쓴다.
# 패턴 없이 오프라인 조회하면 huggingface_hub가 저장소 전체 파일을 요구해
# 부분 다운로드 스냅샷(onnx·README 제외)을 불완전으로 판정한다.
BGE_M3_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
BGE_M3_ALLOW_PATTERNS: tuple[str, ...] = (
    "*.json",
    "*.model",
    "*.pt",
    "pytorch_model.bin",
    "1_Pooling/*",
)
# `*.json`·`*.model`은 onnx/ 하위 파일에도 맞으므로(fnmatch의 `*`는 `/`도 포함) 제외한다.
BGE_M3_IGNORE_PATTERNS: tuple[str, ...] = ("onnx/*",)
_BGE_MODEL_ID = BGE_M3_MODEL_ID
_BGE_DIM = 1024
_CACHE_PROGRESS_EVERY = 100
_REVISION_RE = re.compile(r"[0-9a-f]{40,64}")
_MISSING_EMBED_GROUP = "임베딩 의존성이 없습니다: `uv sync --group embed` 필요"


class Embedder(Protocol):
    """Common interface for deterministic local embedding implementations."""

    model_id: str
    revision: str
    dim: int
    settings: Mapping[str, object]

    def count_tokens(self, texts: Sequence[str]) -> list[int]:
        """Count tokenizer tokens for each text, including special tokens."""

    def prepare(self) -> float:
        """Perform one-time initialization and return its elapsed seconds."""

    def embed(
        self,
        texts: Sequence[str],
        *,
        kind: EmbeddingKind,
    ) -> list[list[float]]:
        """Embed texts without changing their input order."""

    def embed_allowing_truncation(
        self,
        texts: Sequence[str],
        *,
        kind: EmbeddingKind,
    ) -> list[list[float]]:
        """Embed texts after a caller has explicitly audited token overflows."""


class FakeEmbedder:
    """Dependency-free deterministic character n-gram embedder for tests."""

    def __init__(
        self,
        dim: int = _BGE_DIM,
        *,
        max_seq_length: int = 8192,
        model_id: str = "fake-char-ngram",
        revision: str = "1",
    ) -> None:
        _require_positive_int(dim, label="dim")
        _require_positive_int(max_seq_length, label="max_seq_length")
        if not model_id:
            raise ValueError("model_id must not be empty")
        if not revision:
            raise ValueError("revision must not be empty")
        self.model_id = model_id
        self.revision = revision
        self.dim = dim
        self.settings: Mapping[str, object] = MappingProxyType(
            {
                "device": "cpu",
                "dim": dim,
                "max_seq_length": max_seq_length,
                "model_id": model_id,
                "ngram_sizes": [2, 3],
                "normalize": True,
                "revision": revision,
            }
        )

    def count_tokens(self, texts: Sequence[str]) -> list[int]:
        """Return a stable character-length approximation including 2 specials."""
        values = _validated_texts(texts)
        return [len(text) // 2 + 2 for text in values]

    def prepare(self) -> float:
        """Return zero because the fake embedder has no initialization work."""
        return 0.0

    def embed(
        self,
        texts: Sequence[str],
        *,
        kind: EmbeddingKind,
    ) -> list[list[float]]:
        """Hash character bigrams and trigrams into normalized dense vectors."""
        _validate_kind(kind)
        values = _validated_texts(texts)
        _raise_for_over_limit(
            self.count_tokens(values),
            max_seq_length=int(self.settings["max_seq_length"]),
        )
        return self._embed_values(values)

    def embed_allowing_truncation(
        self,
        texts: Sequence[str],
        *,
        kind: EmbeddingKind,
    ) -> list[list[float]]:
        """Truncate deterministically after the index builder records overflow."""
        _validate_kind(kind)
        values = _validated_texts(texts)
        max_seq_length = int(self.settings["max_seq_length"])
        max_characters = max(0, 2 * (max_seq_length - 2) + 1)
        return self._embed_values([text[:max_characters] for text in values])

    def _embed_values(self, values: Sequence[str]) -> list[list[float]]:
        """Hash already-validated values without applying a token-limit policy."""
        vectors: list[list[float]] = []
        for text in values:
            vector = [0.0] * self.dim
            padded = f"^{text}$"
            ngrams = [
                padded[index : index + size]
                for size in (2, 3)
                for index in range(max(0, len(padded) - size + 1))
            ]
            if not ngrams:
                ngrams = ["<empty>"]
            for ngram in ngrams:
                digest = hashlib.sha256(ngram.encode()).digest()
                bucket = int.from_bytes(digest[:8], "big") % self.dim
                vector[bucket] += 1.0
            vectors.append(_normalize_vector(vector))
        return vectors


class Bge3Embedder:
    """Lazy, offline-only dense BAAI/bge-m3 sentence-transformers adapter."""

    dim = _BGE_DIM

    def __init__(
        self,
        *,
        model_path_or_id: str = _BGE_MODEL_ID,
        device: str | None = None,
        batch_size: int = 16,
        max_batch_tokens: int = 16384,
        max_seq_length: int | None = None,
        local_files_only: bool = True,
    ) -> None:
        if not model_path_or_id:
            raise ValueError("model_path_or_id must not be empty")
        _require_positive_int(batch_size, label="batch_size")
        _require_positive_int(max_batch_tokens, label="max_batch_tokens")
        if max_seq_length is not None:
            _require_positive_int(max_seq_length, label="max_seq_length")
        if not local_files_only:
            raise ValueError(
                "local_files_only must remain True; download the model separately"
            )
        self.model_id = model_path_or_id
        self._requested_device = device
        self._batch_size = batch_size
        self._max_batch_tokens = max_batch_tokens
        self._requested_max_seq_length = max_seq_length
        self._local_files_only = local_files_only
        self._model: object | None = None
        self._tokenizer: object | None = None
        self._revision: str | None = None
        self._device: str | None = None
        self._max_seq_length: int | None = None
        self._model_max_position_embeddings: int | None = None
        self._init_s: float | None = None
        self._tokenizer_init_s: float | None = None

    @property
    def revision(self) -> str:
        """Return the local snapshot commit; preparation is required first."""
        if self._revision is None:
            raise RuntimeError("Bge3Embedder.prepare() must run before revision")
        return self._revision

    @property
    def settings(self) -> Mapping[str, object]:
        """Return fully resolved reproducibility settings after preparation."""
        if (
            self._revision is None
            or self._device is None
            or self._max_seq_length is None
        ):
            raise RuntimeError("Bge3Embedder.prepare() must run before settings")
        return MappingProxyType(
            {
                "batch_size": self._batch_size,
                "device": self._device,
                "dim": self.dim,
                "local_files_only": self._local_files_only,
                "max_batch_tokens": self._max_batch_tokens,
                "max_position_embeddings": self._model_max_position_embeddings,
                "max_seq_length": self._max_seq_length,
                "model_id": self.model_id,
                "normalize": True,
                "revision": self._revision,
            }
        )

    def prepare(self) -> float:
        """Load the local model once and return only that initialization time."""
        if self._init_s is not None:
            return self._init_s

        started = time.perf_counter()
        try:
            import torch
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(_MISSING_EMBED_GROUP) from exc

        snapshot = _resolve_local_snapshot(self.model_id)
        supported_max, position_max = _read_model_max_lengths(snapshot)
        selected_max = self._requested_max_seq_length or supported_max
        if selected_max > supported_max:
            raise ValueError(
                "max_seq_length가 모델 지원 최대값 "
                f"{supported_max}을 초과합니다: {selected_max}"
            )
        revision = _read_snapshot_revision(snapshot)
        device = self._requested_device or _select_device(torch)

        try:
            model = SentenceTransformer(
                str(snapshot),
                device=device,
                local_files_only=True,
            )
        except Exception as exc:
            raise RuntimeError(
                "로컬 BGE-M3 스냅샷을 불러오지 못했습니다; "
                "모델은 별도로 다운로드해야 합니다: "
                f"{type(exc).__name__}: {_first_line(str(exc))}"
            ) from exc
        actual_dim = model.get_sentence_embedding_dimension()
        if actual_dim != self.dim:
            raise RuntimeError(
                f"BGE-M3 dense dimension must be {self.dim}, got {actual_dim}"
            )
        tokenizer = getattr(model, "tokenizer", None)
        if tokenizer is None or not callable(getattr(tokenizer, "encode", None)):
            raise RuntimeError("BGE-M3 tokenizer is unavailable after model loading")

        model.max_seq_length = selected_max
        self._model = model
        self._tokenizer = tokenizer
        self._revision = revision
        self._device = device
        self._max_seq_length = selected_max
        self._model_max_position_embeddings = position_max
        self._init_s = time.perf_counter() - started
        return self._init_s

    def prepare_tokenizer(self) -> float:
        """Load only the cached tokenizer for an OpenSearch-free dry run."""
        if self._model is not None and self._init_s is not None:
            return self._init_s
        if self._tokenizer is not None and self._tokenizer_init_s is not None:
            return self._tokenizer_init_s

        started = time.perf_counter()
        try:
            from transformers import AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(_MISSING_EMBED_GROUP) from exc

        snapshot = _resolve_local_snapshot(self.model_id)
        supported_max, position_max = _read_model_max_lengths(snapshot)
        selected_max = self._requested_max_seq_length or supported_max
        if selected_max > supported_max:
            raise ValueError(
                "max_seq_length가 모델 지원 최대값 "
                f"{supported_max}을 초과합니다: {selected_max}"
            )
        try:
            tokenizer = AutoTokenizer.from_pretrained(
                str(snapshot),
                local_files_only=True,
            )
        except Exception as exc:
            raise RuntimeError(
                "로컬 BGE-M3 토크나이저를 불러오지 못했습니다; "
                "모델은 별도로 다운로드해야 합니다: "
                f"{type(exc).__name__}: {_first_line(str(exc))}"
            ) from exc
        if not callable(getattr(tokenizer, "encode", None)):
            raise TypeError("BGE-M3 tokenizer does not provide encode()")

        self._tokenizer = tokenizer
        self._revision = _read_snapshot_revision(snapshot)
        self._device = "tokenizer-only"
        self._max_seq_length = selected_max
        self._model_max_position_embeddings = position_max
        self._tokenizer_init_s = time.perf_counter() - started
        return self._tokenizer_init_s

    def count_tokens(self, texts: Sequence[str]) -> list[int]:
        """Count untruncated tokens after :meth:`prepare` has loaded the tokenizer.

        The tokenizer call itself does not use model weights, but this adapter's
        tokenizer is exposed by sentence-transformers, so ``prepare()`` must run
        before token counting.
        """
        values = _validated_texts(texts)
        tokenizer = self._require_tokenizer()
        return [
            len(
                tokenizer.encode(  # type: ignore[attr-defined]
                    text,
                    add_special_tokens=True,
                    truncation=False,
                )
            )
            for text in values
        ]

    def embed(
        self,
        texts: Sequence[str],
        *,
        kind: EmbeddingKind,
    ) -> list[list[float]]:
        """Embed length-sorted token-budgeted batches and restore input order."""
        _validate_kind(kind)
        values = _validated_texts(texts)
        return self._embed_values(values, allow_truncation=False)

    def embed_allowing_truncation(
        self,
        texts: Sequence[str],
        *,
        kind: EmbeddingKind,
    ) -> list[list[float]]:
        """Embed an already-audited overflow using the model's fixed truncation."""
        _validate_kind(kind)
        values = _validated_texts(texts)
        return self._embed_values(values, allow_truncation=True)

    def _embed_values(
        self,
        values: Sequence[str],
        *,
        allow_truncation: bool,
    ) -> list[list[float]]:
        if not values:
            return []
        model = self._require_model()
        if self._max_seq_length is None:
            raise RuntimeError("Bge3Embedder.prepare() must run before embed()")
        token_counts = self.count_tokens(values)
        if not allow_truncation:
            _raise_for_over_limit(token_counts, max_seq_length=self._max_seq_length)

        ordered = sorted(
            zip(range(len(values)), values, token_counts, strict=True),
            key=lambda item: item[2],
            reverse=True,
        )
        results: list[list[float] | None] = [None] * len(values)
        for batch in self._token_batches(ordered):
            batch_texts = [item[1] for item in batch]
            encoded = model.encode(  # type: ignore[attr-defined]
                batch_texts,
                batch_size=len(batch_texts),
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            if len(encoded) != len(batch):
                raise RuntimeError("BGE-M3 returned an unexpected vector count")
            for (original_index, _text, _tokens), raw_vector in zip(
                batch, encoded, strict=True
            ):
                vector = [float(value) for value in raw_vector]
                if len(vector) != self.dim:
                    raise RuntimeError(
                        "BGE-M3 returned vector dimension "
                        f"{len(vector)} instead of {self.dim}"
                    )
                results[original_index] = _normalize_vector(vector)

        if any(vector is None for vector in results):
            raise RuntimeError("BGE-M3 did not return every requested vector")
        return [vector for vector in results if vector is not None]

    def _token_batches(
        self,
        ordered: Sequence[tuple[int, str, int]],
    ) -> list[list[tuple[int, str, int]]]:
        batches: list[list[tuple[int, str, int]]] = []
        current: list[tuple[int, str, int]] = []
        current_max = 0
        for item in ordered:
            candidate_max = max(current_max, item[2])
            candidate_size = len(current) + 1
            exceeds_count = candidate_size > self._batch_size
            exceeds_tokens = candidate_max * candidate_size > self._max_batch_tokens
            if current and (exceeds_count or exceeds_tokens):
                batches.append(current)
                current = []
                current_max = 0
            current.append(item)
            current_max = max(current_max, item[2])
        if current:
            batches.append(current)
        return batches

    def _require_model(self) -> object:
        if self._model is None:
            raise RuntimeError("Bge3Embedder.prepare() must run before embed()")
        return self._model

    def _require_tokenizer(self) -> object:
        if self._tokenizer is None:
            raise RuntimeError("Bge3Embedder.prepare() must run before count_tokens()")
        return self._tokenizer


class CachedEmbedder:
    """Disk-cache an embedder using vector-determining settings as the key."""

    def __init__(self, inner: Embedder, cache_dir: Path) -> None:
        self._inner = inner
        self._cache_dir = cache_dir
        self._hits = 0
        self._misses = 0

    @property
    def model_id(self) -> str:
        return self._inner.model_id

    @property
    def revision(self) -> str:
        return self._inner.revision

    @property
    def dim(self) -> int:
        return self._inner.dim

    @property
    def settings(self) -> Mapping[str, object]:
        return self._inner.settings

    @property
    def stats(self) -> dict[str, int]:
        """Return cumulative cache hit and miss counts."""
        return {"hits": self._hits, "misses": self._misses}

    def prepare(self) -> float:
        """Delegate one-time initialization without adding it to embed timing."""
        return self._inner.prepare()

    def prepare_tokenizer(self) -> float:
        """Delegate tokenizer-only preparation when the wrapped adapter supports it."""
        prepare = getattr(self._inner, "prepare_tokenizer", None)
        if not callable(prepare):
            return self._inner.prepare()
        return float(prepare())

    def count_tokens(self, texts: Sequence[str]) -> list[int]:
        """Delegate token counting to the wrapped model tokenizer."""
        return self._inner.count_tokens(texts)

    def embed(
        self,
        texts: Sequence[str],
        *,
        kind: EmbeddingKind,
    ) -> list[list[float]]:
        """Read hits, embed only unique misses, and atomically cache vectors."""
        return self._embed_cached(texts, kind=kind, allow_truncation=False)

    def embed_allowing_truncation(
        self,
        texts: Sequence[str],
        *,
        kind: EmbeddingKind,
    ) -> list[list[float]]:
        """Cache explicitly-audited truncated vectors under the same settings key."""
        return self._embed_cached(texts, kind=kind, allow_truncation=True)

    def _embed_cached(
        self,
        texts: Sequence[str],
        *,
        kind: EmbeddingKind,
        allow_truncation: bool,
    ) -> list[list[float]]:
        _validate_kind(kind)
        values = _validated_texts(texts)
        if not values:
            return []
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        results: list[list[float] | None] = [None] * len(values)
        missing: dict[str, tuple[str, list[int]]] = {}
        progress = _Progress(total=len(values))

        for index, text in enumerate(values):
            key = self._key(
                text,
                kind=kind,
                allow_truncation=allow_truncation,
            )
            path = self._cache_dir / f"{key}.json"
            if path.is_file():
                results[index] = self._read_vector(path)
                self._hits += 1
                progress.advance(1)
                continue
            self._misses += 1
            if key in missing:
                missing[key][1].append(index)
            else:
                missing[key] = (text, [index])

        misses = list(missing.items())
        for start in range(0, len(misses), _CACHE_PROGRESS_EVERY):
            group = misses[start : start + _CACHE_PROGRESS_EVERY]
            group_texts = [entry[1][0] for entry in group]
            vectors = (
                self._inner.embed_allowing_truncation(group_texts, kind=kind)
                if allow_truncation
                else self._inner.embed(group_texts, kind=kind)
            )
            if len(vectors) != len(group):
                raise RuntimeError("inner embedder returned an unexpected vector count")
            for (key, (_text, positions)), vector in zip(group, vectors, strict=True):
                checked = _checked_vector(vector, dim=self.dim)
                self._write_vector(self._cache_dir / f"{key}.json", checked)
                for index in positions:
                    results[index] = checked
                progress.advance(len(positions))

        progress.finish()
        if any(vector is None for vector in results):
            raise RuntimeError("cache did not resolve every requested vector")
        return [vector for vector in results if vector is not None]

    def _key(
        self,
        text: str,
        *,
        kind: EmbeddingKind,
        allow_truncation: bool,
    ) -> str:
        try:
            max_seq_length = self.settings["max_seq_length"]
            normalize = self.settings["normalize"]
        except KeyError as exc:
            raise ValueError(
                "embedder settings require max_seq_length and normalize"
            ) from exc
        parts = (
            self.model_id,
            self.revision,
            str(max_seq_length),
            json.dumps(normalize, ensure_ascii=False, sort_keys=True),
            kind,
            text,
        )
        material = " | ".join(
            (*parts, "audited-truncation") if allow_truncation else parts
        )
        return hashlib.sha256(material.encode()).hexdigest()

    def _read_vector(self, path: Path) -> list[float]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid embedding cache file: {path.name}") from exc
        if not isinstance(payload, list):
            raise TypeError(f"invalid embedding cache vector: {path.name}")
        return _checked_vector(payload, dim=self.dim)

    def _write_vector(self, path: Path, vector: Sequence[float]) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.stem}.",
                suffix=".tmp",
                delete=False,
            ) as destination:
                json.dump(list(vector), destination, ensure_ascii=False)
                destination.write("\n")
                destination.flush()
                os.fsync(destination.fileno())
                temporary_path = Path(destination.name)
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()


def embedder_settings_hash(embedder: Embedder) -> str:
    """Hash canonical settings JSON exactly like parsing.schema.settings_hash."""
    serialized = json.dumps(
        dict(embedder.settings),
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode()).hexdigest()[:12]


class _Progress:
    def __init__(self, *, total: int) -> None:
        self.total = total
        self.processed = 0
        self._last_reported = 0
        self._next_report = _CACHE_PROGRESS_EVERY

    def advance(self, count: int) -> None:
        self.processed += count
        while self.processed >= self._next_report:
            self._print(self._next_report)
            self._next_report += _CACHE_PROGRESS_EVERY

    def finish(self) -> None:
        if self._last_reported != self.total:
            self._print(self.total)

    def _print(self, processed: int) -> None:
        print(
            f"embedding cache progress {processed}/{self.total}",
            flush=True,
        )
        self._last_reported = processed


def _resolve_local_snapshot(model_path_or_id: str) -> Path:
    candidate = Path(model_path_or_id).expanduser()
    if candidate.is_dir():
        return candidate.resolve()
    try:
        from huggingface_hub import snapshot_download

        resolved = snapshot_download(
            repo_id=model_path_or_id,
            revision=BGE_M3_REVISION,
            allow_patterns=list(BGE_M3_ALLOW_PATTERNS),
            ignore_patterns=list(BGE_M3_IGNORE_PATTERNS),
            local_files_only=True,
        )
    except Exception as exc:
        raise RuntimeError(
            "로컬 BGE-M3 스냅샷을 찾을 수 없습니다; 모델은 별도로 다운로드해야 합니다 "
            f"(원인: {type(exc).__name__}: {exc})"
        ) from exc
    snapshot = Path(resolved).resolve()
    if not snapshot.is_dir():
        raise RuntimeError("resolved BGE-M3 snapshot path is not a directory")
    return snapshot


def _read_model_max_lengths(snapshot: Path) -> tuple[int, int | None]:
    sentence_config_path = snapshot / "sentence_bert_config.json"
    try:
        sentence_config = _read_json_object(sentence_config_path)
        max_seq_length = sentence_config["max_seq_length"]
        _require_positive_int(max_seq_length, label="max_seq_length")
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "sentence_bert_config.json에서 max_seq_length를 읽을 수 없습니다"
        ) from exc

    position_max: int | None = None
    transformer_config_path = snapshot / "config.json"
    if transformer_config_path.is_file():
        try:
            transformer_config = _read_json_object(transformer_config_path)
            position_max = transformer_config["max_position_embeddings"]
            _require_positive_int(
                position_max,
                label="max_position_embeddings",
            )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "config.json에서 max_position_embeddings를 읽을 수 없습니다"
            ) from exc
        if max_seq_length > position_max:
            raise RuntimeError(
                "sentence_bert_config max_seq_length exceeds "
                "config max_position_embeddings"
            )
    return max_seq_length, position_max


def _read_snapshot_revision(snapshot: Path) -> str:
    parts = snapshot.parts
    for index, part in enumerate(parts[:-1]):
        if part == "snapshots":
            revision = parts[index + 1]
            if _REVISION_RE.fullmatch(revision):
                return revision

    for name in ("config.json", "sentence_bert_config.json"):
        path = snapshot / name
        if not path.is_file():
            continue
        try:
            revision = _read_json_object(path).get("_commit_hash")
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if isinstance(revision, str) and _REVISION_RE.fullmatch(revision):
            return revision

    git_revision = _read_git_revision(snapshot / ".git")
    if git_revision is not None:
        return git_revision
    raise RuntimeError(
        "로컬 모델 스냅샷 경로에서 revision commit hash를 읽을 수 없습니다"
    )


def _read_git_revision(git_path: Path) -> str | None:
    head_path = git_path / "HEAD"
    try:
        head = head_path.read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            head = (
                (git_path / head.removeprefix("ref: "))
                .read_text(encoding="utf-8")
                .strip()
            )
    except OSError:
        return None
    return head if _REVISION_RE.fullmatch(head) else None


def _select_device(torch: object) -> str:
    if bool(torch.cuda.is_available()):  # type: ignore[attr-defined]
        return "cuda"
    if bool(torch.backends.mps.is_available()):  # type: ignore[attr-defined]
        return "mps"
    return "cpu"


def _read_json_object(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"JSON object required: {path.name}")
    return payload


def _normalize_vector(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm == 0.0:
        raise ValueError("embedding vector must have a finite non-zero norm")
    return [float(value / norm) for value in vector]


def _checked_vector(vector: object, *, dim: int) -> list[float]:
    if not isinstance(vector, Sequence) or isinstance(vector, str | bytes):
        raise TypeError("embedding cache value must be a numeric sequence")
    try:
        values = [float(value) for value in vector]
    except (TypeError, ValueError) as exc:
        raise RuntimeError("embedding vector contains a non-numeric value") from exc
    if len(values) != dim or not all(math.isfinite(value) for value in values):
        raise RuntimeError(f"embedding vector must contain {dim} finite values")
    return values


def _validated_texts(texts: Sequence[str]) -> list[str]:
    if isinstance(texts, str | bytes):
        raise TypeError("texts must be a sequence of strings, not one string")
    values = list(texts)
    if not all(isinstance(text, str) for text in values):
        raise TypeError("every text must be a string")
    return values


def _validate_kind(kind: str) -> None:
    if kind not in {"passage", "query"}:
        raise ValueError("kind must be 'passage' or 'query'")


def _raise_for_over_limit(
    token_counts: Sequence[int],
    *,
    max_seq_length: int,
) -> None:
    over_limit = [count for count in token_counts if count > max_seq_length]
    if over_limit:
        raise ValueError(
            "text token count exceeds max_seq_length "
            f"{max_seq_length}: maximum={max(over_limit)}"
        )


def _require_positive_int(value: object, *, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")


def _first_line(message: str) -> str:
    lines = message.splitlines()
    return (lines[0] if lines else "")[:200]
