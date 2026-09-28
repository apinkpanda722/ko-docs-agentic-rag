"""Deterministic synthetic golden-question specification tests."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import MappingProxyType

import pytest

from kodocs.eval import golden_specs
from kodocs.eval.golden import (
    GOLD_QUOTA,
    SILVER_QUOTA,
    load_questions,
    validate_question,
)
from kodocs.synth.seed import Fact, Person, Seed, Service

ROOT = Path(__file__).resolve().parents[2]


def _document(
    doc_id: str,
    facts: list[tuple[str, str]],
    *,
    access: dict[str, object] | None = None,
    canary: dict[str, object] | None = None,
    injection: dict[str, object] | None = None,
) -> dict[str, object]:
    body_parts: list[str] = []
    spans: list[dict[str, object]] = []
    cursor = 0
    for fact_id, text in facts:
        if body_parts:
            body_parts.append(" / ")
            cursor += 3
        start = cursor
        body_parts.append(text)
        cursor += len(text)
        spans.append({"fact_id": fact_id, "start": start, "end": cursor})
    return {
        "doc_id": doc_id,
        "access": access or {"level": "public", "teams": [], "min_grade": 1},
        "body": "".join(body_parts),
        "fact_spans": spans,
        "canary": canary,
        "injection": injection,
    }


@pytest.fixture
def tiny_seed() -> Seed:
    people = (
        Person("p-001", "가온", "platform", 1, "개발", date(2024, 1, 1)),
        Person("p-002", "나래", "security", 5, "보안", date(2024, 1, 1)),
    )
    facts = {
        "f-service-alpha-policy": Fact(
            "f-service-alpha-policy",
            "text",
            "배포 전 승인을 받는다.",
            "alpha-api",
            None,
            "normal",
            "fixture",
        ),
        "f-inc-id": Fact(
            "f-inc-id",
            "id",
            "INC-2026-001",
            "INC-2026-001",
            date(2026, 1, 10),
            "normal",
            "fixture",
        ),
        "f-inc-service": Fact(
            "f-inc-service",
            "service",
            "alpha-api",
            "INC-2026-001",
            date(2026, 1, 10),
            "normal",
            "fixture",
        ),
        "f-inc-cause": Fact(
            "f-inc-cause",
            "text",
            "잠금 경합이 원인이었다.",
            "INC-2026-001",
            date(2026, 1, 10),
            "normal",
            "fixture",
        ),
        "f-inc-action": Fact(
            "f-inc-action",
            "text",
            "잠금 범위를 줄였다.",
            "INC-2026-001",
            date(2026, 1, 10),
            "normal",
            "fixture",
        ),
        "f-evt-old-plan": Fact(
            "f-evt-old-plan",
            "text",
            "구형 절차를 적용했다.",
            "evt-old",
            date(2026, 2, 1),
            "normal",
            "fixture",
        ),
        "f-evt-old-owner": Fact(
            "f-evt-old-owner",
            "person",
            "p-001",
            "evt-old",
            date(2026, 2, 1),
            "normal",
            "fixture",
        ),
        "f-evt-old-service": Fact(
            "f-evt-old-service",
            "service",
            "alpha-api",
            "evt-old",
            date(2026, 2, 1),
            "normal",
            "fixture",
        ),
        "f-evt-new-plan": Fact(
            "f-evt-new-plan",
            "text",
            "신형 절차를 적용했다.",
            "evt-new",
            date(2026, 4, 1),
            "normal",
            "fixture",
        ),
        "f-evt-new-owner": Fact(
            "f-evt-new-owner",
            "person",
            "p-002",
            "evt-new",
            date(2026, 4, 1),
            "normal",
            "fixture",
        ),
        "f-evt-new-service": Fact(
            "f-evt-new-service",
            "service",
            "alpha-api",
            "evt-new",
            date(2026, 4, 1),
            "normal",
            "fixture",
        ),
    }
    service = Service(
        "alpha-api",
        "platform",
        "fixture service",
        (facts["f-service-alpha-policy"],),
    )
    return Seed(
        company={
            "random_seed": 7,
            "period": {"start": "2025-01-01", "end": "2026-06-30"},
        },
        people=people,
        services=(service,),
        facts=MappingProxyType(facts),
        entities=MappingProxyType(
            {
                "INC-2026-001": {
                    "id": "INC-2026-001",
                    "date": "2026-01-10",
                    "service": "alpha-api",
                    "owner_team": "platform",
                },
                "evt-old": {
                    "id": "evt-old",
                    "date": "2026-02-01",
                    "service": "alpha-api",
                    "owner_team": "platform",
                },
                "evt-new": {
                    "id": "evt-new",
                    "date": "2026-04-01",
                    "service": "alpha-api",
                    "owner_team": "security",
                },
            }
        ),
        regulation_refs=(),
        banned_names=(),
        ledger_rules={},
    )


@pytest.fixture
def tiny_docs() -> dict[str, dict[str, object]]:
    return {
        "doc-public-a": _document(
            "doc-public-a",
            [
                ("f-inc-id", "INC-2026-001"),
                ("f-inc-cause", "잠금 경합이 원인이었다."),
                ("f-service-alpha-policy", "배포 전 승인을 받는다."),
                ("f-evt-old-plan", "구형 절차를 적용했다."),
                ("f-evt-old-owner", "가온"),
                ("f-evt-old-service", "alpha-api"),
            ],
        ),
        "doc-public-b": _document(
            "doc-public-b",
            [
                ("f-inc-action", "잠금 범위를 줄였다."),
                ("f-evt-new-plan", "신형 절차를 적용했다."),
                ("f-evt-new-owner", "나래"),
                ("f-evt-new-service", "alpha-api"),
            ],
            injection={"injection_id": "inj-fixture"},
        ),
        "doc-public-c": _document(
            "doc-public-c",
            [("f-inc-id", "INC-2026-001")],
        ),
        "doc-restricted": _document(
            "doc-restricted",
            [("f-canary-fixture", "검은고래-0001")],
            access={"level": "restricted", "teams": ["security"], "min_grade": 4},
            canary={
                "canary_id": "cn-fixture",
                "fact_id": "f-canary-fixture",
                "value": "검은고래-0001",
                "start": 0,
                "end": len("검은고래-0001"),
            },
        ),
        "doc-unmarked": _document("doc-unmarked", [])
        | {"body": "INC-2026-001이 다시 언급되었다."},
    }


def test_fact_index_qrels_and_unmarked_mentions_cover_all_spans(
    tiny_seed: Seed, tiny_docs: dict[str, dict[str, object]]
) -> None:
    index = golden_specs.fact_index(tiny_docs)
    qrels = golden_specs.build_qrels(
        ["f-inc-id"], seed=tiny_seed, docs=tiny_docs, index=index
    )

    assert index["f-inc-id"] == [
        ("doc-public-a", 0, len("INC-2026-001")),
        ("doc-public-c", 0, len("INC-2026-001")),
    ]
    assert {(q.doc_id, q.rel, q.fact_id) for q in qrels} >= {
        ("doc-public-a", 2, "f-inc-id"),
        ("doc-public-c", 2, "f-inc-id"),
        ("doc-public-b", 1, "f-inc-action"),
    }
    assert golden_specs.unmarked_mentions(
        tiny_seed.facts["f-inc-id"],
        tiny_docs,
        {person.person_id: person for person in tiny_seed.people},
        {doc_id for doc_id, _, _ in index["f-inc-id"]},
    ) == ["doc-unmarked"]


@pytest.mark.parametrize(
    ("question_type", "assertion"),
    [
        ("identifier", lambda c: c.anchor_values == ("INC-2026-001",)),
        ("semantic", lambda c: c.answer_fact_ids == ("f-service-alpha-policy",)),
        (
            "multi_doc",
            lambda c: (
                len(c.answer_fact_ids) >= 2
                and not any(
                    set(c.answer_fact_ids).issubset(facts)
                    for facts in c.document_fact_ids.values()
                )
            ),
        ),
        (
            "temporal",
            lambda c: (
                c.as_of is not None
                and "temporal:as_of_event" in c.tags
                and c.temporal_candidate_count >= 2
            ),
        ),
        (
            "permission",
            lambda c: (
                c.expected_response == "indistinguishable_from_no_answer"
                and "canary" in c.tags
                and any(qrel.rel == 2 for qrel in c.qrels)
            ),
        ),
        (
            "unanswerable",
            lambda c: c.no_answer_checks == {"seed": True, "docs": True, "real": None},
        ),
    ],
)
def test_fixture_candidate_generation_by_type(
    question_type: str,
    assertion: object,
    tiny_seed: Seed,
    tiny_docs: dict[str, dict[str, object]],
) -> None:
    pools, _ = golden_specs._candidate_pools(
        tiny_seed, tiny_docs, golden_specs.fact_index(tiny_docs)
    )
    candidates = pools[question_type]

    assert candidates
    assert any(assertion(candidate) for candidate in candidates)  # type: ignore[operator]


def test_temporal_fixture_includes_a_discovered_value_replacement(
    tiny_seed: Seed, tiny_docs: dict[str, dict[str, object]]
) -> None:
    pools, _ = golden_specs._candidate_pools(
        tiny_seed, tiny_docs, golden_specs.fact_index(tiny_docs)
    )
    replacement = next(
        candidate
        for candidate in pools["temporal"]
        if candidate.temporal_mode == "replacement"
    )

    assert (
        date(2026, 2, 1)
        < date.fromisoformat(replacement.as_of or "")
        < date(2026, 4, 1)
    )
    assert replacement.answer_fact_ids == ("f-evt-old-owner",)


def test_multi_doc_rejects_a_pair_contained_by_one_document(
    tiny_seed: Seed, tiny_docs: dict[str, dict[str, object]]
) -> None:
    together = _document(
        "doc-together",
        [
            ("f-inc-cause", "잠금 경합이 원인이었다."),
            ("f-inc-action", "잠금 범위를 줄였다."),
        ],
    )
    docs = {**tiny_docs, "doc-together": together}
    pools, rejected = golden_specs._candidate_pools(
        tiny_seed, docs, golden_specs.fact_index(docs)
    )

    assert not any(
        set(candidate.answer_fact_ids) == {"f-inc-cause", "f-inc-action"}
        for candidate in pools["multi_doc"]
    )
    assert rejected["multi_doc_one_document_has_all_facts"] >= 1


def test_sql_candidates_execute_six_fixed_aggregate_queries() -> None:
    rows = {
        "tickets": [
            {
                "owner_team": "platform",
                "status": "closed",
                "created_at": "2026-01-02T10:00:00+09:00",
            },
            {
                "owner_team": "platform",
                "status": "open",
                "created_at": "2026-02-03T10:00:00+09:00",
            },
        ],
        "approvals": [
            {
                "owner_team": "finance",
                "status": "approved",
                "submitted_at": "2026-01-04T10:00:00+09:00",
                "amount_krw": 300,
            }
        ],
    }

    candidates = golden_specs._sql_candidates(rows, "db-hash")

    assert len(candidates) == 6
    assert all(candidate.sql is not None for candidate in candidates)
    assert candidates[0].sql is not None
    assert candidates[0].sql.db_content_hash == "db-hash"
    assert candidates[0].sql.expected


def test_allocation_shortage_fails_instead_of_substituting_types() -> None:
    with pytest.raises(golden_specs.SpecBuildError, match="semantic.*1.*2"):
        golden_specs._take_candidates("silver", "semantic", [object()], 2)


def test_actual_repository_specs_meet_quotas_validate_and_are_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    specs_path = tmp_path / "specs.jsonl"
    summary_path = tmp_path / "golden_specs.json"
    monkeypatch.setattr(golden_specs, "SPECS_PATH", specs_path)
    monkeypatch.setattr(golden_specs, "SUMMARY_PATH", summary_path)

    assert golden_specs.run(ROOT, print_fn=lambda _: None) == 0
    first_specs = specs_path.read_bytes()
    first_summary = summary_path.read_bytes()
    assert golden_specs.run(ROOT, print_fn=lambda _: None) == 0
    assert specs_path.read_bytes() == first_specs
    assert summary_path.read_bytes() == first_summary

    questions = load_questions(specs_path)
    docs = {
        payload["doc_id"]: payload
        for path in sorted((ROOT / "data/synthetic/docs").glob("*.json"))
        if (payload := json.loads(path.read_text(encoding="utf-8")))
    }
    counts = Counter((question.set, question.type) for question in questions)
    assert len(questions) == 180
    assert {
        question_type: counts[("gold", question_type)]
        for question_type, source in GOLD_QUOTA
        if source == "synthetic" and counts[("gold", question_type)]
    } == {
        question_type: quota
        for (question_type, source), quota in GOLD_QUOTA.items()
        if source == "synthetic" and quota
    }
    assert {
        question_type: counts[("silver", question_type)]
        for question_type in SILVER_QUOTA
    } == SILVER_QUOTA
    assert all(
        not validate_question(
            replace(question, question="명세 단계 검증용 질문"),
            docs=docs,
            regulation_index={},
            table_ids=set(),
        )
        for question in questions
    )

    gold_facts = {
        qrel.fact_id
        for question in questions
        if question.set == "gold"
        for qrel in question.qrels
        if qrel.rel == 2
    }
    silver_facts = {
        qrel.fact_id
        for question in questions
        if question.set == "silver"
        for qrel in question.qrels
        if qrel.rel == 2
    }
    assert gold_facts.isdisjoint(silver_facts)

    temporal = [question for question in questions if question.type == "temporal"]
    assert len(temporal) == 20
    assert all("temporal:as_of_event" in question.tags for question in temporal)
    seed = golden_specs.load_seed(ROOT)
    temporal_answer_facts = {
        qrel.fact_id: seed.facts[qrel.fact_id]
        for question in temporal
        for qrel in question.qrels
        if qrel.rel == 2 and qrel.fact_id in seed.facts
    }
    temporal_answer_ids = set(temporal_answer_facts)
    assert sum(fact.kind == "person" for fact in temporal_answer_facts.values()) == 1
    assert "f-inc-2026-004-owner" in temporal_answer_ids
    replacement = next(
        question
        for question in temporal
        if any(qrel.fact_id == "f-inc-2026-004-owner" for qrel in question.qrels)
    )
    assert replacement.as_of == "2026-04-18"

    document_facts = {
        doc_id: {span["fact_id"] for span in document["fact_spans"]}
        for doc_id, document in docs.items()
    }
    for question in (q for q in questions if q.type == "multi_doc"):
        answer_facts = {qrel.fact_id for qrel in question.qrels if qrel.rel == 2}
        assert not any(
            answer_facts.issubset(marked_facts)
            for marked_facts in document_facts.values()
        )

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    unanswerable = [q for q in questions if q.type == "unanswerable"]
    assert all(
        question.no_answer_checks == {"seed": True, "docs": True, "real": None}
        for question in unanswerable
    )
    raw_rows = [json.loads(line) for line in first_specs.decode().splitlines()]
    assert all("unmarked_candidates" in row for row in raw_rows)
    assert (
        sum(bool(question.unmarked_candidates) for question in questions)
        == summary["unmarked_candidate_questions"]
    )
    assert set(summary["candidates"]) == {"gold", "silver"}
    assert summary["rejected_candidates"]
