"""Tests for deterministic M5 evaluation-input preparation."""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import replace
from pathlib import Path

import pytest

from kodocs.chunking.corpus import Access, CorpusDoc, Section
from kodocs.eval.golden import Asker, Qrel, Question, write_questions
from kodocs.eval.m5_inputs import (
    RETRIEVAL_TYPES,
    answer_ranges_by_doc,
    answer_repeated,
    build_eval_questions,
    check_asker_visibility,
    load_retrieval_targets,
    qrel_answer_ranges,
    visibility_report,
)
from kodocs.eval.m5_metrics import ndcg_at_k, recall_at_k
from kodocs.retrieval import index_build

_PUBLIC_ACCESS = Access("public", (), 0)
_PLATFORM_ASKER = Asker("platform", 2)


def _doc(
    doc_id: str,
    text: str,
    *,
    source: str = "synthetic",
    access: Access = _PUBLIC_ACCESS,
    sections: tuple[Section, ...] | None = None,
    rule_id: str | None = None,
) -> CorpusDoc:
    return CorpusDoc(
        doc_id=doc_id,
        source=source,
        doc_type="regulation" if source == "real" else "guide",
        title=doc_id,
        text=text,
        sections=sections or (Section("preamble", None, (doc_id,), 0, len(text)),),
        access=access,
        team=None,
        date=None,
        rule_id=rule_id,
        is_current=True,
        superseded_by=None,
        identifiers={
            kind: ()
            for kind in ("ticket", "approval", "incident", "service", "regulation")
        },
    )


def _question(
    *qrels: Qrel,
    qid: str = "q-1",
    set_name: str = "gold",
    source: str = "synthetic",
    question_type: str = "semantic",
    split: str = "dev",
    asker: Asker = _PLATFORM_ASKER,
) -> Question:
    return Question(
        id=qid,
        set=set_name,
        source=source,
        type=question_type,
        tags=(),
        question="질문",
        asker=asker,
        as_of=None,
        qrels=qrels,
        anchor_values=(),
        answer_values=(),
        expected_response="answer",
        sql=None,
        reference_answer=None,
        reference_sha256=None,
        summary80="요약" if source == "real" else None,
        split=split,
    )


def test_load_retrieval_targets_filters_and_deduplicates_question_ids(
    tmp_path: Path,
) -> None:
    query_dir = tmp_path / "data/golden/queries"
    eligible = _question(Qrel("doc-1", 1))
    wrong_type = _question(Qrel("doc-1", 2), qid="q-sql", question_type="sql")
    no_relevance = _question(Qrel("doc-1", 0), qid="q-zero")
    silver = replace(eligible, id="s-1", set="silver", split="test")
    write_questions(
        query_dir / "gold.jsonl", [eligible, eligible, wrong_type, no_relevance]
    )
    write_questions(query_dir / "silver.jsonl", [silver])

    targets = load_retrieval_targets(tmp_path)

    assert RETRIEVAL_TYPES == (
        "identifier",
        "semantic",
        "table",
        "multi_doc",
        "temporal",
    )
    assert [question.id for question in targets["gold_dev"]] == ["q-1"]
    assert targets["gold_test"] == []
    assert targets["silver_dev"] == []
    assert [question.id for question in targets["silver_test"]] == ["s-1"]


def test_eval_question_uses_only_visible_relevant_documents_and_ranges() -> None:
    documents = {
        "visible-high": _doc("visible-high", "높은 정답"),
        "visible-low": _doc(
            "visible-low",
            "낮은 정답",
            access=Access("team", ("platform",), 0),
        ),
        "hidden": _doc(
            "hidden",
            "숨은 정답",
            access=Access("restricted", ("security",), 4),
        ),
    }
    question = _question(
        Qrel("visible-high", 2, start=0, end=5),
        Qrel("visible-low", 1, start=0, end=5),
        Qrel("hidden", 2, start=0, end=5),
    )

    [prepared] = build_eval_questions(
        [question], documents, table_golden={"tables": []}
    )

    assert prepared.rel_by_doc == {"visible-high": 2, "visible-low": 1}
    assert prepared.n_relevant_total == 3
    assert prepared.n_relevant_visible == 2
    assert {span.doc_id for span in prepared.answer_ranges} == {
        "visible-high",
        "visible-low",
    }
    ranking = ["visible-high", "visible-low"]
    assert recall_at_k(ranking, prepared.rel_by_doc) == 1.0
    assert ndcg_at_k(ranking, prepared.rel_by_doc) == 1.0

    report = visibility_report([question], documents)
    assert [pair.question_id for pair in report.excluded_pairs] == [question.id]
    assert report.excluded_pairs[0].doc_id == "hidden"
    assert report.excluded_pairs[0].rel == 2
    assert report.per_question == {question.id: {"total": 3, "visible": 2}}


def test_visibility_reports_no_visible_relevant_and_missing_documents() -> None:
    hidden = _doc(
        "hidden",
        "본문",
        access=Access("restricted", ("security",), 4),
    )
    no_visible = _question(Qrel("hidden", 2), qid="q-hidden")
    missing = _question(Qrel("missing", 2), qid="q-missing")

    failures = check_asker_visibility([no_visible, missing], {"hidden": hidden})

    assert ("q-hidden", None, "no_visible_relevant") in {
        (failure.question_id, failure.doc_id, failure.reason) for failure in failures
    }
    assert ("q-missing", "missing", "missing_doc") in {
        (failure.question_id, failure.doc_id, failure.reason) for failure in failures
    }


def test_qrel_answer_ranges_maps_synthetic_articles_and_annexes() -> None:
    synthetic = _doc("syn-1", "0123456789")
    article_text = "앞부분조문본문별표본문"
    real = _doc(
        "law-1",
        article_text,
        source="real",
        sections=(
            Section("article", "제1조", ("규정", "제1조"), 3, 7),
            Section("article", "제1조", ("규정", "제1조"), 7, 9),
            Section("annex", "별표2", ("규정", "별표2"), 9, len(article_text)),
        ),
        rule_id="rule-1",
    )
    table_golden = {"tables": [{"id": "table-1", "annex_label": "<별표2>"}]}

    synthetic_question = _question(
        Qrel("syn-1", 2, start=2, end=5),
        Qrel("syn-1", 1),
    )
    real_question = _question(
        Qrel("law-1", 2, article_key="제1조"),
        Qrel("law-1", 2, annex_id="table-1"),
        qid="q-real",
        source="real",
    )

    assert [
        (span.doc_id, span.start, span.end)
        for span in qrel_answer_ranges(
            synthetic_question, {"syn-1": synthetic}, table_golden=table_golden
        )
    ] == [("syn-1", 2, 5)]
    assert [
        (span.doc_id, span.start, span.end)
        for span in qrel_answer_ranges(
            real_question, {"law-1": real}, table_golden=table_golden
        )
    ] == [
        ("law-1", 3, 7),
        ("law-1", 7, 9),
        ("law-1", 9, len(article_text)),
    ]


def test_answer_repeated_uses_only_registered_unicode_normalization() -> None:
    evidence = "가 나,다!"
    first = _doc("syn-1", f"앞{evidence}뒤")
    second = _doc("syn-2", "가나다")
    question = _question(Qrel("syn-1", 2, start=1, end=1 + len(evidence)))

    repeated, hashes = answer_repeated(question, [first, second])

    assert repeated is True
    assert hashes == [hashlib.sha256("가나다".encode()).hexdigest()]


def test_answer_repeated_collapses_versions_of_one_real_rule() -> None:
    text = "제1조 같은근거문장열글자이상"
    sections = (Section("article", "제1조", ("규정", "제1조"), 0, len(text)),)
    current = _doc(
        "law-current", text, source="real", sections=sections, rule_id="same-rule"
    )
    prior = _doc(
        "law-prior", text, source="real", sections=sections, rule_id="same-rule"
    )
    question = _question(Qrel("law-current", 2, article_key="제1조"), source="real")

    repeated, hashes = answer_repeated(question, [current, prior])

    assert repeated is False
    assert len(hashes) == 1


def test_answer_repeated_without_evidence_is_untaggable() -> None:
    question = _question(Qrel("syn-1", 2))

    assert answer_repeated(question, [_doc("syn-1", "본문")]) == (False, [])


def test_answer_ranges_by_doc_uses_condition_specific_sections_and_rel_positive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    question = _question(
        Qrel("law-1", 2, article_key="제1조"),
        Qrel("ignored", 0, start=0, end=1),
        source="real",
    )
    starts = {"composite": 2, "pypdf": 4, "upstage": 6}

    def fake_load_corpus(condition: str, *, root: Path):
        start = starts[condition]
        document = _doc(
            "law-1",
            "x" * 20,
            source="real",
            sections=(Section("article", "제1조", ("규정",), start, start + 3),),
            rule_id="rule-1",
        )
        return [document, _doc("ignored", "x")], object()

    monkeypatch.setattr("kodocs.eval.m5_inputs.load_corpus", fake_load_corpus)
    monkeypatch.setattr(
        "kodocs.eval.m5_inputs.load_retrieval_targets",
        lambda root: {
            "gold_dev": [question],
            "gold_test": [],
            "silver_dev": [],
            "silver_test": [],
        },
    )
    monkeypatch.setattr(
        "kodocs.eval.m5_inputs._load_table_golden",
        lambda root: {"tables": []},
    )

    assert answer_ranges_by_doc("pypdf", tmp_path) == {
        "gold": {"law-1": [(4, 7)]},
        "silver": {},
    }
    assert answer_ranges_by_doc("upstage", tmp_path)["gold"]["law-1"] == [(6, 9)]


def test_answer_ranges_by_doc_signature_matches_index_build_lazy_call() -> None:
    signature = inspect.signature(answer_ranges_by_doc)

    assert list(signature.parameters) == ["condition", "root"]
    assert signature.parameters["root"].default == Path(".")
    assert list(inspect.signature(index_build._load_answer_ranges).parameters) == [
        "condition",
        "root",
    ]
