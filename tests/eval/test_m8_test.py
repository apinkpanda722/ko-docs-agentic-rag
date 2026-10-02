"""M8 preflight ordering, immutable evidence and fake-only execution."""

import copy
import json
from types import SimpleNamespace as NS

import pytest
from test_m8_contract import put

from kodocs.eval import m8_test as m
from kodocs.eval.m5_test import PreflightError
from kodocs.eval.m5_test import TestLockError as LockError
from kodocs.eval.m7_rawhash import fingerprint
from kodocs.eval.m8_contract import FREEZE_PATH, RUNS_PATH, validate_result


class Model:
    calls = 0

    def __init__(self, *args, **kwargs):
        Model.calls += 1

    def score(self, question, texts):
        return list(range(len(texts)))

    def describe(self):
        return {"device": "cpu", "revision": "fixture"}


def m7_failures_document(sets):
    """Shape of the committed m7_failures.json (verified on the real file).

    ``class_descriptions.d1`` is a plain STRING, ``by_type`` repeats the same
    classes per type, and ``d1_partial`` is a different class; none of them may
    be read as the d1 list.
    """
    by_system_set = {}
    for key, ids in sets.items():
        by_system_set[key] = {
            "by_type": {
                "semantic": {"classes": {"d1": {"count": len(ids), "ids": ["dup"]}}}
            },
            "classes": {
                "d1": {"count": len(ids), "ids": list(ids)},
                "d2": {"count": 0, "ids": []},
            },
            "d1_partial": {"count": 1, "ids": ["partial-only"]},
            "total_runs": 30,
        }
    return {
        "by_system_set": by_system_set,
        "class_descriptions": {"d1": "d1 검색 실패: M8 리랭커 근거"},
        "schema_version": 1,
    }


REAL_LIKE_SETS = {
    "agentic|test_gold": ["gold-0"],
    "agentic|test_silver": ["silver-1", "excluded"],
    "simple|test_gold": [],
    "simple|test_silver": [],
    "agentic|dev_other": ["not-a-test-set"],
}


@pytest.fixture
def experiment(tmp_path, monkeypatch):
    Model.calls = 0
    config = {
        "config_hash": "a" * 64,
        "depth": 20,
        "fusion": "alone",
        "max_length": 512,
        "device": "cpu",
        "batch_size": 4,
        "rules_sha256": "b" * 64,
    }
    put(tmp_path, FREEZE_PATH, config)
    put(tmp_path, "data/manifest/m5_config.json", {})
    put(tmp_path, "data/manifest/m5_index.json", {})
    questions, records, runs = [], [], []
    for scope, count in [("gold", 21), ("silver", 65)]:
        qs = []
        for i in range(count):
            qid = f"{scope}-{i}"
            q = NS(
                id=qid,
                set=scope,
                split="test",
                source="synthetic",
                type="semantic",
                question="PRIVATE QUESTION",
                asker=NS(team="dev", grade=1),
            )
            questions.append(NS(question=q, rel_by_doc={"doc-0": 2}, answer_ranges=()))
            chunks = [
                {
                    "chunk_id": f"chunk-{j}",
                    "doc_id": f"doc-{j}",
                    "text": "PRIVATE CHUNK",
                    "start": 0,
                    "end": 10,
                    "base_rank": j + 1,
                    "base_score": 50 - j,
                }
                for j in range(50)
            ]
            records.append(
                {
                    "question_id": qid,
                    "set": scope,
                    "question": q.question,
                    "chunks": chunks,
                }
            )
            qs.append({"qid": qid, "ndcg10": 1.0})
        runs.append(
            {
                "config": m.FINAL_CONFIG,
                "question_set": scope,
                "split": "test",
                "questions": qs,
            }
        )
    local = put(tmp_path, m.M5_LOCAL_PATH, {"runs": runs})
    put(tmp_path, m.M7_FAILURES_PATH, m7_failures_document(REAL_LIKE_SETS))
    put(
        tmp_path,
        m.M5_RESULT_PATH,
        {
            "analysis": {
                "ladder": {
                    "stages": [{"id": "nori_hybrid_structural", "mean_ndcg10": 1.0}]
                },
                "silver_direction": {
                    "all": {
                        "ladder": {
                            "stages": [
                                {"id": "nori_hybrid_structural", "mean_ndcg10": 1.0}
                            ]
                        }
                    }
                },
            }
        },
    )
    monkeypatch.setattr(m, "verify_config", lambda p: NS(ok=True, mismatches=()))
    monkeypatch.setattr(m, "preflight_indexes", lambda *a: None)

    def collect(*a):
        return copy.deepcopy(records)

    kwargs = {
        "root": tmp_path,
        "client": object(),
        "embedder": object(),
        "questions": questions,
        "collect_fn": collect,
        "model_factory": Model,
        "snapshot_fn": lambda: tmp_path,
        "lengths_fn": lambda model, r: [513] * 50,
        "memory_fn": dict,
    }
    return kwargs, local


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "zero",
        "duplicate",
        "aggregate",
        "question",
        "extra_key",
        "index",
        "freeze",
    ],
)
def test_preflight_no_output_or_model(experiment, monkeypatch, damage):
    kwargs, local = experiment
    root = kwargs["root"]
    d = json.loads(local.read_bytes())
    if damage == "missing":
        local.unlink()
    elif damage == "zero":
        d["runs"][0]["config"] = {}
        put(root, m.M5_LOCAL_PATH, d)
    elif damage == "duplicate":
        d["runs"].append(d["runs"][0])
        put(root, m.M5_LOCAL_PATH, d)
    elif damage == "extra_key":
        d["runs"][0]["config"]["extra"] = 1
        put(root, m.M5_LOCAL_PATH, d)
    elif damage == "aggregate":
        d["runs"][0]["questions"][0]["ndcg10"] = 0
        put(root, m.M5_LOCAL_PATH, d)
    elif damage == "question":
        # Keep the local mean authenticated while individual values disagree.
        d["runs"][0]["questions"][0]["ndcg10"] = 0.5
        d["runs"][0]["questions"][1]["ndcg10"] = 1.5
        put(root, m.M5_LOCAL_PATH, d)
    elif damage == "index":

        def fail(*a):
            raise PreflightError("index mismatch")

        monkeypatch.setattr(m, "preflight_indexes", fail)
    else:
        monkeypatch.setattr(
            m, "verify_config", lambda p: NS(ok=False, mismatches=("hash",))
        )
    with pytest.raises(PreflightError) as exc:
        m.run_test(**kwargs)
    if damage == "missing":
        assert "M5 문항별 결과 파일 없음" in str(exc.value)
    if damage == "question":
        assert "gold-0" in str(exc.value)
    assert Model.calls == 0
    assert not (root / RUNS_PATH).exists()
    assert not (root / "data/interim/m8").exists()


def test_complete_contract_hashes_lock_and_rerun(experiment):
    kwargs, local = experiment
    root = kwargs["root"]
    result = m.run_test(**kwargs)
    validate_result(result)
    assert result["harm"] == {"gold": 21, "silver": 65}
    assert result["truncated_ratio"] == 1
    ledger = m.read_rows(root / RUNS_PATH)
    assert ledger[0]["raw"] == fingerprint(
        root / "data/interim/m8/test/run-01/runs.jsonl"
    )
    assert ledger[0]["m5_local_results"]["sha256"] == fingerprint(local)["sha256"]
    assert ledger[0]["m5_local_results"]["rows"] == 86
    assert ledger[0]["m5_local_results"]["note"] == "M5 기록 없음, M8에서 처음 기록"
    for path in [
        root / RUNS_PATH,
        root / m.RESULTS_DIR / "run-01.json",
        root / "data/interim/m8/test/run-01/runs.jsonl",
    ]:
        assert "PRIVATE" not in path.read_text()
    with pytest.raises(LockError):
        m.run_test(**kwargs)
    m.run_test(**kwargs, rerun_reason="verification rerun")
    ledger = m.read_rows(root / RUNS_PATH)
    assert (
        len(ledger) == 2
        and ledger[1]["previous_result_sha256"] == ledger[0]["result_sha256"]
    )
    assert ledger[1]["rerun_reason"] == "verification rerun"


def test_failed_model_records_raw_without_result(experiment):
    kwargs, _ = experiment

    def fail(*a, **k):
        raise RuntimeError("PRIVATE")

    kwargs["model_factory"] = fail
    with pytest.raises(RuntimeError):
        m.run_test(**kwargs)
    root = kwargs["root"]
    assert not (root / m.RESULTS_DIR / "run-01.json").exists()
    record = m.read_rows(root / RUNS_PATH)[0]
    assert record["status"] == "failed"
    assert record["raw"] == fingerprint(root / record["raw_path"])
    with pytest.raises(LockError):
        m.run_test(**kwargs)


@pytest.mark.parametrize(
    "low,high,expected",
    [
        (0.01, 0.1, "H8 채택"),
        (-0.1, -0.01, "H8 기각(반대 방향)"),
        (0, 0.1, "H8 판정 보류(차이 입증 못 함)"),
        (-0.1, 0, "H8 판정 보류(차이 입증 못 함)"),
    ],
)
def test_registered_verdict(low, high, expected):
    assert m.h8_verdict(NS(ci_low=low, ci_high=high)) == expected


def test_harm_zero_is_not_negative():
    assert m.harm_count([0, -0.01, 0.02]) == 1


def test_matching_dev_and_other_set_do_not_duplicate(experiment):
    kwargs, local = experiment
    data = json.loads(local.read_bytes())
    distractor = copy.deepcopy(data["runs"][0])
    distractor.update(split="dev", questions=distractor["questions"][:6])
    data["runs"].append(distractor)
    put(kwargs["root"], m.M5_LOCAL_PATH, data)
    assert (
        m.verify_control(kwargs["root"], kwargs["collect_fn"](), kwargs["questions"])[
            "rows"
        ]
        == 86
    )


def test_first_run_reason_rejected_before_collection(experiment):
    kwargs, _ = experiment
    with pytest.raises(LockError):
        m.run_test(**kwargs, rerun_reason="not a rerun")
    assert Model.calls == 0
    assert not (kwargs["root"] / RUNS_PATH).exists()


def test_interrupted_scoring_pins_partial_raw(experiment):
    kwargs, _ = experiment

    class InterruptModel(Model):
        n = 0

        def score(self, question, texts):
            self.n += 1
            if self.n == 2:
                raise KeyboardInterrupt
            return super().score(question, texts)

    kwargs["model_factory"] = InterruptModel
    with pytest.raises(KeyboardInterrupt):
        m.run_test(**kwargs)
    record = m.read_rows(kwargs["root"] / RUNS_PATH)[0]
    assert record["status"] == "stopped"
    assert record["raw"]["rows"] == 1
    assert record["raw"] == fingerprint(kwargs["root"] / record["raw_path"])


def test_d1_visible_rel2_presence_and_ranks(experiment):
    kwargs, _ = experiment
    rows = [
        {
            "question_id": "gold-0",
            "control_docs": ["other", "doc-0"],
            "reranked_docs": ["doc-0", "other"],
        }
    ]
    ids = m.load_d1_ids(kwargs["root"])
    table = m.d1_table(ids, rows, kwargs["questions"])
    assert [r["question_id"] for r in table] == ["excluded", "gold-0", "silver-1"]
    assert table[0]["status"] == "대상 외"
    assert table[1]["candidate_rel2_count"] == 1
    assert table[1]["control_ranks"] == {"doc-0": 2}
    assert table[1]["reranked_ranks"] == {"doc-0": 1}


def test_d1_ids_come_only_from_the_explicit_test_set_path(experiment):
    kwargs, _ = experiment
    ids = m.load_d1_ids(kwargs["root"])
    assert ids == ["excluded", "gold-0", "silver-1"]
    assert "dup" not in ids and "partial-only" not in ids  # by_type / d1_partial
    assert "not-a-test-set" not in ids  # only test_gold and test_silver


def test_d1_ids_of_the_committed_m7_failures_manifest():
    """The real file, no network: class_descriptions.d1 is a string there."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    document = m.read_json(root / m.M7_FAILURES_PATH)
    assert isinstance(document["class_descriptions"]["d1"], str)
    groups = document["by_system_set"]
    ids = m.load_d1_ids(root)
    assert groups["agentic|test_gold"]["classes"]["d1"]["ids"] == [
        "g-syn-007",
        "g-syn-012",
    ]
    assert groups["simple|test_gold"]["classes"]["d1"]["ids"] == []
    assert groups["simple|test_silver"]["classes"]["d1"]["ids"] == []
    expected_silver = groups["agentic|test_silver"]["classes"]["d1"]["ids"]
    assert expected_silver[:3] == ["s-syn-003", "s-syn-014", "s-syn-033"]
    assert ids == sorted({"g-syn-007", "g-syn-012", *expected_silver})


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda d: d.pop("by_system_set"), "by_system_set"),
        (
            lambda d: [
                d["by_system_set"].pop(key)
                for key in ("agentic|test_gold", "simple|test_gold")
            ],
            "test_gold",
        ),
        (
            lambda d: d["by_system_set"]["agentic|test_gold"]["classes"].pop("d1"),
            "classes.d1.ids",
        ),
        (
            lambda d: d["by_system_set"]["agentic|test_gold"]["classes"]["d1"].update(
                ids="g-syn-007"
            ),
            "classes.d1.ids",
        ),
        (
            lambda d: d["by_system_set"]["agentic|test_gold"]["classes"]["d1"].update(
                ids=[1]
            ),
            "classes.d1.ids",
        ),
    ],
)
def test_d1_structure_errors_stop_clearly(experiment, mutate, match):
    kwargs, _ = experiment
    sets = {k: v for k, v in REAL_LIKE_SETS.items() if k != "agentic|dev_other"}
    document = m7_failures_document(sets)
    mutate(document)
    put(kwargs["root"], m.M7_FAILURES_PATH, document)
    with pytest.raises(PreflightError, match=match):
        m.load_d1_ids(kwargs["root"])
    (kwargs["root"] / m.M7_FAILURES_PATH).unlink()
    with pytest.raises(PreflightError, match="없음"):
        m.load_d1_ids(kwargs["root"])


def test_structure_error_stops_before_the_model_and_leaves_no_attempt(experiment):
    kwargs, _ = experiment
    root = kwargs["root"]
    put(root, m.M7_FAILURES_PATH, {"by_system_set": {}})
    with pytest.raises(PreflightError):
        m.run_test(**kwargs)
    assert Model.calls == 0
    assert not (root / RUNS_PATH).exists()  # no failed attempt is burned


def test_analysis_is_rehearsed_before_the_model_is_loaded(experiment, monkeypatch):
    kwargs, _ = experiment
    root = kwargs["root"]

    def broken(rows, d1_ids, questions):
        raise AttributeError("analysis bug")

    monkeypatch.setattr(m, "analyze", broken)
    with pytest.raises(AttributeError):
        m.run_test(**kwargs)
    assert Model.calls == 0  # the model never loaded
    assert not (root / RUNS_PATH).exists()


def test_m5_span_hit_and_candidate_recall(experiment):
    kwargs, _ = experiment
    q = kwargs["questions"][0]
    q.answer_ranges = (m.Span("doc-0", 1, 5),)
    record = kwargs["collect_fn"]()[0]
    metrics = m.metric_row(q, record["chunks"], m.control_docs(record))
    assert metrics == {"ndcg10": 1, "recall10": 1, "span_hit": True}


def test_failed_first_attempt_can_rerun_into_first_result(experiment):
    from kodocs.eval.m8_contract import load_h8_state

    kwargs, _ = experiment
    factory = kwargs["model_factory"]

    def fail(*a, **k):
        raise RuntimeError("fixture failure")

    kwargs["model_factory"] = fail
    with pytest.raises(RuntimeError):
        m.run_test(**kwargs)
    kwargs["model_factory"] = factory
    m.run_test(**kwargs, rerun_reason="recover failed initialization")
    root = kwargs["root"]
    ledger = m.read_rows(root / RUNS_PATH)
    assert [r["seq"] for r in ledger] == [1, 1]
    assert ledger[0]["raw"] == fingerprint(root / ledger[0]["raw_path"])
    assert ledger[1]["raw"] == fingerprint(root / ledger[1]["raw_path"])
    assert load_h8_state(root).status == "result"


def test_exclusive_result_refusal_preserves_existing_file(experiment, monkeypatch):
    kwargs, _ = experiment
    path = kwargs["root"] / m.RESULTS_DIR / "run-01.json"

    def collision(target, payload):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("existing result")
        raise FileExistsError

    monkeypatch.setattr(m, "_write_json_exclusive", collision)
    with pytest.raises(FileExistsError):
        m.run_test(**kwargs)
    assert path.read_text() == "existing result"


def test_pair_lengths_must_match_all_scored_pairs(experiment):
    kwargs, _ = experiment
    kwargs["lengths_fn"] = lambda *a: []
    with pytest.raises(PreflightError, match="쌍 길이"):
        m.run_test(**kwargs)
    assert not (kwargs["root"] / m.RESULTS_DIR / "run-01.json").exists()
