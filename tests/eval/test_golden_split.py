"""Deterministic, leakage-aware golden dev/test split contracts."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from kodocs.eval.golden import GOLD_SPLIT_QUOTA, Asker, Qrel, Question
from kodocs.eval.golden_split import assign_split, question_groups, split_report


def _question(
    question_id: str,
    question_type: str,
    *,
    source: str = "synthetic",
    question_set: str = "gold",
    docs: tuple[str, ...] | None = None,
    tags: tuple[str, ...] = (),
) -> Question:
    if docs is None:
        docs = (f"doc-{question_id}",)
    qrels = tuple(Qrel(doc_id=doc_id, rel=2) for doc_id in docs)
    return Question(
        id=question_id,
        set=question_set,
        source=source,
        type=question_type,
        tags=tags,
        question=f"{question_id} 질문은 무엇인가?",
        asker=Asker(team="platform", grade=3),
        as_of=None,
        qrels=qrels,
        anchor_values=(),
        answer_values=(),
        expected_response="answer",
        sql=None,
        reference_answer=None,
        reference_sha256=None,
        summary80=None,
        split=None,
    )


def _gold_fixture() -> list[Question]:
    questions: list[Question] = []
    ordinal = 0
    for question_type in GOLD_SPLIT_QUOTA["test"]:
        count = (
            GOLD_SPLIT_QUOTA["test"][question_type]
            + GOLD_SPLIT_QUOTA["dev"][question_type]
        )
        for _ in range(count):
            ordinal += 1
            docs = () if question_type == "sql" else None
            questions.append(
                _question(
                    f"g-fixture-{ordinal:03d}",
                    question_type,
                    docs=docs,
                )
            )
    return questions


def test_relevance_two_shared_document_stays_in_one_split() -> None:
    questions = [
        _question("g-shared-1", "semantic", docs=("shared-doc",)),
        _question("g-shared-2", "semantic", docs=("shared-doc",)),
        *[_question(f"g-single-{number}", "semantic") for number in range(1, 9)],
    ]

    assert ["g-shared-1", "g-shared-2"] in question_groups(questions)
    assignment, _ = assign_split(
        questions,
        {"test": {"semantic": 5}, "dev": {"semantic": 5}},
        random_seed=71,
    )

    assert assignment["g-shared-1"] == assignment["g-shared-2"]


def test_gold_fixture_meets_every_type_quota_exactly() -> None:
    assignment, report = assign_split(
        _gold_fixture(), GOLD_SPLIT_QUOTA, random_seed=20250101 + 404
    )

    assert Counter(assignment.values()) == {"test": 30, "dev": 20}
    assert all(
        difference == 0
        for by_type in report["quota_diff"].values()
        for difference in by_type.values()
    )
    assert report["warnings"] == []


def test_oversized_group_is_split_at_a_reported_leakage_boundary() -> None:
    questions = [
        *[
            _question(f"g-large-{number}", "semantic", docs=("shared-large",))
            for number in range(1, 7)
        ],
        *[
            _question(f"g-rest-{number}", "semantic", docs=("shared-rest",))
            for number in range(1, 5)
        ],
    ]

    assignment, report = assign_split(
        questions,
        {"test": {"semantic": 5}, "dev": {"semantic": 5}},
        random_seed=19,
    )

    assert Counter(assignment.values()) == {"test": 5, "dev": 5}
    assert report["quota_diff"] == {
        "test": {"semantic": 0},
        "dev": {"semantic": 0},
    }
    assert report["warnings"]
    assert report["cross_split_docs"] == {"n": 1, "doc_ids": ["shared-large"]}


def test_unsplittable_groups_choose_nearest_quota_and_warn() -> None:
    questions = [
        *[
            _question(
                f"g-group-{group}-{number}",
                "semantic",
                docs=(f"shared-{group}",),
            )
            for group in range(1, 4)
            for number in range(1, 4)
        ],
        _question("g-single", "semantic"),
    ]

    assignment, report = assign_split(
        questions,
        {"test": {"semantic": 5}, "dev": {"semantic": 5}},
        random_seed=23,
    )

    assert abs(Counter(assignment.values())["test"] - 5) == 1
    assert abs(report["quota_diff"]["test"]["semantic"]) == 1
    assert (
        report["quota_diff"]["dev"]["semantic"]
        == -report["quota_diff"]["test"]["semantic"]
    )
    assert any(warning.startswith("quota_not_exact:") for warning in report["warnings"])


def test_hypothesis_counts_use_the_declared_type_sets() -> None:
    questions = [
        _question("q-identifier-synthetic", "identifier"),
        _question("q-semantic-real", "semantic", source="real", docs=("law-1",)),
        _question("q-table-real", "table", source="real", docs=("law-2",)),
        _question("q-multi-real", "multi_doc", source="real", docs=("law-3",)),
        _question(
            "q-temporal-real",
            "temporal",
            source="real",
            docs=("law-4",),
            tags=("temporal:revision",),
        ),
        _question("q-sql-synthetic", "sql", docs=()),
        _question("q-permission-synthetic", "permission"),
        _question("q-unanswerable-synthetic", "unanswerable", docs=()),
        _question(
            "q-temporal-synthetic-dev",
            "temporal",
            question_set="silver",
            tags=("temporal:as_of_event",),
        ),
    ]
    assignment = {question.id: "test" for question in questions}
    assignment["q-temporal-synthetic-dev"] = "dev"

    report = split_report(questions, assignment)

    # Retrieval: identifier, semantic, table, multi_doc, temporal with qrels.
    assert report["retrieval_test_n"] == 5
    # H5: identifier + sql + multi_doc, independently of qrel presence.
    assert report["h5_bundle_test_n"] == 3
    # H7: identifier, semantic, multi_doc, split by source.
    assert report["h7"] == {"synthetic_n": 1, "real_n": 2}
    assert report["temporal_tags"] == {
        "temporal:revision": {"test": 1, "dev": 0},
        "temporal:as_of_event": {"test": 0, "dev": 1},
    }


def test_attention_check_is_rejected_before_split() -> None:
    questions = [
        _question("g-normal", "semantic"),
        _question("chk-deliberately-wrong", "semantic"),
    ]

    with pytest.raises(ValueError, match="chk-"):
        assign_split(questions, None, random_seed=1)


def test_same_input_and_seed_have_byte_identical_assignment_and_report() -> None:
    questions = _gold_fixture()

    first_assignment, first_report = assign_split(
        questions, GOLD_SPLIT_QUOTA, random_seed=20250101 + 404
    )
    second_assignment, second_report = assign_split(
        questions, GOLD_SPLIT_QUOTA, random_seed=20250101 + 404
    )

    first = json.dumps(
        [first_assignment, first_report],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    second = json.dumps(
        [second_assignment, second_report],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert first == second


def test_relevance_one_document_does_not_join_groups() -> None:
    shared_rel_one = Qrel(doc_id="background", rel=1)
    questions = [
        replace(
            _question("g-rel-one-1", "semantic"),
            qrels=(_question("unused-1", "semantic").qrels[0], shared_rel_one),
        ),
        replace(
            _question("g-rel-one-2", "semantic"),
            qrels=(_question("unused-2", "semantic").qrels[0], shared_rel_one),
        ),
    ]

    assert question_groups(questions) == [["g-rel-one-1"], ["g-rel-one-2"]]


def test_real_versions_with_the_same_rule_id_are_one_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refs = tmp_path / "data/manifest/regulation_refs.json"
    refs.parent.mkdir(parents=True)
    refs.write_text(
        json.dumps(
            {
                "rules": {
                    "가짜 규정": {
                        "rule_id": "rule-one",
                        "serial": "101",
                        "articles": {},
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    old_law = tmp_path / "data/interim/law/rule-one/100.json"
    old_law.parent.mkdir(parents=True)
    old_law.write_text("{}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    questions = [
        _question("g-real-old", "temporal", source="real", docs=("law-100",)),
        _question("g-real-current", "semantic", source="real", docs=("law-101",)),
    ]

    assert question_groups(questions) == [["g-real-current", "g-real-old"]]
