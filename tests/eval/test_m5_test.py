"""Tests for the locked, one-shot M5 test runner."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from kodocs.eval import m5_test
from kodocs.eval.golden import Asker, Qrel, Question
from kodocs.eval.m5_inputs import EvalQuestion
from kodocs.eval.m5_metrics import Span
from kodocs.eval.m5_runner import QuestionRun
from kodocs.eval.m5_test import (
    ExperimentOutput,
    Preflight,
    execute_experiment,
    main,
    run_test,
)
from kodocs.ingest.manifest import load_records, sha256_file
from kodocs.retrieval.embedding import FakeEmbedder, embedder_settings_hash
from kodocs.retrieval.index_build import m5_index_specs
from kodocs.retrieval.index_schema import index_name


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_hash(value: object) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _valid_root(root: Path) -> Path:
    manifest = root / "data/manifest"
    _write_json(manifest / "m5_dev.json", {"selected": "fixture"})
    _write_json(
        manifest / "m5_eval_inputs.json",
        {
            "status": "ok",
            "target_counts": {
                "gold_dev": 14,
                "gold_test": 21,
                "silver_dev": 43,
                "silver_test": 65,
            },
            "count_validation_errors": [],
            "visibility": {"failure_count": 0, "failures": []},
            "answer_range_mapping": {"failure_count": 0, "failures": []},
        },
    )
    indexes = {}
    frozen_indexes = {}
    for number, spec in enumerate(m5_index_specs(), start=1):
        name = index_name(spec.condition, spec.chunking)
        schema_hash = f"schema-{number}"
        indexes[name] = {
            "status": "ok",
            "index_field_schema_hash": schema_hash,
            "failure_count": 0,
            "failures": [],
            "embedder": {
                "settings_hash": "embedder-settings",
                "device": "cpu",
                "init_s": 0.25,
            },
        }
        frozen_indexes[name] = {"schema_hash": schema_hash, "chunk_count": number}
    _write_json(manifest / "m5_index.json", {"indexes": indexes})
    config = {
        "hybrid_method": "hybrid_rrf",
        "bm25_weight": None,
        "candidate_chunks": 50,
        "rank_constant": 60,
        "fixed_size": 200,
        "fixed_overlap": 40,
        "embedder": {
            "model_id": "fake-char-ngram",
            "revision": "1",
            "settings_hash": "embedder-settings",
        },
        "indexes": frozen_indexes,
        "m5_eval_inputs_sha256": _sha256(manifest / "m5_eval_inputs.json"),
        "m5_dev_sha256": _sha256(manifest / "m5_dev.json"),
        "code_commit": "fixture-commit",
        "frozen_at": "2026-09-30T12:00:00+09:00",
    }
    config["config_hash"] = _canonical_hash(config)
    _write_json(manifest / "m5_config.json", config)
    return root


class _FakeExperiment:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def __call__(self, _context) -> ExperimentOutput:
        self.calls += 1
        if self.fail:
            raise RuntimeError("search failed")
        return ExperimentOutput(
            result={
                "analysis": {"fake": True},
                "environment": {"opensearch_version": "3.2.0"},
            },
            raw={"ranked_documents": {"g-1": ["doc-a"]}},
        )


def test_first_run_records_success_and_second_without_reason_returns_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _valid_root(tmp_path / "repo")
    out = tmp_path / "isolated"
    experiment = _FakeExperiment()

    assert (
        main(
            ["--embedder", "fake", "--out-root", str(out)],
            root=root,
            experiment_fn=experiment,
        )
        == 0
    )
    assert (out / "data/manifest/m5_test_results/run-01.json").is_file()
    records = load_records(out / "data/manifest/m5_test_runs.jsonl")
    assert len(records) == 1
    assert records[0] == {
        "config_hash": json.loads(
            (root / "data/manifest/m5_config.json").read_text(encoding="utf-8")
        )["config_hash"],
        "previous_result_sha256": None,
        "ran_at": records[0]["ran_at"],
        "rerun_reason": None,
        "result_path": "data/manifest/m5_test_results/run-01.json",
        "result_sha256": sha256_file(out / "data/manifest/m5_test_results/run-01.json"),
        "run_id": "m5-test-01",
        "seq": 1,
        "status": "completed",
    }

    assert (
        main(
            ["--embedder", "fake", "--out-root", str(out)],
            root=root,
            experiment_fn=experiment,
        )
        == 2
    )
    assert experiment.calls == 1
    assert (
        "이전 실행이 있습니다. 재실행은 --rerun-reason이 필요합니다"
        in capsys.readouterr().err
    )


def test_rerun_reason_creates_new_result_and_links_previous_hash(
    tmp_path: Path,
) -> None:
    root = _valid_root(tmp_path / "repo")
    out = tmp_path / "isolated"
    experiment = _FakeExperiment()
    fixed_now = lambda: "2026-09-30T12:34:56+09:00"

    run_test(root=root, out_root=out, experiment_fn=experiment, now_fn=fixed_now)
    first_path = out / "data/manifest/m5_test_results/run-01.json"
    first_hash = sha256_file(first_path)

    run_test(
        root=root,
        out_root=out,
        rerun_reason="OpenSearch 재기동 후 재측정",
        experiment_fn=experiment,
        now_fn=fixed_now,
    )

    second_path = out / "data/manifest/m5_test_results/run-02.json"
    assert second_path.is_file()
    records = load_records(out / "data/manifest/m5_test_runs.jsonl")
    assert records[-1]["seq"] == 2
    assert records[-1]["rerun_reason"] == "OpenSearch 재기동 후 재측정"
    assert records[-1]["previous_result_sha256"] == first_hash
    assert sha256_file(first_path) == first_hash


def test_first_run_rejects_a_rerun_reason_without_search(tmp_path: Path) -> None:
    root = _valid_root(tmp_path / "repo")
    experiment = _FakeExperiment()

    exit_code = main(
        [
            "--rerun-reason",
            "첫 실행인데 잘못 지정",
            "--embedder",
            "fake",
            "--out-root",
            str(tmp_path / "isolated"),
        ],
        root=root,
        experiment_fn=experiment,
    )

    assert exit_code == 2
    assert experiment.calls == 0


@pytest.mark.parametrize(
    "tamper",
    ["config", "index_failure", "visibility", "range_mapping"],
)
def test_preflight_failures_never_call_search(tmp_path: Path, tamper: str) -> None:
    root = _valid_root(tmp_path / "repo")
    manifest = root / "data/manifest"
    if tamper == "config":
        config_path = manifest / "m5_config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["candidate_chunks"] = 200
        _write_json(config_path, config)
    elif tamper == "index_failure":
        index_path = manifest / "m5_index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        first = next(iter(index["indexes"].values()))
        first["failure_count"] = 1
        first["failures"] = [{"reason": "bulk failure"}]
        _write_json(index_path, index)
    else:
        eval_path = manifest / "m5_eval_inputs.json"
        eval_inputs = json.loads(eval_path.read_text(encoding="utf-8"))
        section = "visibility" if tamper == "visibility" else "answer_range_mapping"
        eval_inputs[section]["failure_count"] = 1
        eval_inputs[section]["failures"] = [{"reason": tamper}]
        _write_json(eval_path, eval_inputs)
        config_path = manifest / "m5_config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["m5_eval_inputs_sha256"] = _sha256(eval_path)
        unhashed = {key: value for key, value in config.items() if key != "config_hash"}
        config["config_hash"] = _canonical_hash(unhashed)
        _write_json(config_path, config)
    experiment = _FakeExperiment()

    with pytest.raises(RuntimeError, match="사전 검사"):
        run_test(
            root=root,
            out_root=tmp_path / "isolated",
            experiment_fn=experiment,
        )

    assert experiment.calls == 0


def test_existing_result_file_is_never_overwritten_or_searched(tmp_path: Path) -> None:
    root = _valid_root(tmp_path / "repo")
    out = tmp_path / "isolated"
    result_path = out / "data/manifest/m5_test_results/run-01.json"
    _write_json(result_path, {"sentinel": True})
    before = result_path.read_bytes()
    experiment = _FakeExperiment()

    with pytest.raises(FileExistsError, match="이미 있습니다"):
        run_test(root=root, out_root=out, experiment_fn=experiment)

    assert experiment.calls == 0
    assert result_path.read_bytes() == before


def test_failed_search_is_recorded_but_does_not_consume_first_run_lock(
    tmp_path: Path,
) -> None:
    root = _valid_root(tmp_path / "repo")
    out = tmp_path / "isolated"
    failure = _FakeExperiment(fail=True)

    with pytest.raises(RuntimeError, match="search failed"):
        run_test(root=root, out_root=out, experiment_fn=failure)

    records = load_records(out / "data/manifest/m5_test_runs.jsonl")
    assert records[-1]["status"] == "failed"
    assert records[-1]["seq"] == 1
    assert not (out / "data/manifest/m5_test_results/run-01.json").exists()

    success = _FakeExperiment()
    run_test(root=root, out_root=out, experiment_fn=success)
    assert success.calls == 1
    assert (out / "data/manifest/m5_test_results/run-01.json").is_file()


def _eval_question(
    qid: str,
    *,
    set_name: str,
    split: str,
    source: str,
    question_type: str = "semantic",
) -> EvalQuestion:
    question = Question(
        id=qid,
        set=set_name,
        source=source,
        type=question_type,
        tags=(),
        question=f"질문 {qid}",
        asker=Asker("platform", 2),
        as_of=None,
        qrels=(Qrel("doc-a", 2, start=0, end=2),),
        anchor_values=(),
        answer_values=(),
        expected_response="answer",
        sql=None,
        reference_answer=None,
        reference_sha256=None,
        summary80=None,
        split=split,
    )
    return EvalQuestion(
        question=question,
        rel_by_doc={"doc-a": 2},
        n_relevant_total=1,
        n_relevant_visible=1,
        answer_ranges=(Span("doc-a", 0, 2),),
        repeated=False,
    )


def test_execute_experiment_injects_fake_runner_and_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_dev = tuple(
        _eval_question(f"gd-{number}", set_name="gold", split="dev", source="real")
        for number in range(6)
    )
    gold_test = tuple(
        _eval_question(
            f"gt-{number}",
            set_name="gold",
            split="test",
            source="real" if number < 13 else "synthetic",
            question_type="identifier" if number < 4 else "semantic",
        )
        for number in range(21)
    )
    silver_test = tuple(
        _eval_question(
            f"st-{number}",
            set_name="silver",
            split="test",
            source="synthetic",
            question_type="identifier" if number < 15 else "semantic",
        )
        for number in range(65)
    )
    real_test = gold_test[:13]
    real_all = (*real_dev, *real_test)
    bundle = m5_test._InputBundle(
        gold_test=gold_test,
        silver_test=silver_test,
        real_gold_dev=real_dev,
        real_gold_test=real_test,
        pypdf_real_all=real_all,
        upstage_real_all=real_all,
    )
    fake_embedder = FakeEmbedder()
    embedder_hash = embedder_settings_hash(fake_embedder)
    config = {
        "config_hash": "frozen-config",
        "hybrid_method": "hybrid_weighted",
        "bm25_weight": 0.4,
        "candidate_chunks": 50,
        "rank_constant": 60,
        "embedder": {"settings_hash": embedder_hash},
    }
    root = tmp_path / "repo"
    _write_json(root / "data/manifest/m5_prior_versions.json", {"versions": []})
    (root / "infra").mkdir(parents=True)
    (root / "infra/docker-compose.yml").write_text(
        'OPENSEARCH_JAVA_OPTS: "-Xms2g -Xmx2g"\n', encoding="utf-8"
    )
    context = Preflight(
        root=root,
        output_root=tmp_path / "out",
        config=config,
        index_manifest={},
        eval_inputs={},
        seq=1,
        run_id="m5-test-01",
        ran_at="2026-09-30T12:34:56+09:00",
        rerun_reason=None,
        previous_result_sha256=None,
        result_path=tmp_path / "out/result.json",
        raw_dir=tmp_path / "out/raw",
        runs_path=tmp_path / "out/runs.jsonl",
        embedder_name="fake",
    )
    monkeypatch.setattr(m5_test, "_load_inputs", lambda _root: bundle)
    monkeypatch.setattr(
        m5_test,
        "_make_embedder",
        lambda _name, *, cache_root: fake_embedder,
    )
    monkeypatch.setattr(m5_test, "_load_service_names", lambda _path: ("service",))
    monkeypatch.setattr(
        m5_test,
        "_run_identifier_lookups",
        lambda _client, questions, *, service_names: {
            item.question.id: True for item in questions
        },
    )

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def request(self, method, path):
            assert (method, path) == ("GET", "/")
            return {"version": {"number": "3.2.0"}}

    run_calls: list[tuple[str, str, int]] = []

    def fake_runner(_client, _embedder, questions, config):
        run_calls.append((config.condition, config.chunking, len(questions)))
        return [
            QuestionRun(
                qid=item.question.id,
                ranked_docs=("doc-a",),
                top10_chunks=(Span("doc-a", 0, 2),),
                recall10=1.0,
                ndcg10=1.0,
                span_hit=True,
                top1=True,
            )
            for item in questions
        ]

    analyzed = False

    def fake_analysis(inputs):
        nonlocal analyzed
        analyzed = True
        assert len(inputs.runs) == 29
        assert set(inputs.parser_runs) == {"composite", "pypdf", "upstage"}
        assert len(inputs.real_all_ids) == 19
        return {"fake_analysis": True}

    output = execute_experiment(
        context,
        run_fn=fake_runner,
        analysis_fn=fake_analysis,
        client_factory=FakeClient,
    )

    assert analyzed is True
    assert len(run_calls) == 31
    assert run_calls.count(("pypdf", "structural", 19)) == 1
    assert run_calls.count(("upstage", "structural", 19)) == 1
    assert output.result["fixed_configuration"]["combination_count"] == 14
    assert output.result["question_counts"] == m5_test._EXPECTED_COUNTS
    assert output.result["analysis"] == {"fake_analysis": True}
