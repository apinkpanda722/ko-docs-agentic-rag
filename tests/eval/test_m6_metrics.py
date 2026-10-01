from __future__ import annotations

from kodocs.agent.finalize import NO_ANSWER_TEXT
from kodocs.agent.types import AnswerResult, Citation
from kodocs.eval.golden import Asker, Qrel
from kodocs.eval.m6_metrics import (
    aggregate_runs,
    answer_values_match,
    citation_precision,
    leak_check_ab,
    refusal_rate,
)


def test_answer_values_match_requires_every_normalized_value() -> None:
    assert answer_values_match("A B이며, 값은 3.0 입니다.", ("AB", "3 0"))
    assert not answer_values_match("AB만 있습니다.", ("AB", "3.0"))
    assert answer_values_match("e\u0301 결과", ("é결과.",))


def test_citation_precision_excludes_no_doc_citation_or_empty_qrels() -> None:
    result = AnswerResult(
        answer="답",
        citations=(
            Citation(kind="doc", doc_id="d1"),
            Citation(kind="doc", doc_id="d2"),
            Citation(kind="sql", call_id="c1"),
        ),
        refused=False,
        refuse_reason=None,
        trace={},
    )
    assert citation_precision(result, (Qrel("d1", 2), Qrel("d3", 1))) == 0.5
    sql_only = AnswerResult(
        "답", (Citation(kind="sql", call_id="c1"),), False, None, {}
    )
    assert citation_precision(sql_only, (Qrel("d1", 2),)) is None
    assert citation_precision(result, ()) is None


def test_refusal_rate_uses_same_target_set_and_permission_text() -> None:
    results = [
        {"question_id": "a", "refused": True, "answer": NO_ANSWER_TEXT},
        {"question_id": "b", "refused": True, "answer": "다른 문구"},
        {"question_id": "outside", "refused": True, "answer": NO_ANSWER_TEXT},
    ]
    assert refusal_rate(results, {"a", "b"}) == {
        "numerator": 2,
        "denominator": 2,
        "rate": 1.0,
    }
    assert refusal_rate(results, {"a", "b"}, permission=True) == {
        "numerator": 1,
        "denominator": 2,
        "rate": 0.5,
    }


def test_leak_check_detects_inaccessible_refs_and_canaries() -> None:
    docs = {
        "hidden": {
            "access": {"level": "restricted", "teams": ["security"], "min_grade": 4}
        },
        "visible": {"access": {"level": "public", "teams": [], "min_grade": 1}},
    }
    canaries = {
        "canaries": [
            {"canary_id": "c-hidden", "doc_id": "hidden", "value": "비밀-1234"},
            {"canary_id": "c-visible", "doc_id": "visible", "value": "공개-1234"},
        ]
    }
    result = {
        "answer": "비밀-1234",
        "trace": {
            "steps": [
                {
                    "kind": "tool",
                    "refs": [
                        {"kind": "doc", "doc_id": "hidden"},
                        {"kind": "doc", "doc_id": "visible"},
                    ],
                }
            ]
        },
    }

    leak = leak_check_ab(result, Asker("platform", 1), docs, canaries)

    assert leak["a"] is True
    assert leak["b"] is True
    assert leak["restricted_ref_doc_ids"] == ["hidden"]
    assert leak["canary_ids"] == ["c-hidden"]


def test_aggregate_runs_keeps_numerators_and_denominators_aligned() -> None:
    rows = [
        {
            "question_id": "a",
            "system": "simple",
            "model": "m",
            "status": "completed",
            "cost_usd": 1.0,
            "latency_s": 2.0,
            "steps": 1,
            "tokens": {"input": 10, "output": 2},
            "refuse_reason": None,
        },
        {
            "question_id": "outside",
            "system": "simple",
            "model": "m",
            "status": "completed",
            "cost_usd": 100.0,
            "latency_s": 100.0,
            "steps": 100,
            "tokens": {"input": 1000, "output": 1000},
            "refuse_reason": None,
        },
    ]

    summary = aggregate_runs(rows, question_ids={"a"}, init_s=3.5)

    assert summary["n"] == 1
    assert summary["cost_usd"]["total"] == 1.0
    assert summary["latency_s"]["p50"] == 2.0
    assert summary["steps"]["mean"] == 1.0
    assert summary["init_s"] == 3.5
