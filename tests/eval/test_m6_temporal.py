"""Tests for the OpenSearch-free M6 temporal comparison harness."""

from __future__ import annotations

from kodocs.eval.golden import Asker, Qrel, Question
from kodocs.eval.m5_inputs import EvalQuestion
from kodocs.eval.m5_runner import QuestionRun, RunConfig
from kodocs.eval.m6_temporal import TemporalInputs, evaluate_temporal


def _question(qid: str, *, split: str, temporal: bool) -> EvalQuestion:
    current = f"current-{qid}"
    raw = Question(
        id=qid,
        set="gold" if qid.startswith("g-") else "silver",
        source="real" if temporal else "synthetic",
        type="temporal" if temporal else "semantic",
        tags=("temporal:revision",) if temporal else (),
        question=f"{qid} 질문",
        asker=Asker("platform", 3),
        as_of=None,
        qrels=(Qrel(current, 2, start=0, end=1),),
        anchor_values=(),
        answer_values=(),
        expected_response="answer",
        sql=None,
        reference_answer=None,
        reference_sha256=None,
        summary80=None,
        split=split,
    )
    return EvalQuestion(raw, {current: 2}, 1, 1, (), False)


def _run(qid: str, ranked_docs: tuple[str, ...]) -> QuestionRun:
    return QuestionRun(qid, ranked_docs, (), 1.0, 1.0, None, True)


def test_evaluate_temporal_reuses_m5_runs_and_records_only_ids_ranks_counts() -> None:
    temporal = tuple(
        _question(f"g-t{number}", split="dev" if number < 3 else "test", temporal=True)
        for number in range(1, 6)
    )
    outside = _question("s-other", split="test", temporal=False)
    inputs = TemporalInputs(
        groups={
            "gold_dev": temporal[:2],
            "gold_test": temporal[2:],
            "silver_dev": (),
            "silver_test": (outside,),
        },
        prior_versions={
            f"current-{question.question.id}": f"prior-{question.question.id}"
            for question in temporal
        },
    )
    calls: list[tuple[tuple[str, ...], bool]] = []

    def fake_run(
        _client,
        _embedder,
        questions,
        _config,
        *,
        prefer_current: bool = False,
    ):
        calls.append((tuple(item.question.id for item in questions), prefer_current))
        values = []
        for item in questions:
            qid = item.question.id
            if qid == "g-t1":
                ranking = (
                    (f"current-{qid}", "unrelated", f"prior-{qid}")
                    if prefer_current
                    else (f"prior-{qid}", "unrelated", f"current-{qid}")
                )
            elif qid == "s-other":
                ranking = (
                    ("outside-b", "outside-a")
                    if prefer_current
                    else (
                        "outside-a",
                        "outside-b",
                    )
                )
            else:
                ranking = (f"current-{qid}", f"prior-{qid}")
            values.append(_run(qid, ranking))
        return values

    config = RunConfig("composite", "structural", "hybrid_weighted", "nori", 50, 0.7)
    result = evaluate_temporal(
        object(),
        object(),
        inputs,
        config,
        m5_config_hash="frozen-hash",
        run_fn=fake_run,
    )

    assert len(calls) == 8
    assert {prefer for _ids, prefer in calls} == {False, True}
    assert result["m5_config_hash"] == "frozen-hash"
    assert result["question_counts"] == {
        "gold_dev": 2,
        "gold_test": 3,
        "silver_dev": 0,
        "silver_test": 1,
        "all": 6,
        "gold_temporal": 5,
    }
    changes = result["top10_changes"]
    assert changes["changed_question_ids"] == ["g-t1", "s-other"]
    assert changes["temporal_question_ids"] == [f"g-t{n}" for n in range(1, 6)]
    assert "unexpected_changes" not in changes
    assert "unexpected_change_count" not in changes
    assert changes["non_temporal_change_count"] == 1
    # Same two docs in both runs, only the order differs; its relevant doc is current.
    assert changes["non_temporal_changes"] == [
        {
            "question_id": "s-other",
            "membership_changed": False,
            "order_only": True,
            "relevant_docs_all_current": True,
        }
    ]
    assert changes["non_temporal_membership_changed_count"] == 0
    assert changes["non_temporal_order_only_count"] == 1
    assert changes["non_temporal_relevant_all_current_count"] == 1
    before = result["temporal"]["prefer_current_off"]["rows"][0]
    after = result["temporal"]["prefer_current_on"]["rows"][0]
    assert before == {
        "qid": "g-t1",
        "split": "dev",
        "current_rank": 3,
        "prior_rank": 1,
        "prior_above_current": True,
    }
    assert after == {
        "qid": "g-t1",
        "split": "dev",
        "current_rank": 1,
        "prior_rank": 3,
        "prior_above_current": False,
    }
    assert "current_doc_id" not in before
    assert "prior_doc_id" not in before


def test_non_temporal_change_classifies_membership_and_prior_relevant_docs() -> None:
    temporal = tuple(
        _question(f"g-t{number}", split="dev" if number < 3 else "test", temporal=True)
        for number in range(1, 6)
    )
    membership = _question("s-member", split="test", temporal=False)
    # Its only relevant document is the prior version of a temporal rule.
    prior_relevant = _question("s-prior", split="test", temporal=False)
    prior_relevant = EvalQuestion(
        prior_relevant.question,
        {"prior-g-t1": 2},
        1,
        1,
        (),
        False,
    )
    inputs = TemporalInputs(
        groups={
            "gold_dev": temporal[:2],
            "gold_test": temporal[2:],
            "silver_dev": (),
            "silver_test": (membership, prior_relevant),
        },
        prior_versions={
            f"current-{question.question.id}": f"prior-{question.question.id}"
            for question in temporal
        },
    )

    def fake_run(_client, _embedder, questions, _config, *, prefer_current=False):
        values = []
        for item in questions:
            qid = item.question.id
            if qid == "s-member":
                ranking = ("a", "b", "c") if prefer_current else ("a", "b", "x")
            elif qid == "s-prior":
                ranking = ("p2", "p1") if prefer_current else ("p1", "p2")
            else:
                ranking = (f"current-{qid}", f"prior-{qid}")
            values.append(_run(qid, ranking))
        return values

    result = evaluate_temporal(
        object(),
        object(),
        inputs,
        RunConfig("composite", "structural", "hybrid_weighted", "nori", 50, 0.7),
        m5_config_hash="frozen-hash",
        run_fn=fake_run,
    )

    changes = result["top10_changes"]
    assert changes["non_temporal_change_count"] == 2
    by_id = {row["question_id"]: row for row in changes["non_temporal_changes"]}
    assert by_id["s-member"] == {
        "question_id": "s-member",
        "membership_changed": True,
        "order_only": False,
        "relevant_docs_all_current": True,
    }
    assert by_id["s-prior"] == {
        "question_id": "s-prior",
        "membership_changed": False,
        "order_only": True,
        "relevant_docs_all_current": False,
    }
    assert changes["non_temporal_membership_changed_count"] == 1
    assert changes["non_temporal_order_only_count"] == 1
    assert changes["non_temporal_relevant_all_current_count"] == 1
