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
    question_to_dict,
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


def test_temporal_candidates_are_all_as_of_event_questions(
    tiny_seed: Seed, tiny_docs: dict[str, dict[str, object]]
) -> None:
    # The seed has no service-level value change; "value replacement" was a
    # miscount of two incidents' owners grouped by service (ADR-025 correction).
    pools, _ = golden_specs._candidate_pools(
        tiny_seed, tiny_docs, golden_specs.fact_index(tiny_docs)
    )

    assert pools["temporal"]
    assert all(c.temporal_mode == "event" for c in pools["temporal"])
    assert all("temporal:as_of_event" in c.tags for c in pools["temporal"])
    assert not any("temporal:revision" in c.tags for c in pools["temporal"])


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


def _cand(key: str, subjects: tuple[str, ...], service: str = "svc-a"):
    return golden_specs._Candidate(
        type="semantic",
        anchor_values=(key,),
        answer_values=(),
        answer_fact_ids=(f"f-{key}",),
        qrels=(),
        reference_answer=None,
        subjects=subjects,
        service=service,
        discriminator=key,
    )


def _select(candidates, quota, *, type_cap=1, set_cap=2, counts=None, avoid=()):
    return golden_specs._select_diverse(
        candidates,
        quota,
        set_name="gold",
        question_type="semantic",
        seed_key="7:gold:semantic",
        type_cap=type_cap,
        set_cap=set_cap,
        set_counts=Counter() if counts is None else counts,
        avoid=frozenset(avoid),
    )


def test_diverse_selection_caps_questions_per_subject_within_a_type() -> None:
    candidates = [_cand(f"a{i}", ("INC-A",)) for i in range(5)] + [
        _cand("b0", ("INC-B",), "svc-b"),
        _cand("c0", ("INC-C",), "svc-c"),
    ]

    chosen = _select(candidates, 3, type_cap=1)

    assert Counter(s for c in chosen for s in c.subjects) == {
        "INC-A": 1,
        "INC-B": 1,
        "INC-C": 1,
    }


def test_set_cap_counts_every_subject_of_a_multi_doc_answer() -> None:
    counts = Counter({"INC-A": 1})
    pair = _cand("pair", ("INC-A", "INC-B"))
    lone = _cand("lone", ("INC-C",), "svc-c")

    chosen = _select([pair, lone], 2, type_cap=2, set_cap=2, counts=counts)

    assert {c.discriminator for c in chosen} == {"pair", "lone"}
    assert counts == {"INC-A": 2, "INC-B": 1, "INC-C": 1}
    with pytest.raises(golden_specs.SpecBuildError):
        _select([_cand("again", ("INC-A",))], 1, type_cap=2, set_cap=2, counts=counts)


def test_diverse_selection_is_a_deterministic_shuffle_not_a_prefix() -> None:
    candidates = [
        _cand(f"k{i:02d}", (f"INC-{i:02d}",), f"svc-{i % 4}") for i in range(40)
    ]

    first = [c.discriminator for c in _select(candidates, 10)]
    second = [c.discriminator for c in _select(candidates, 10)]

    assert first == second
    assert first != [f"k{i:02d}" for i in range(10)]


def test_diverse_selection_prefers_subjects_the_gold_set_did_not_use() -> None:
    candidates = [_cand("gold-used", ("INC-G",)), _cand("fresh", ("INC-F",), "svc-f")]

    assert [c.discriminator for c in _select(candidates, 1, avoid=("INC-G",))] == [
        "fresh"
    ]
    # Falls back to an avoided subject rather than failing when it must.
    assert len(_select(candidates, 2, avoid=("INC-G",))) == 2


def test_diverse_selection_shortage_fails_with_the_achievable_maximum() -> None:
    candidates = [_cand(f"a{i}", ("INC-A",)) for i in range(5)]

    with pytest.raises(golden_specs.SpecBuildError, match="semantic.*max=1.*quota=3"):
        _select(candidates, 3, type_cap=1)


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
    assert all(
        question.as_of == temporal_answer_facts[qrel.fact_id].date.isoformat()
        for question in temporal
        for qrel in question.qrels
        if qrel.rel == 2 and qrel.fact_id in temporal_answer_facts
    )

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
    caps = {"gold": (1, 2), "silver": (2, 4)}
    for set_name, (type_cap, set_cap) in caps.items():
        per_type = summary["diversity"][set_name]
        assert all(v["max_per_subject"] <= type_cap for v in per_type.values())
        assert summary["diversity_totals"][set_name]["max_per_subject"] <= set_cap
    assert set(summary["gold_silver_overlap"]) == {"subjects", "silver_questions"}
    assert summary["rejected_candidates"]


# ---------------------------------------------------------------------------
# Sync mode: keep the committed 180 selections, recompute only derived fields.
# ---------------------------------------------------------------------------

_PRESERVED = (
    "type",
    "tags",
    "asker",
    "as_of",
    "anchor_values",
    "answer_values",
    "expected_response",
    "sql",
)
_EMPTY_ROWS = {"tickets": [], "approvals": []}


def _pools(seed: Seed, docs: dict[str, dict[str, object]]):
    index = golden_specs.fact_index(docs)
    pools, _ = golden_specs._candidate_pools(seed, docs, index)
    pools["sql"] = golden_specs._sql_candidates(_EMPTY_ROWS, "db-hash")
    return pools


def _spec(seed: Seed, docs, *, qid: str, set_name: str, qtype: str, pick) -> object:
    """Build one existing spec from the first pool candidate matching ``pick``."""
    people = tuple(sorted(seed.people, key=lambda item: item.person_id))
    candidate = next(c for c in _pools(seed, docs)[qtype] if pick(c))
    candidate = golden_specs._annotate_subjects(candidate, seed, docs)
    asker = golden_specs._choose_asker(
        candidate, people, docs, random_seed=7, ordinal=len(qid)
    )
    return golden_specs._question_from_candidate(
        candidate, qid=qid, set_name=set_name, asker=asker
    )


def _sync(existing, seed: Seed, docs):
    return golden_specs.sync_specs(
        existing,
        seed,
        docs,
        db_rows=_EMPTY_ROWS,
        db_content_hash="db-hash",
        random_seed=7,
    )


def _fixed_fields(question) -> str:
    payload = question_to_dict(question)
    return json.dumps({k: payload[k] for k in _PRESERVED}, sort_keys=True)


def _silver_specs(seed: Seed, docs) -> list:
    return [
        _spec(
            seed,
            docs,
            qid="s-syn-001",
            set_name="silver",
            qtype="identifier",
            pick=lambda c: c.answer_fact_ids == ("f-inc-cause",),
        ),
        _spec(
            seed,
            docs,
            qid="s-syn-002",
            set_name="silver",
            qtype="semantic",
            pick=lambda c: c.answer_fact_ids == ("f-service-alpha-policy",),
        ),
        _spec(
            seed,
            docs,
            qid="s-syn-003",
            set_name="silver",
            qtype="temporal",
            pick=lambda c: c.as_of == "2026-02-01",
        ),
        _spec(
            seed,
            docs,
            qid="s-syn-004",
            set_name="silver",
            qtype="permission",
            pick=lambda c: True,
        ),
        _spec(
            seed,
            docs,
            qid="s-syn-005",
            set_name="silver",
            qtype="unanswerable",
            pick=lambda c: c.anchor_values == ("INC-2026-001",),
        ),
    ]


def test_sync_adding_a_document_keeps_question_defining_fields_byte_identical(
    tiny_seed: Seed, tiny_docs: dict[str, dict[str, object]]
) -> None:
    existing = _silver_specs(tiny_seed, tiny_docs)
    docs = {
        **tiny_docs,
        "doc-new": _document("doc-new", [("f-inc-cause", "잠금 경합이 원인이었다.")]),
    }

    result = _sync(existing, tiny_seed, docs)

    assert result.replaced == {}
    assert [q.id for q in result.questions] == [q.id for q in existing]
    before = {q.id: _fixed_fields(q) for q in existing}
    assert {q.id: _fixed_fields(q) for q in result.questions} == before
    # Only the question whose answer fact gained a span changes its qrels.
    assert result.qrels_changed == ("s-syn-001",)
    changed = next(q for q in result.questions if q.id == "s-syn-001")
    assert ("doc-new", 2) in {(x.doc_id, x.rel) for x in changed.qrels}
    for question in result.questions:
        assert not validate_question(
            replace(question, question="명세 단계 빈 질문 허용"),
            docs=docs,
            regulation_index={},
            table_ids=set(),
        )


def test_sync_without_corpus_change_reproduces_the_existing_specs(
    tiny_seed: Seed, tiny_docs: dict[str, dict[str, object]]
) -> None:
    existing = _silver_specs(tiny_seed, tiny_docs)

    result = _sync(existing, tiny_seed, tiny_docs)

    assert result.replaced == {}
    assert result.qrels_changed == ()
    assert result.questions == existing


def test_sync_replaces_only_the_question_that_breaks_a_condition(
    tiny_seed: Seed, tiny_docs: dict[str, dict[str, object]]
) -> None:
    pair = _spec(
        tiny_seed,
        tiny_docs,
        qid="s-syn-006",
        set_name="silver",
        qtype="multi_doc",
        pick=lambda c: set(c.answer_fact_ids) == {"f-inc-cause", "f-inc-action"},
    )
    existing = [*_silver_specs(tiny_seed, tiny_docs), pair]
    docs = {
        **tiny_docs,
        "doc-together": _document(
            "doc-together",
            [
                ("f-inc-cause", "잠금 경합이 원인이었다."),
                ("f-inc-action", "잠금 범위를 줄였다."),
            ],
        ),
    }

    result = _sync(existing, tiny_seed, docs)

    assert set(result.replaced) == {"s-syn-006"}
    assert "multi_doc_single_document" in result.replaced["s-syn-006"]
    by_id = {q.id: q for q in result.questions}
    replacement = by_id["s-syn-006"]
    assert replacement.type == "multi_doc"
    assert {q.rel for q in replacement.qrels} >= {2}
    answer_facts = {x.fact_id for x in replacement.qrels if x.rel == 2}
    assert not any(
        answer_facts <= facts
        for facts in golden_specs._document_fact_ids(docs).values()
    )
    assert answer_facts != {"f-inc-cause", "f-inc-action"}
    survivors = [q for q in existing if q.id != "s-syn-006"]
    assert {q.id: _fixed_fields(by_id[q.id]) for q in survivors} == {
        q.id: _fixed_fields(q) for q in survivors
    }


def test_sync_flags_an_unanswerable_question_that_became_answerable(
    tiny_seed: Seed, tiny_docs: dict[str, dict[str, object]]
) -> None:
    existing = _silver_specs(tiny_seed, tiny_docs)
    docs = {
        **tiny_docs,
        "doc-cost": _document("doc-cost", [])
        | {"body": "INC-2026-001 처리 비용은 영향 사용자 수 기준으로 산정했다."},
    }

    result = _sync(existing, tiny_seed, docs)

    assert set(result.replaced) == {"s-syn-005"}
    assert "unanswerable_now_answerable" in result.replaced["s-syn-005"]
    replacement = next(q for q in result.questions if q.id == "s-syn-005")
    assert replacement.type == "unanswerable"
    assert replacement.anchor_values != ("INC-2026-001",)
    assert replacement.no_answer_checks == {"seed": True, "docs": True, "real": None}


def test_sync_replaces_a_silver_question_that_shares_a_gold_answer_fact(
    tiny_seed: Seed, tiny_docs: dict[str, dict[str, object]]
) -> None:
    gold = _spec(
        tiny_seed,
        tiny_docs,
        qid="g-syn-001",
        set_name="gold",
        qtype="identifier",
        pick=lambda c: c.answer_fact_ids == ("f-inc-cause",),
    )
    silver = _spec(
        tiny_seed,
        tiny_docs,
        qid="s-syn-001",
        set_name="silver",
        qtype="semantic",
        pick=lambda c: c.answer_fact_ids == ("f-inc-cause",),
    )

    result = _sync([gold, silver], tiny_seed, tiny_docs)

    assert set(result.replaced) == {"s-syn-001"}
    assert "silver_shares_gold_fact" in result.replaced["s-syn-001"]
    by_id = {q.id: q for q in result.questions}
    assert by_id["g-syn-001"] == gold
    silver_facts = {x.fact_id for x in by_id["s-syn-001"].qrels if x.rel == 2}
    assert "f-inc-cause" not in silver_facts


def test_sync_is_deterministic_for_the_same_input(
    tiny_seed: Seed, tiny_docs: dict[str, dict[str, object]]
) -> None:
    pair = _spec(
        tiny_seed,
        tiny_docs,
        qid="s-syn-006",
        set_name="silver",
        qtype="multi_doc",
        pick=lambda c: set(c.answer_fact_ids) == {"f-inc-cause", "f-inc-action"},
    )
    existing = [*_silver_specs(tiny_seed, tiny_docs), pair]
    docs = {
        **tiny_docs,
        "doc-together": _document(
            "doc-together",
            [
                ("f-inc-cause", "잠금 경합이 원인이었다."),
                ("f-inc-action", "잠금 범위를 줄였다."),
            ],
        ),
    }

    first = _sync(existing, tiny_seed, docs)
    second = _sync(existing, tiny_seed, docs)

    assert first.questions == second.questions
    assert first.replaced == second.replaced
    assert first.qrels_changed == second.qrels_changed


def test_replacement_order_is_a_stable_per_candidate_hash() -> None:
    a, b, c = _cand("a", ("INC-A",)), _cand("b", ("INC-B",)), _cand("c", ("INC-C",))

    def rank(pool):
        return [x.discriminator for x in sorted(pool, key=golden_specs._rank_key(7))]

    without_c = rank([a, b])
    with_c = [x for x in rank([a, b, c]) if x != "c"]

    assert without_c == with_c


@pytest.mark.parametrize("blocked", ["INC-A", "INC-B"])
def test_replacement_skips_candidates_over_the_diversity_cap(blocked: str) -> None:
    pool = [_cand("a", ("INC-A",)), _cand("b", ("INC-B",), "svc-b")]

    picked = golden_specs._pick_replacement(
        pool,
        set_name="gold",
        question_type="semantic",
        random_seed=7,
        used_keys=set(),
        gold_answer_facts=frozenset(),
        type_counts=Counter({blocked: 1}),
        set_counts=Counter({blocked: 1}),
    )

    assert picked.subjects != (blocked,)


def test_replacement_skips_used_keys_and_reports_a_shortage() -> None:
    only = _cand("only", ("INC-A",))

    with pytest.raises(golden_specs.SpecBuildError, match="replacement"):
        golden_specs._pick_replacement(
            [only],
            set_name="silver",
            question_type="semantic",
            random_seed=7,
            used_keys={only.key},
            gold_answer_facts=frozenset(),
            type_counts=Counter(),
            set_counts=Counter(),
        )


def test_invalidating_drafts_makes_replaced_specs_pending_again(
    tiny_seed: Seed, tiny_docs: dict[str, dict[str, object]], tmp_path: Path
) -> None:
    from types import SimpleNamespace

    from kodocs.eval import query_draft
    from kodocs.eval.golden import write_questions

    kept, replaced = _silver_specs(tiny_seed, tiny_docs)[:2]
    drafted = [
        replace(kept, question="INC-2026-001의 원인은 무엇인가요?"),
        replace(replaced, question="알파 서비스 배포 절차는 무엇인가요?"),
    ]
    validated = tmp_path / query_draft.VALIDATION_PATH
    write_questions(validated, drafted)
    config = SimpleNamespace(max_attempts=3)
    assert query_draft.pending_specs(tmp_path, [kept, replaced], config=config) == []

    removed = golden_specs._invalidate_drafts(tmp_path, {replaced.id})

    assert removed == 1
    assert [q.id for q in load_questions(validated)] == [kept.id]
    pending = query_draft.pending_specs(tmp_path, [kept, replaced], config=config)
    assert [spec.id for spec, _ in pending] == [replaced.id]


def test_invalidating_drafts_without_a_validated_file_is_a_no_op(
    tmp_path: Path,
) -> None:
    assert golden_specs._invalidate_drafts(tmp_path, {"s-syn-001"}) == 0


def test_run_keeps_existing_selections_and_reselect_rebuilds_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    specs_path = tmp_path / "specs.jsonl"
    summary_path = tmp_path / "golden_specs.json"
    monkeypatch.setattr(golden_specs, "SPECS_PATH", specs_path)
    monkeypatch.setattr(golden_specs, "SUMMARY_PATH", summary_path)
    assert golden_specs.run(ROOT, print_fn=lambda _: None) == 0
    original = specs_path.read_bytes()

    from kodocs.eval.golden import write_questions

    edited = [
        replace(q, tags=(*q.tags, "lexical_mismatch")) if q.id == "g-syn-001" else q
        for q in load_questions(specs_path)
    ]
    write_questions(specs_path, edited)
    edited_bytes = specs_path.read_bytes()

    assert golden_specs.run(ROOT, print_fn=lambda _: None) == 0
    assert specs_path.read_bytes() == edited_bytes

    assert golden_specs.run(ROOT, reselect=True, print_fn=lambda _: None) == 0
    assert specs_path.read_bytes() == original


def test_sync_names_the_ambiguity_when_a_semantic_subject_gains_a_twin_fact(
    tiny_seed: Seed, tiny_docs: dict[str, dict[str, object]]
) -> None:
    existing = [
        _spec(
            tiny_seed,
            tiny_docs,
            qid="s-syn-001",
            set_name="silver",
            qtype="semantic",
            pick=lambda c: c.answer_fact_ids == ("f-inc-cause",),
        )
    ]
    twin = Fact(
        "f-inc2-cause",
        "text",
        "디스크 포화가 원인이었다.",
        "INC-2026-002",
        date(2026, 3, 1),
        "normal",
        "fixture",
    )
    seed = replace(
        tiny_seed,
        facts=MappingProxyType({**tiny_seed.facts, twin.fact_id: twin}),
        entities=MappingProxyType(
            {
                **tiny_seed.entities,
                "INC-2026-002": {
                    "id": "INC-2026-002",
                    "date": "2026-03-01",
                    "service": "alpha-api",
                    "owner_team": "platform",
                },
            }
        ),
    )
    docs = {
        **tiny_docs,
        "doc-twin": _document(
            "doc-twin", [("f-inc2-cause", "디스크 포화가 원인이었다.")]
        ),
    }

    result = _sync(existing, seed, docs)

    assert result.replaced == {"s-syn-001": ("semantic_ambiguous_subject_attribute",)}
    assert result.questions[0].type == "semantic"
    assert result.questions[0].id == "s-syn-001"
    assert result.questions[0].answer_values != existing[0].answer_values
