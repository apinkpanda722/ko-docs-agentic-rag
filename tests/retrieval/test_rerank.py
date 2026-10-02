"""M8 core contracts; all model, hub, tokenizer, and MPS calls are fakes."""

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kodocs.eval.m8_rules import extract_rules_section
from kodocs.retrieval import rerank as r

ROOT = Path(__file__).resolve().parents[2]
FILES = (
    "config.json",
    "model.safetensors",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)


def test_constants_match_registered_rule_and_confirmed_file_inventory():
    rule = next(
        line
        for line in extract_rules_section(
            ROOT / "docs/results/hypotheses.md"
        ).splitlines()
        if "**M8-R2**" in line
    )
    assert r.RERANKER_REVISION == re.search(r"revision `([a-f0-9]+)`", rule)[1]
    assert r.RERANKER_EXPECTED_FILES == dict.fromkeys(FILES, None) | {
        "model.safetensors": int(
            re.search(r"`model.safetensors` ([\d,]+)바이트", rule)[1].replace(",", "")
        )
    }
    assert r.RERANKER_MODEL_ID == "BAAI/bge-reranker-v2-m3"
    assert r.RERANKER_LICENSE == "Apache-2.0"
    assert r.RERANKER_ALLOW_PATTERNS == ("*.json", "*.model", "*.safetensors")
    assert r.RERANKER_IGNORE_PATTERNS == ("assets/*", "*.png", "*.md")


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    path = tmp_path / "snapshots" / r.RERANKER_REVISION
    path.mkdir(parents=True)
    for name in FILES:
        (path / name).touch()
    real_stat = Path.stat

    def stat(self, *args, **kwargs):
        result = real_stat(self, *args, **kwargs)
        if self == path / "model.safetensors":
            return SimpleNamespace(st_size=2271071852, st_mode=result.st_mode)
        return result

    monkeypatch.setattr(Path, "stat", stat)
    calls = []

    def download(repo_id, **kwargs):
        calls.append((repo_id, kwargs))
        return str(path)

    def load(path_arg, **kwargs):
        calls.append((path_arg, kwargs))
        return object()

    monkeypatch.setitem(
        sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=download)
    )
    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(
            AutoTokenizer=SimpleNamespace(from_pretrained=load),
            AutoConfig=SimpleNamespace(from_pretrained=load),
        ),
    )
    return path, calls


def test_snapshot_pins_offline_query_and_loads_tokenizer_and_config(snapshot):
    path, calls = snapshot
    assert r.resolve_local_reranker_snapshot(cache_dir=path.parent) == path
    assert calls == [
        (
            r.RERANKER_MODEL_ID,
            {
                "revision": r.RERANKER_REVISION,
                "allow_patterns": list(r.RERANKER_ALLOW_PATTERNS),
                "ignore_patterns": list(r.RERANKER_IGNORE_PATTERNS),
                "local_files_only": True,
                "cache_dir": path.parent,
            },
        ),
        (str(path), {"local_files_only": True}),
        (str(path), {"local_files_only": True}),
    ]


@pytest.mark.parametrize("missing", FILES)
def test_snapshot_rejects_each_missing_file(snapshot, missing):
    path, calls = snapshot
    (path / missing).unlink()
    with pytest.raises(RuntimeError, match=re.escape(missing)):
        r.resolve_local_reranker_snapshot()
    assert len(calls) == 1


def test_snapshot_rejects_wrong_model_bytes(snapshot, monkeypatch):
    _path, _ = snapshot
    monkeypatch.setattr(
        Path,
        "stat",
        lambda self, **kwargs: SimpleNamespace(st_size=123, st_mode=0o100644),
    )
    with pytest.raises(RuntimeError, match="model.safetensors.*123.*2271071852"):
        r.resolve_local_reranker_snapshot()


@pytest.mark.parametrize("stage", ["snapshot_download", "AutoTokenizer", "AutoConfig"])
def test_snapshot_failure_keeps_original_cause(snapshot, monkeypatch, stage):
    def fail(*args, **kwargs):
        raise OSError("original cache broken")

    if stage == "snapshot_download":
        monkeypatch.setattr(sys.modules["huggingface_hub"], stage, fail)
    else:
        monkeypatch.setattr(
            getattr(sys.modules["transformers"], stage), "from_pretrained", fail
        )
    with pytest.raises(RuntimeError, match="OSError: original cache broken"):
        r.resolve_local_reranker_snapshot()


def candidates():
    return [
        r.RerankCandidate(str(i), f"doc-{i}", f"text-{i}", i) for i in (4, 2, 1, 3, 5)
    ]


@pytest.mark.parametrize(
    ("fusion", "expected"),
    [("alone", ["3", "2", "1", "4", "5"]), ("rrf", ["1", "3", "2", "4", "5"])],
)
def test_reorder_hand_calculated_ties_and_tail(fusion, expected):
    # depth=3, reranker ranks: 3,2,1. RRF: ranks 1 & 3 tie
    # at 1/61+1/63; rank 2 gets 2/62 (slightly smaller).
    values = candidates()
    assert r.reorder(values, [100, 2, 1, 3, 100], depth=3, fusion=fusion) == expected
    tied = r.reorder(values, [100, 1, 1, 1, 100], depth=3, fusion=fusion)
    assert tied == ["1", "2", "3", "4", "5"]
    assert sorted(expected) == sorted(c.chunk_id for c in values)
    assert [c.base_rank for c in values] == [4, 2, 1, 3, 5]


@pytest.mark.parametrize("depth", [0, -1, 6, 1.5, True])
def test_reorder_invalid_depth(depth):
    with pytest.raises(ValueError, match="depth"):
        r.reorder(candidates(), [1] * 5, depth=depth, fusion="alone")


def test_reorder_invalid_length_and_fusion():
    with pytest.raises(ValueError, match="scores"):
        r.reorder(candidates(), [1], depth=1, fusion="alone")
    with pytest.raises(ValueError, match="fusion"):
        r.reorder(candidates(), [1] * 5, depth=1, fusion="unknown")
    with pytest.raises(ValueError, match="depth"):
        r.reorder([], [], depth=1, fusion="alone")


@pytest.mark.parametrize(
    ("lengths", "value", "p99", "capped", "truncated"),
    [
        ([1], 1, 1, False, 0),
        ([2], 2, 2, False, 0),
        ([3], 4, 3, False, 0),
        ([512], 512, 512, False, 0),
        ([1024], 1024, 1024, False, 0),
        ([1025], 1024, 1025, True, 1),
        ([100] * 99 + [2000], 128, 100, False, 1),
    ],
)
def test_max_length_boundaries(lengths, value, p99, capped, truncated):
    choice = r.choose_max_length(lengths)
    assert (
        choice.value,
        choice.p99,
        choice.capped,
        choice.truncated_pairs,
        choice.total_pairs,
    ) == (value, p99, capped, truncated, len(lengths))
    assert choice.truncated_ratio == truncated / len(lengths)
    assert r.p99_nearest_rank(lengths) == p99


@pytest.mark.parametrize("lengths", [[], [0], [-1], [True], [1.5]])
def test_invalid_lengths(lengths):
    with pytest.raises(ValueError):
        r.choose_max_length(lengths)


def test_measure_pair_lengths_uses_same_pair_batch_without_truncation():
    # ST 6.1.0 base/modules/transformer.py:1260,1869-1872:
    # tokenizer(primary_input=list of pairs), truncation='longest_first'.
    calls = []

    def tokenizer(pairs, **kwargs):
        calls.append((pairs, kwargs))
        return {"input_ids": [[1, 2, 3], [1, 2, 3, 4]]}

    assert r.measure_pair_lengths(tokenizer, " 질문 ", [" 청크 ", "다른 청크"]) == [
        3,
        4,
    ]
    assert calls == [
        (
            [(" 질문 ", " 청크 "), (" 질문 ", "다른 청크")],
            {"add_special_tokens": True, "truncation": False, "padding": False},
        )
    ]
    assert r.measure_pair_lengths(tokenizer, "q", []) == []


@pytest.fixture
def fake_torch(monkeypatch):
    class Identity:
        def __call__(self, value):
            return value

    torch = SimpleNamespace(
        backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: False)),
        nn=SimpleNamespace(Identity=Identity),
        mps=SimpleNamespace(
            current_allocated_memory=lambda: 10,
            driver_allocated_memory=lambda: 20,
            recommended_max_memory=lambda: 30,
        ),
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    return torch


def test_cross_encoder_source_contract_and_loading_separate_from_score(
    snapshot, fake_torch
):
    # ST 6.1.0 cross_encoder/model.py:150-172 constructor;
    # :406-428 config override then Sigmoid for one label;
    # :568-585 predict (NOT old activation_fct); :738-744 ndarray return.
    path, _ = snapshot
    calls = []

    class FakeModel:
        device = "cpu"

        def predict(self, pairs, **kwargs):
            calls.append((pairs, kwargs))
            return [0.5, -2]

    def factory(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeModel()

    model = r.CrossEncoderReranker(
        path, max_length=512, device="cpu", batch_size=4, model_factory=factory
    )
    assert calls[0][0] == (str(path),)
    assert calls[0][1] | {"activation_fn": None} == {
        "max_length": 512,
        "device": "cpu",
        "local_files_only": True,
        "activation_fn": None,
    }
    assert isinstance(calls[0][1]["activation_fn"], fake_torch.nn.Identity)
    assert len(calls) == 1
    assert model.score(" 질문 ", ["a", "b"]) == [0.5, -2.0]
    pairs, kwargs = calls[1]
    assert pairs == [(" 질문 ", "a"), (" 질문 ", "b")]
    assert kwargs | {"activation_fn": None} == {
        "batch_size": 4,
        "show_progress_bar": False,
        "activation_fn": None,
        "apply_softmax": False,
        "convert_to_numpy": True,
        "convert_to_tensor": False,
        "device": "cpu",
    }
    assert kwargs["activation_fn"] is calls[0][1]["activation_fn"]
    description = model.describe()
    assert description["device"] == "cpu"
    assert description["batch_size"] == 4
    assert description["max_length"] == 512
    assert description["revision"] == r.RERANKER_REVISION
    assert description["activation_fn"] == "torch.nn.Identity"
    assert model.score("q", []) == []
    assert len(calls) == 2


def test_mps_unavailable_never_falls_back(snapshot, fake_torch):
    with pytest.raises(RuntimeError, match="mps"):
        r.CrossEncoderReranker(
            snapshot[0],
            max_length=512,
            device="mps",
            batch_size=4,
            model_factory=lambda *a, **k: pytest.fail("must not load"),
        )


def test_actual_device_mismatch_is_error(snapshot, fake_torch):
    fake_torch.backends.mps.is_available = lambda: True
    with pytest.raises(RuntimeError, match="device"):
        r.CrossEncoderReranker(
            snapshot[0],
            max_length=512,
            device="mps",
            batch_size=4,
            model_factory=lambda *a, **k: SimpleNamespace(device="cpu"),
        )


def test_memory_snapshot_unavailable_and_partial_apis(fake_torch):
    assert all(v is None for v in r.mps_memory_snapshot().values())
    fake_torch.backends.mps.is_available = lambda: True
    assert r.mps_memory_snapshot() == {
        "current_allocated_memory": 10,
        "driver_allocated_memory": 20,
        "recommended_max_memory": 30,
    }
    del fake_torch.mps.driver_allocated_memory
    assert r.mps_memory_snapshot()["driver_allocated_memory"] is None


def test_available_mps_is_loaded_and_recorded(snapshot, fake_torch):
    fake_torch.backends.mps.is_available = lambda: True
    calls = []

    def factory(path, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(device="mps:0")

    model = r.CrossEncoderReranker(
        snapshot[0], max_length=1024, device="mps", batch_size=2, model_factory=factory
    )
    assert calls[0]["device"] == "mps"
    assert model.describe()["device"] == "mps:0"


@pytest.mark.parametrize("scores", [[float("nan")], [float("inf")], []])
def test_invalid_predict_output_stops_run(snapshot, fake_torch, scores):
    model = r.CrossEncoderReranker(
        snapshot[0],
        max_length=512,
        device="cpu",
        batch_size=4,
        model_factory=lambda *a, **k: SimpleNamespace(
            device="cpu", predict=lambda *a, **k: scores
        ),
    )
    with pytest.raises(RuntimeError, match="one finite score"):
        model.score("q", ["text"])


def test_unordered_lengths_and_custom_cap():
    assert r.p99_nearest_rank([1000, 1, 4]) == 1000
    choice = r.choose_max_length([65, 64], cap=64)
    assert (choice.value, choice.capped, choice.truncated_ratio) == (64, True, 0.5)
    with pytest.raises(ValueError, match="cap"):
        r.choose_max_length([1], cap=0)


@pytest.mark.parametrize("fusion", ["alone", "rrf"])
def test_all_depths_keep_every_candidate_and_control_tail(fusion):
    values = candidates()
    for depth in range(1, 6):
        result = r.reorder(values, [0, 1, 2, 3, 4], depth=depth, fusion=fusion)
        assert set(result) == {c.chunk_id for c in values}
        assert len(result) == len(values)
        assert result[depth:] == [str(i) for i in range(depth + 1, 6)]
