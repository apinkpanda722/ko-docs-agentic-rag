"""M8 local reranking components for candidates returned by authorized_* only.

No search, authorization, or orchestration is performed here. Optional model
packages are imported only when an operator explicitly loads a local model.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from kodocs.retrieval.embedding import _read_snapshot_revision

RERANKER_MODEL_ID = "BAAI/bge-reranker-v2-m3"
RERANKER_REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
RERANKER_LICENSE = "Apache-2.0"
RERANKER_EXPECTED_FILES: dict[str, int | None] = {
    "config.json": None,
    "model.safetensors": 2271071852,  # fp32
    "sentencepiece.bpe.model": None,
    "special_tokens_map.json": None,
    "tokenizer.json": None,
    "tokenizer_config.json": None,
}
# 2026-10-02 HF API로 revision 953dc6f6… 파일 목록 확인
# .gitattributes, README.md, assets/*.png 4개와 위 6개 파일; onnx 폴더 없음.
# 다운로드와 오프라인 조회는 이 상수를 함께 사용한다(FP-045).
RERANKER_ALLOW_PATTERNS = ("*.json", "*.model", "*.safetensors")
RERANKER_IGNORE_PATTERNS = ("assets/*", "*.png", "*.md")
RANK_CONSTANT = 60


def resolve_local_reranker_snapshot(cache_dir: str | Path | None = None) -> Path:
    """Resolve and validate the fixed partial snapshot without network access."""
    from huggingface_hub import snapshot_download

    kwargs = {} if cache_dir is None else {"cache_dir": cache_dir}
    try:
        path = Path(
            snapshot_download(
                RERANKER_MODEL_ID,
                revision=RERANKER_REVISION,
                allow_patterns=list(RERANKER_ALLOW_PATTERNS),
                ignore_patterns=list(RERANKER_IGNORE_PATTERNS),
                local_files_only=True,
                **kwargs,
            )
        ).absolute()
    except Exception as exc:
        raise RuntimeError(
            "로컬 리랭커 스냅샷 조회 실패; make rerank-models를 별도로 실행하세요: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    errors = []
    for name, expected_size in RERANKER_EXPECTED_FILES.items():
        file = path / name
        if not file.is_file():
            errors.append(f"missing file: {name}")
        elif expected_size is not None:
            actual = file.stat().st_size
            if actual != expected_size:
                errors.append(f"{name}: bytes={actual}, expected={expected_size}")
    if errors:
        raise RuntimeError("리랭커 스냅샷 검증 실패: " + "; ".join(errors))
    try:
        from transformers import AutoConfig, AutoTokenizer

        # Installed transformers/models/auto/tokenization_auto.py:660 and
        # configuration_auto.py:303 accept local directory + **kwargs.
        AutoTokenizer.from_pretrained(str(path), local_files_only=True)
        AutoConfig.from_pretrained(str(path), local_files_only=True)
    except Exception as exc:
        raise RuntimeError(
            f"리랭커 tokenizer/config 오프라인 로드 실패: {type(exc).__name__}: {exc}"
        ) from exc
    return path


@dataclass(frozen=True)
class RerankCandidate:
    """Already authorized chunk, with its one-based control rank."""

    chunk_id: str
    doc_id: str
    text: str
    base_rank: int

    def __post_init__(self) -> None:
        _positive_integer(self.base_rank, "base_rank")


class CrossEncoderReranker:
    """Load once in the constructor; time score() separately from init_s.

    Verified installed sentence-transformers 6.1.0 sources (FP-011):
    cross_encoder/model.py:150-172 constructor: model_name_or_path,
    max_length, device, local_files_only, model_kwargs/processor_kwargs/
    config_kwargs, activation_fn. :183-186 maps max_length to tokenizer.
    :406-428 config activation overrides default; otherwise single label
    uses nn.Sigmoid (multiple labels nn.Identity).
    :568-585 predict accepts Sequence[PairInput], batch_size,
    show_progress_bar, activation_fn, convert_to_numpy/convert_to_tensor.
    :738-744 restores input order and returns ndarray when numpy=True.
    :752-768 rank returns sorted corpus_id/score dictionaries; not used here
    because depth/fusion/ties must follow M8-T1.
    base/modules/transformer.py:1260 uses truncation='longest_first';
    :1869-1872 calls tokenizer(primary_input=list of text pairs).
    Raw logits use explicit Identity, avoiding sigmoid saturation ties;
    they are used only for ordering, never thresholds (M8-R3).
    """

    def __init__(
        self,
        model_path: Path,
        *,
        max_length: int,
        device: str,
        batch_size: int,
        model_factory: Callable | None = None,
    ) -> None:
        _positive_integer(max_length, "max_length")
        _positive_integer(batch_size, "batch_size")
        if not isinstance(device, str) or not device:
            raise ValueError("device must be explicit")
        if not model_path.is_dir():
            raise ValueError(f"local model directory required: {model_path}")
        revision = _read_snapshot_revision(model_path)
        if revision != RERANKER_REVISION:
            raise ValueError(f"model revision={revision}, expected={RERANKER_REVISION}")
        import torch

        # torch/backends/mps/__init__.py:29-31, verified installed source.
        if device.split(":", 1)[0] == "mps" and not torch.backends.mps.is_available():
            raise RuntimeError("requested mps device is unavailable")
        if model_factory is None:
            from sentence_transformers import CrossEncoder

            model_factory = CrossEncoder
        self._activation = torch.nn.Identity()
        self._model = model_factory(
            str(model_path),
            max_length=max_length,
            device=device,
            local_files_only=True,
            activation_fn=self._activation,
        )
        # base/model.py:1510-1516 reads actual transformers model device.
        actual_device = str(self._model.device)
        if _torch_device_key(actual_device) != _torch_device_key(device):
            raise RuntimeError(
                f"device mismatch: requested={device}, actual={actual_device}"
            )
        self._device = actual_device
        self._batch_size = batch_size
        self._max_length = max_length
        self._revision = revision

    def score(self, question: str, texts: Sequence[str]) -> list[float]:
        """Score unchanged input pairs, with no model loading in this method."""
        if not texts:
            return []
        scores = self._model.predict(
            [(question, text) for text in texts],
            batch_size=self._batch_size,
            show_progress_bar=False,
            activation_fn=self._activation,
            apply_softmax=False,
            convert_to_numpy=True,
            convert_to_tensor=False,
            device=self._device,
        )
        values = [float(value) for value in scores]
        if len(values) != len(texts) or not all(math.isfinite(v) for v in values):
            raise RuntimeError("predict must return one finite score per input pair")
        return values

    def describe(self) -> dict:
        """Return actual device and fixed predict settings for result records."""
        return {
            "model_id": RERANKER_MODEL_ID,
            "revision": self._revision,
            "device": self._device,
            "batch_size": self._batch_size,
            "max_length": self._max_length,
            "activation_fn": "torch.nn.Identity",
            "local_files_only": True,
            "predict": {
                "show_progress_bar": False,
                "activation_fn": "torch.nn.Identity",
                "apply_softmax": False,
                "convert_to_numpy": True,
                "convert_to_tensor": False,
            },
        }


def _torch_device_key(device: str) -> str:
    """Normalize implicit device zero for comparison without choosing a device."""
    return device.removesuffix(":0")


def mps_memory_snapshot() -> dict:
    """Record MPS byte counters only; unavailable APIs yield None (M8-P1).

    Installed torch/mps/__init__.py:110,120,130 defines these three counters.
    Availability: torch/backends/mps/__init__.py:29. No allocation is requested.
    """
    fields = (
        "current_allocated_memory",
        "driver_allocated_memory",
        "recommended_max_memory",
    )
    values = dict.fromkeys(fields)
    try:
        import torch

        if not torch.backends.mps.is_available():
            return values
    except (ImportError, AttributeError, RuntimeError):
        return values
    for name in fields:
        function = getattr(getattr(torch, "mps", None), name, None)
        if callable(function):
            try:
                values[name] = function()
            except RuntimeError:
                pass
    return values


def measure_pair_lengths(tokenizer, question: str, texts: Sequence[str]) -> list[int]:
    """Count full pairs as CrossEncoder does, including special tokens.

    ST 6.1.0 base/modality.py:430-438 keeps a batch of (str,str) text pairs;
    base/modules/transformer.py:1869-1872 passes it to the tokenizer. Only
    truncation/padding differ here to measure untruncated individual lengths.
    """
    if not texts:
        return []
    encoded = tokenizer(
        [(question, text) for text in texts],
        add_special_tokens=True,
        truncation=False,
        padding=False,
    )
    return [len(ids) for ids in encoded["input_ids"]]


def p99_nearest_rank(values: Sequence[int]) -> int:
    """Return the ceil(0.99*n)-th ascending positive pair length."""
    if not values:
        raise ValueError("lengths must not be empty")
    for value in values:
        _positive_integer(value, "pair length")
    return sorted(values)[(99 * len(values) + 99) // 100 - 1]


@dataclass(frozen=True)
class MaxLengthChoice:
    """Registered P99 choice and truncation count/ratio for all measured pairs."""

    value: int
    p99: int
    capped: bool
    truncated_pairs: int
    total_pairs: int

    @property
    def truncated_ratio(self) -> float:
        return self.truncated_pairs / self.total_pairs


def choose_max_length(lengths: Sequence[int], cap: int = 1024) -> MaxLengthChoice:
    """Select smallest power of two >= P99, with the registered cap."""
    _positive_integer(cap, "cap")
    p99 = p99_nearest_rank(lengths)
    power = 1 << (p99 - 1).bit_length()
    value = min(power, cap)
    return MaxLengthChoice(
        value, p99, power > cap, sum(n > value for n in lengths), len(lengths)
    )


def reorder(
    candidates: Sequence[RerankCandidate],
    scores: Sequence[float],
    *,
    depth: int,
    fusion: Literal["alone", "rrf"],
) -> list[str]:
    """Permute authorized candidates only, keeping the control tail (M8-T1)."""
    if len(scores) != len(candidates):
        raise ValueError("scores and candidates must have equal length")
    _positive_integer(depth, "depth")
    if depth > len(candidates):
        raise ValueError("depth must be in 1..len(candidates)")
    if fusion not in {"alone", "rrf"}:
        raise ValueError("fusion must be alone or rrf")
    if sorted(c.base_rank for c in candidates) != list(range(1, len(candidates) + 1)):
        raise ValueError("base_rank must be a permutation of 1..len(candidates)")
    if len({c.chunk_id for c in candidates}) != len(candidates):
        raise ValueError("chunk_id must be unique")
    if not all(math.isfinite(s) for s in scores):
        raise ValueError("scores must be finite")
    base = sorted(
        zip(candidates, scores, strict=True), key=lambda pair: pair[0].base_rank
    )
    head, tail = base[:depth], base[depth:]
    reranked = sorted(head, key=lambda pair: (-pair[1], pair[0].base_rank))
    if fusion == "rrf":
        reranker_ranks = {c.chunk_id: rank for rank, (c, _) in enumerate(reranked, 1)}
        reranked = sorted(
            head,
            key=lambda pair: (
                -(
                    1 / (RANK_CONSTANT + pair[0].base_rank)
                    + 1 / (RANK_CONSTANT + reranker_ranks[pair[0].chunk_id])
                ),
                pair[0].base_rank,
            ),
        )
    return [c.chunk_id for c, _ in (*reranked, *tail)]


def _positive_integer(value: int, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
