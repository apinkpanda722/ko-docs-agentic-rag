"""Offline M8 dev contracts: no real service or model is used."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from kodocs.eval import m8_dev as m
from kodocs.eval.m5_dev import _config_hash
from kodocs.eval.m5_test import PreflightError
from kodocs.retrieval.index_schema import index_body


def put(root, path, value):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def config():
    return json.loads(Path("data/manifest/m5_config.json").read_text())


@pytest.fixture
def m5_index():
    return json.loads(Path("data/manifest/m5_index.json").read_text())


def seed_m5_index(root):
    """Copy the committed M5 index records into a temporary root."""
    put(
        root,
        m.M5_INDEX_PATH,
        json.loads(Path("data/manifest/m5_index.json").read_text()),
    )


def live_mapping(config):
    """Shape of the real OpenSearch mapping: what was sent plus server defaults.

    Observed read-only on the real index (key paths only): the only key the
    server added is ``vector.method.parameters``; ``_meta`` carries the embedder
    settings hash.
    """
    properties = index_body()["mappings"]["properties"]
    properties["vector"]["method"]["parameters"] = {
        "ef_construction": 100,
        "m": 16,
    }
    return {
        "properties": properties,
        "_meta": {
            "condition": "composite",
            "chunking": "structural",
            "embedder_settings_hash": config["embedder"]["settings_hash"],
        },
    }


class Client:
    def __init__(self, config=None):
        config = config or json.loads(Path("data/manifest/m5_config.json").read_text())
        self.count = 4811
        self.mapping = live_mapping(config)
        self.fail = None
        self.calls = []

    def request(self, method, path):
        self.calls.append((method, path))
        assert method == "GET"
        if self.fail:
            raise self.fail
        if path.endswith("_count"):
            return {"count": self.count}
        return {m.INDEX: {"mappings": self.mapping}}


def test_live_mapping_with_server_defaults_passes(config, m5_index):
    client = Client(config)
    m.preflight_indexes(client, config, m5_index)
    assert {method for method, _ in client.calls} == {"GET"}  # read-only


def test_the_raw_live_mapping_hash_is_not_what_m5_recorded(config, m5_index):
    """The bug: hashing the stored mapping never equals the builder-side hash."""
    live = live_mapping(config)["properties"]
    recorded = m5_index["indexes"][m.INDEX]["index_field_schema_hash"]
    assert m._canonical_hash(live) != recorded
    assert m._canonical_hash(index_body()["mappings"]["properties"]) == recorded


def _fail(config, m5_index, client=None, match=None):
    with pytest.raises(PreflightError, match=match):
        m.preflight_indexes(client or Client(config), config, m5_index)


def test_changed_analyzer_fails(config, m5_index):
    client = Client(config)
    client.mapping["properties"]["text_nori"]["analyzer"] = "standard"
    _fail(config, m5_index, client, "text_nori.analyzer")


def test_missing_field_fails(config, m5_index):
    client = Client(config)
    del client.mapping["properties"]["doc_id"]
    _fail(config, m5_index, client, "doc_id")


def test_extra_top_level_field_fails(config, m5_index):
    client = Client(config)
    client.mapping["properties"]["extra"] = {"type": "keyword"}
    _fail(config, m5_index, client, "extra")


def test_server_added_nested_defaults_are_allowed_but_changed_values_are_not(
    config, m5_index
):
    client = Client(config)
    client.mapping["properties"]["vector"]["method"]["name"] = "ivf"
    _fail(config, m5_index, client, "vector.method.name")


def test_recorded_mismatch_fails(config, m5_index):
    recorded = json.loads(json.dumps(m5_index))
    recorded["indexes"][m.INDEX]["index_field_schema_hash"] = "changed"
    _fail(config, recorded, None, "스키마 해시")
    changed_config = json.loads(json.dumps(config))
    changed_config["indexes"][m.INDEX]["schema_hash"] = "changed"
    with pytest.raises(PreflightError):
        m.preflight_indexes(Client(changed_config), changed_config, m5_index)


def test_code_schema_that_drifted_from_the_record_fails(config, m5_index, monkeypatch):
    drifted = lambda **kw: {"mappings": {"properties": {"x": {"type": "keyword"}}}}
    monkeypatch.setattr(m, "index_body", drifted)
    _fail(config, m5_index, None, "현재 코드")


def test_meta_embedder_hash_mismatch_fails(config, m5_index):
    client = Client(config)
    client.mapping["_meta"]["embedder_settings_hash"] = "other"
    _fail(config, m5_index, client, "임베더")
    del client.mapping["_meta"]
    _fail(config, m5_index, client, "임베더")


def test_count_mismatch_fails(config, m5_index):
    client = Client(config)
    client.count += 1
    _fail(config, m5_index, client, "chunk_count")


def test_name_and_config_hash_mismatches_fail(config, m5_index):
    changed = json.loads(json.dumps(config))
    del changed["indexes"][m.INDEX]
    with pytest.raises(PreflightError):
        m.preflight_indexes(Client(config), changed, m5_index)
    changed = json.loads(json.dumps(config))
    changed["config_hash"] = "changed"
    with pytest.raises(PreflightError):
        m.preflight_indexes(Client(config), changed, m5_index)


def test_lookup_failure_names_only_the_exception_type(config, m5_index):
    client = Client(config)
    client.fail = ConnectionError("PRIVATE response body")
    with pytest.raises(PreflightError) as caught:
        m.preflight_indexes(client, config, m5_index)
    assert "조회 실패(ConnectionError)" in str(caught.value)
    assert "PRIVATE" not in str(caught.value)


def row(qid="g", scope="gold"):
    return {
        "question_id": qid,
        "set": scope,
        "question": "비공개 질문",
        "asker": {"team": "platform", "grade": 2},
        "chunks": [
            {
                "chunk_id": str(i),
                "doc_id": str(i),
                "text": "비공개 원문",
                "section_key": "s",
                "start": 0,
                "end": 5,
                "base_rank": i + 1,
                "base_score": 50 - i,
            }
            for i in range(50)
        ],
    }


@pytest.fixture
def cached(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "current_rules_hash", lambda root: "a" * 64)
    m.write_candidates([row(), row("s", "silver")], root=tmp_path)
    seed_m5_index(tmp_path)
    return tmp_path


def test_candidates_fingerprint_and_reuse(cached):
    manifest = m.read_candidates_manifest(cached)
    assert manifest["splits"]["dev"]["rows"] == 2
    assert "비공개" not in json.dumps(manifest, ensure_ascii=False)
    assert m.load_candidates(cached) == [row(), row("s", "silver")]
    assert m.write_candidates([], root=cached) == manifest
    (cached / m.CANDIDATES_PATH).write_text("tampered")
    with pytest.raises(PreflightError):
        m.load_candidates(cached)


def test_private_paths_and_manifest_allowlist(tmp_path):
    assert m.CANDIDATES_PATH.is_relative_to("data/interim/m8")
    assert m.SCORES_PATH.is_relative_to("data/interim/m8")
    root = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "check-ignore", str(m.CANDIDATES_PATH)],
            cwd=root,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0
    except FileNotFoundError:
        assert "/data/interim/" in (root / ".gitignore").read_text()
    assert not {"text", "question", "chunks", "asker"} & m.CANDIDATE_MANIFEST_KEYS
    with pytest.raises(PreflightError):
        m.write_candidate_manifest(tmp_path, {"question": "secret"})
    with pytest.raises(PreflightError):
        m.write_private_jsonl(
            tmp_path / "data/manifest/bad.jsonl", [row()], root=tmp_path
        )


def test_tokens_exclusive_and_precondition(cached):
    with pytest.raises(PreflightError, match="make m8-tokens"):
        m.dev_inputs(cached)
    calls = []

    def tokenizer(pairs, **kwargs):
        calls.append(kwargs)
        return {"input_ids": [[0] * 129 for _ in pairs]}

    payload = m.measure_tokens(tokenizer, "local-snapshot", root=cached)
    assert payload["max_length"] == 256
    assert payload["n_pairs"] == 100
    assert calls[0]["truncation"] is False
    assert payload["candidate_cache"] == m.fingerprint(cached / m.CANDIDATES_PATH)
    with pytest.raises(FileExistsError):
        m.measure_tokens(tokenizer, "local-snapshot", root=cached)
    m.dev_inputs(cached)
    manifest = m.read_candidates_manifest(cached)
    manifest["splits"]["dev"]["sha256"] = "bad"
    put(cached, m.CANDIDATE_MANIFEST_PATH, manifest)
    with pytest.raises(PreflightError):
        m.dev_inputs(cached)


@pytest.mark.parametrize(
    "field", ["question", "text", "max_length", "revision", "question_id", "chunk_id"]
)
def test_score_key_covers_entire_request(field):
    values = {
        "question_id": "g",
        "chunk_id": "c",
        "question": "q",
        "text": "t",
        "max_length": 256,
        "revision": "r",
    }
    before = m.score_key(**values)
    values[field] = 512 if field == "max_length" else "changed"
    assert m.score_key(**values) != before


def test_score_cache_only_rescores_changed_pair(cached):
    calls = []
    scorer = SimpleNamespace(
        score=lambda q, texts: calls.append(texts) or [0.5] * len(texts)
    )
    records = m.load_candidates(cached)
    first = m.score_candidates(records, scorer, max_length=256, root=cached)
    assert len(calls) == 2
    assert m.score_candidates(records, scorer, max_length=256, root=cached) == first
    assert len(calls) == 2
    records[0]["chunks"][0]["text"] += "변경"
    m.score_candidates(records, scorer, max_length=256, root=cached)
    assert calls[-1] == ["비공개 원문변경"]
    assert len(calls) == 3


def test_grid_baseline_and_ties(cached):
    records = m.load_candidates(cached)
    scores = [[0.0] * 50] * 2
    questions = [
        SimpleNamespace(
            question=SimpleNamespace(id=q["question_id"], split="dev"),
            rel_by_doc={"0": 2},
        )
        for q in records
    ]
    result = m.evaluate_grid(records, scores, questions)
    assert len(result["grid"]) == 6
    assert result["selected"]["depth"] == 20
    assert result["selected"]["fusion"] == "alone"
    assert result["baseline"]["gold"]["ndcg10"] == 1
    assert result["baseline"]["silver"]["recall10"] == 1
    assert result["baseline_note"] == m.BASELINE_NOTE
    assert not (cached / "data/manifest/m5_dev.json").exists()


def test_freeze_authenticates_files_and_canonical_hash(cached):
    lengths = token_manifest(cached)
    scores = m.score_candidates(
        m.load_candidates(cached),
        SimpleNamespace(score=lambda q, t: [0.0] * len(t)),
        max_length=lengths["max_length"],
        root=cached,
    )
    payload = {
        **m.evaluate_grid(
            m.load_candidates(cached), scores, fake_questions(m.load_candidates(cached))
        ),
        "max_length": lengths["max_length"],
        "device": "mps",
        "batch_size": 4,
        "rules_sha256": "a" * 64,
        "candidate_manifest_sha256": m.sha256_file(cached / m.CANDIDATE_MANIFEST_PATH),
        "max_length_sha256": m.sha256_file(cached / m.MAX_LENGTH_PATH),
        "score_cache": m.fingerprint(cached / m.SCORES_PATH),
    }
    assert scores
    put(cached, m.DEV_PATH, payload)
    frozen = m.freeze_config(root=cached)
    assert frozen["config_hash"] == _config_hash(
        {k: v for k, v in frozen.items() if k != "config_hash"}
    )
    assert m.verify_config(cached / m.FREEZE_PATH).ok
    with pytest.raises(FileExistsError):
        m.freeze_config(root=cached)
    frozen["rules_sha256"] = "b" * 64
    frozen["config_hash"] = _config_hash(
        {k: v for k, v in frozen.items() if k != "config_hash"}
    )
    put(cached, m.FREEZE_PATH, frozen)
    assert not m.verify_config(cached / m.FREEZE_PATH).ok
    with pytest.raises(PreflightError):
        m.dev_inputs(cached)


def fake_questions(records):
    return [
        SimpleNamespace(
            question=SimpleNamespace(id=r["question_id"], split="dev", set=r["set"]),
            rel_by_doc={"0": 2},
        )
        for r in records
    ]


def token_manifest(cached):
    return m.measure_tokens(
        lambda pairs, **kw: {"input_ids": [[1] * 100 for _ in pairs]},
        "local",
        root=cached,
    )


def test_fake_complete_dev_records_timings_baseline_and_fingerprints(
    cached, config, monkeypatch
):
    token_manifest(cached)
    monkeypatch.setattr(
        m, "embedder_settings_hash", lambda e: config["embedder"]["settings_hash"]
    )
    events = []
    embedder = SimpleNamespace(prepare=lambda: events.append("embed"))

    class Scorer:
        def __init__(self, path, **kw):
            events.append("reranker")
            self.kw = kw

        def score(self, q, texts):
            events.append("score")
            return [0.0] * len(texts)

        def describe(self):
            return {"device": "mps", **self.kw}

    def memory():
        assert events == ["embed", "reranker"]
        events.append("memory")
        return {"driver_allocated_memory": 123}

    times = iter([1.0, 3.0, 10.0, 15.0])
    payload = m.run_dev(
        Client(),
        embedder,
        fake_questions(m.load_candidates(cached)),
        config,
        root=cached,
        model_factory=Scorer,
        snapshot_fn=lambda: Path("local"),
        memory_fn=memory,
        clock=lambda: next(times),
    )
    assert payload["init_s"] == 2
    assert payload["score_s"] == 5
    assert payload["baseline"]["gold"]["ndcg10"] == 1
    assert payload["baseline_note"] == m.BASELINE_NOTE
    assert payload["mps_memory"]["driver_allocated_memory"] == 123
    assert payload["score_cache"] == m.fingerprint(cached / m.SCORES_PATH)
    assert "비공개" not in (cached / m.DEV_PATH).read_text()
    with pytest.raises(FileExistsError):
        m.run_dev(Client(), embedder, [], config, root=cached)
    (cached / m.SCORES_PATH).write_text("changed")
    with pytest.raises(PreflightError):
        m.freeze_config(root=cached)


def test_dev_preflight_before_model_load(cached, config):
    token_manifest(cached)
    config["config_hash"] = "bad"
    with pytest.raises(PreflightError):
        m.run_dev(
            Client(),
            None,
            [],
            config,
            root=cached,
            model_factory=lambda *a, **kw: pytest.fail("must not load"),
        )


def test_nested_raw_fields_rejected(cached):
    payload = m.read_candidates_manifest(cached)
    payload["splits"]["dev"]["questions"]["g"]["text"] = "secret"
    with pytest.raises(PreflightError):
        m.write_candidate_manifest(cached, payload)


def test_max_length_bound_to_manifest_bytes(cached):
    token_manifest(cached)
    manifest = m.read_candidates_manifest(cached)
    manifest["splits"]["dev"]["question_count"] += 1
    put(cached, m.CANDIDATE_MANIFEST_PATH, manifest)
    with pytest.raises(PreflightError):
        m.dev_inputs(cached)


def test_freeze_rejects_rules_change_before_writing(cached):
    put(cached, m.DEV_PATH, {"rules_sha256": "changed"})
    with pytest.raises(PreflightError, match="규칙"):
        m.freeze_config(root=cached)
    assert not (cached / m.FREEZE_PATH).exists()


def test_selection_alone_precedes_smaller_depth():
    rows = [
        {"depth": 20, "fusion": "rrf", "selection_score": 0.5},
        {"depth": 50, "fusion": "alone", "selection_score": 0.5},
        {"depth": 30, "fusion": "alone", "selection_score": 0.5},
    ]
    assert m.select_setting(rows) == rows[2]


def test_candidate_collection_reuses_authorized_m5_parameters(
    cached, config, monkeypatch
):
    from kodocs.eval.golden import Asker
    from kodocs.retrieval.search import ChunkHit

    root = cached / "new"
    seed_m5_index(root)
    monkeypatch.setattr(
        m, "embedder_settings_hash", lambda e: config["embedder"]["settings_hash"]
    )
    calls = []

    def search(client, index, context, question, params, embedder, **kwargs):
        calls.append((index, context, question, params, kwargs))
        return SimpleNamespace(
            chunks=tuple(
                ChunkHit(str(i), str(i), 50 - i, 0, 5, "s", None, text="private chunk")
                for i in range(50)
            )
        )

    monkeypatch.setattr(m, "authorized_search", search)
    questions = fake_questions([row(), row("s", "silver")])
    for q in questions:
        q.question.asker = Asker("platform", 2)
        q.question.question = "private question"
    records = m.collect_candidates(Client(), object(), questions, config, root=root)
    assert len(records) == 2
    index, context, text, params, kwargs = calls[0]
    assert index == m.INDEX
    assert context.team == "platform"
    assert text == "private question"
    assert (
        params.method,
        params.candidate_chunks,
        params.bm25_weight,
        params.analyzer,
    ) == ("hybrid_weighted", 50, 0.7, "nori")
    assert kwargs == {"prefer_current": False}
    m.collect_candidates(Client(), None, questions, config, root=root)
    assert len(calls) == 2
    assert "private" not in (root / m.CANDIDATE_MANIFEST_PATH).read_text()


def test_score_fingerprint_checked_before_cache_reuse(cached):
    records = m.load_candidates(cached)
    m.score_candidates(
        records,
        SimpleNamespace(score=lambda q, t: [0.0] * len(t)),
        max_length=256,
        root=cached,
    )
    put(cached, m.DEV_PATH, {"score_cache": m.fingerprint(cached / m.SCORES_PATH)})
    with (cached / m.SCORES_PATH).open("a") as out:
        out.write("{}\n")
    with pytest.raises(PreflightError):
        m.score_candidates(records, None, max_length=256, root=cached)


def test_freeze_rejects_canonical_hash_tamper(cached):
    token_manifest(cached)
    scores = m.score_candidates(
        m.load_candidates(cached),
        SimpleNamespace(score=lambda q, t: [0.0] * len(t)),
        max_length=128,
        root=cached,
    )
    put(
        cached,
        m.DEV_PATH,
        {
            **m.evaluate_grid(
                m.load_candidates(cached),
                scores,
                fake_questions(m.load_candidates(cached)),
            ),
            "max_length": 128,
            "device": "mps",
            "batch_size": 4,
            "rules_sha256": "a" * 64,
            "candidate_manifest_sha256": m.sha256_file(
                cached / m.CANDIDATE_MANIFEST_PATH
            ),
            "max_length_sha256": m.sha256_file(cached / m.MAX_LENGTH_PATH),
            "score_cache": m.fingerprint(cached / m.SCORES_PATH),
        },
    )
    frozen = m.freeze_config(root=cached)
    frozen["depth"] = 50
    put(cached, m.FREEZE_PATH, frozen)
    assert "config_hash" in m.verify_config(cached / m.FREEZE_PATH).mismatches


def test_grid_rejects_test_questions(cached):
    records = m.load_candidates(cached)
    questions = fake_questions(records)
    questions[0].question.split = "test"
    with pytest.raises(PreflightError):
        m.evaluate_grid(records, [[0.0] * 50] * 2, questions)
