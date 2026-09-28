"""Contracts for the deterministic synthetic-document generation plan."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from kodocs.synth.db import build_rows
from kodocs.synth.identifiers import INCIDENT_RE
from kodocs.synth.injection_texts import INJECTION_TEXTS
from kodocs.synth.plan import (
    ACCESS_COUNTS,
    CANARIES_PATH,
    DOC_TYPES,
    INJECTION_COUNTS,
    INJECTIONS_PATH,
    PLAN_PATH,
    TYPE_COUNTS,
    build_plan,
    incident_age_summary,
    time_phrase,
    write_plan,
)
from kodocs.synth.seed import PERIOD_END, TEAMS, SeedError, load_seed

REPO_ROOT = Path(__file__).parents[2]
MIN_ROOT = Path(__file__).parent / "fixtures/seed_min"
FACT_COUNT_RANGES = {
    "meeting": (4, 8),
    "weekly": (5, 8),
    "incident": (8, 12),
    "runbook": (4, 7),
    "guide": (3, 6),
    "notice": (2, 5),
}
DOC_ID_RE = re.compile(r"^[a-z0-9-]{1,40}$")
CANARY_RE = re.compile(r"^[가-힣]+-[0-9]{4}$")


@pytest.fixture(scope="module")
def built():
    seed = load_seed(REPO_ROOT)
    plans, canaries, injections = build_plan(seed)
    return seed, plans, canaries, injections


def _record(plan) -> dict[str, object]:
    return json.loads(json.dumps(asdict(plan), ensure_ascii=False))


def test_build_plan_is_deterministic_and_has_exact_distributions(built) -> None:
    seed, plans, canaries, injections = built

    assert build_plan(seed) == (plans, canaries, injections)
    assert len(plans) == 200
    assert Counter(plan.doc_type for plan in plans) == TYPE_COUNTS
    assert Counter(plan.access.level for plan in plans) == ACCESS_COUNTS
    assert [plan.doc_type for plan in plans[: TYPE_COUNTS["meeting"]]] == [
        "meeting"
    ] * TYPE_COUNTS["meeting"]
    assert all(plan.doc_type in DOC_TYPES for plan in plans)
    assert len({plan.doc_id for plan in plans}) == len(plans)
    assert all(DOC_ID_RE.fullmatch(plan.doc_id) for plan in plans)
    for doc_type in DOC_TYPES:
        ids = [plan.doc_id for plan in plans if plan.doc_type == doc_type]
        assert ids == [
            f"syn-{doc_type}-{number:03d}"
            for number in range(1, TYPE_COUNTS[doc_type] + 1)
        ]
    team_counts = {
        team: sum(
            plan.team == team and plan.access.level in {"public", "team"}
            for plan in plans
        )
        for team in TEAMS
    }
    assert all(team_counts[team] >= 10 for team in TEAMS)
    for plan in plans:
        expected_seed = int(
            hashlib.sha256(
                f"{seed.company['random_seed']}:{plan.doc_id}".encode()
            ).hexdigest()[:8],
            16,
        )
        assert plan.seed == expected_seed


def test_fact_ranges_dates_authors_and_normal_fact_coverage(built) -> None:
    seed, plans, _, _ = built
    people = {person.person_id: person for person in seed.people}
    covered_normal: set[str] = set()

    for plan in plans:
        assigned_seed_facts = [
            seed.facts[fact_id] for fact_id in plan.fact_ids if fact_id in seed.facts
        ]
        count_without_canary = len(plan.fact_ids) - (plan.canary_fact_id is not None)
        minimum, maximum = FACT_COUNT_RANGES[plan.doc_type]
        assert minimum <= count_without_canary <= maximum
        assert plan.fact_ids == tuple(sorted(plan.fact_ids))
        assert plan.regulation_refs == tuple(sorted(plan.regulation_refs))
        assert plan.related_doc_ids == tuple(sorted(plan.related_doc_ids))
        assert people[plan.author_id].team == plan.team
        if plan.access.level == "restricted":
            assert people[plan.author_id].grade >= 4
        fact_dates = [fact.date for fact in assigned_seed_facts if fact.date]
        if fact_dates:
            assert date.fromisoformat(plan.date) >= max(fact_dates)
        assert date.fromisoformat(plan.date) <= PERIOD_END
        assert plan.title_hint
        assert set(plan.style) == {
            "tone",
            "structure",
            "length_target_chars",
            "persona",
            "frame",
        }
        assert plan.style["frame"] in {"일반", "회고"}
        covered_normal.update(
            fact.fact_id for fact in assigned_seed_facts if fact.sensitivity == "normal"
        )

    normal_facts = {
        fact.fact_id for fact in seed.facts.values() if fact.sensitivity == "normal"
    }
    assert len(covered_normal) / len(normal_facts) >= 0.70


def test_incidents_are_one_to_one_linked_and_dated_from_recovery(built) -> None:
    seed, plans, _, _ = built
    incident_entities = {
        entity_id: entity
        for entity_id, entity in seed.entities.items()
        if INCIDENT_RE.fullmatch(entity_id)
    }
    incident_plans = [plan for plan in plans if plan.doc_type == "incident"]
    assert len(incident_plans) == len(incident_entities) == 25

    for plan, (incident_id, entity) in zip(
        incident_plans, sorted(incident_entities.items()), strict=True
    ):
        incident_fact_ids = {
            fact.fact_id for fact in seed.facts.values() if fact.subject == incident_id
        }
        assert set(plan.fact_ids) == incident_fact_ids
        assert plan.team == entity["owner_team"]
        restored = next(
            fact
            for fact in seed.facts.values()
            if fact.subject == incident_id and fact.fact_id.endswith("-restored")
        )
        recovery_date = datetime.fromisoformat(str(restored.value)).date()
        assert (
            recovery_date
            <= date.fromisoformat(plan.date)
            <= recovery_date + timedelta(days=14)
        )
        linked = [
            other
            for other in plans
            if other.doc_type in {"meeting", "weekly"}
            and incident_fact_ids.intersection(other.fact_ids)
        ]
        assert len(linked) >= 2
        expected_related = {
            other.doc_id
            for other in plans
            if other.doc_id != plan.doc_id
            and set(plan.fact_ids).intersection(other.fact_ids)
        }
        assert set(plan.related_doc_ids) == expected_related
        assert {other.doc_id for other in linked} <= set(plan.related_doc_ids)


def test_restricted_facts_and_canaries_are_isolated(built) -> None:
    seed, plans, canaries, _ = built
    restricted_fact_ids = {
        fact.fact_id for fact in seed.facts.values() if fact.sensitivity == "restricted"
    }
    restricted_plans = [plan for plan in plans if plan.access.level == "restricted"]
    canary_rows = canaries["canaries"]

    assert canaries["count"] == len(canary_rows) == len(restricted_plans) == 30
    assert {row["doc_id"] for row in canary_rows} == {
        plan.doc_id for plan in restricted_plans
    }
    assert all(
        not restricted_fact_ids.intersection(plan.fact_ids)
        for plan in plans
        if plan.access.level != "restricted"
    )
    for plan in plans:
        if plan.access.level == "restricted":
            assert plan.access.teams == (plan.team,)
            assert plan.access.min_grade == 4
            assert plan.canary_fact_id in plan.fact_ids
            assert restricted_fact_ids.intersection(plan.fact_ids)
        else:
            assert plan.canary_fact_id is None
            assert not any(fact_id.startswith("f-canary-") for fact_id in plan.fact_ids)

    values = [str(row["value"]) for row in canary_rows]
    assert len(values) == len(set(values)) == 30
    assert all(CANARY_RE.fullmatch(value) for value in values)
    assert not any(
        left in right
        for index, left in enumerate(values)
        for other_index, right in enumerate(values)
        if index != other_index
    )
    seed_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((REPO_ROOT / "data/seed").rglob("*.yaml"))
    )
    row_text = json.dumps(build_rows(seed), ensure_ascii=False, sort_keys=True)
    assert all(value not in seed_text and value not in row_text for value in values)


def test_regulation_references_match_teams_and_cover_thirty_documents(built) -> None:
    seed, plans, _, _ = built
    eligible = [
        plan
        for plan in plans
        if plan.doc_type in {"guide", "notice", "meeting"} and plan.regulation_refs
    ]
    allowed = defaultdict(set)
    for row in seed.regulation_refs:
        reference = f"{row['name']} {row['article']}"
        for team in row["teams"]:
            allowed[str(team)].add(reference)

    assert len(eligible) >= 30
    assert all(1 <= len(plan.regulation_refs) <= 2 for plan in eligible)
    assert all(set(plan.regulation_refs) <= allowed[plan.team] for plan in eligible)


def test_injection_registry_has_balanced_nonleaking_slots(built) -> None:
    seed, plans, canaries, injections = built
    plan_by_id = {plan.doc_id: plan for plan in plans}
    rows = injections["injections"]

    assert injections["count"] == len(rows) == 12
    assert Counter(row["access_level"] for row in rows) == INJECTION_COUNTS
    assert Counter(row["variant"] for row in rows) == {
        variant: 3 for variant in INJECTION_TEXTS
    }
    assert (
        max(Counter(plan_by_id[row["doc_id"]].doc_type for row in rows).values()) <= 4
    )
    assert all(row["insert_rule"] == "before_last_h2" for row in rows)
    assert all(row["text"] == INJECTION_TEXTS[row["variant"]] for row in rows)
    assert all(
        plan_by_id[row["doc_id"]].injection_id == row["injection_id"] for row in rows
    )
    assert all(
        plan.injection_id is None
        for plan in plans
        if plan.doc_id not in {row["doc_id"] for row in rows}
    )

    forbidden = (
        {str(row["value"]) for row in canaries["canaries"]}
        | {
            str(fact.value)
            for fact in seed.facts.values()
            if fact.sensitivity == "restricted"
        }
        | {
            str(entity["title"])
            for entity in seed.entities.values()
            if entity.get("sensitivity") == "restricted"
        }
    )
    assert all(secret not in str(row["text"]) for row in rows for secret in forbidden)


def test_write_plan_is_byte_deterministic_and_uses_fixed_json_keys(
    built, tmp_path: Path
) -> None:
    _, plans, canaries, injections = built
    write_plan(plans, canaries, injections, root=tmp_path)
    first = {
        path: (tmp_path / path).read_bytes()
        for path in (PLAN_PATH, CANARIES_PATH, INJECTIONS_PATH)
    }
    write_plan(plans, canaries, injections, root=tmp_path)
    assert first == {
        path: (tmp_path / path).read_bytes()
        for path in (PLAN_PATH, CANARIES_PATH, INJECTIONS_PATH)
    }
    records = [
        json.loads(line)
        for line in (tmp_path / PLAN_PATH).read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 200
    assert list(records[0]) == [
        "doc_id",
        "doc_type",
        "team",
        "access",
        "author_id",
        "date",
        "title_hint",
        "fact_ids",
        "regulation_refs",
        "canary_fact_id",
        "injection_id",
        "style",
        "seed",
        "related_doc_ids",
        "fact_age_days",
        "fact_time_phrases",
    ]


def test_committed_plan_and_registries_match_current_seed(built) -> None:
    _, plans, canaries, injections = built
    committed_plans = [
        json.loads(line)
        for line in (REPO_ROOT / PLAN_PATH).read_text(encoding="utf-8").splitlines()
    ]
    assert committed_plans == [_record(plan) for plan in plans]
    assert (
        json.loads((REPO_ROOT / CANARIES_PATH).read_text(encoding="utf-8")) == canaries
    )
    assert (
        json.loads((REPO_ROOT / INJECTIONS_PATH).read_text(encoding="utf-8"))
        == injections
    )


def test_insufficient_seed_fails_instead_of_reducing_counts() -> None:
    with pytest.raises(SeedError, match="document plan requires 25 incidents"):
        build_plan(load_seed(MIN_ROOT))


INCIDENT_AGE_LIMITS = {"weekly": 30, "meeting": 90, "notice": 90}
RETROSPECTIVE_CAP = 5


def _incident_fact(seed, fact_id: str) -> bool:
    fact = seed.facts.get(fact_id)
    return fact is not None and INCIDENT_RE.fullmatch(fact.subject) is not None


def test_fact_age_days_match_document_and_fact_dates(built) -> None:
    seed, plans, _, _ = built
    for plan in plans:
        document_date = date.fromisoformat(plan.date)
        expected = {
            fact_id: (document_date - seed.facts[fact_id].date).days
            for fact_id in plan.fact_ids
            if fact_id in seed.facts and seed.facts[fact_id].date is not None
        }
        assert plan.fact_age_days == expected
        assert all(age >= 0 for age in expected.values())


def test_incident_facts_respect_type_age_limits_and_retrospective_cap(built) -> None:
    seed, plans, _, _ = built
    retrospective = [plan for plan in plans if plan.style["frame"] == "회고"]

    assert len(retrospective) <= RETROSPECTIVE_CAP
    assert all(plan.doc_type in {"meeting", "notice"} for plan in retrospective)
    for plan in plans:
        limit = INCIDENT_AGE_LIMITS.get(plan.doc_type)
        if limit is None or plan.style["frame"] == "회고":
            continue
        stale = {
            fact_id: age
            for fact_id, age in plan.fact_age_days.items()
            if _incident_fact(seed, fact_id) and age > limit
        }
        assert stale == {}, (plan.doc_id, stale)
    for plan in retrospective:
        assert any(
            _incident_fact(seed, fact_id) and age > INCIDENT_AGE_LIMITS[plan.doc_type]
            for fact_id, age in plan.fact_age_days.items()
        ), plan.doc_id


def test_incident_age_summary_reports_no_violations(built) -> None:
    seed, plans, _, _ = built
    summary = incident_age_summary(plans, seed)

    assert summary["violations"] == []
    assert summary["retrospective"] <= RETROSPECTIVE_CAP
    assert sum(summary["buckets"]["incident"].values()) == 25
    assert summary["buckets"]["weekly"]["31-90"] == 0
    assert summary["buckets"]["weekly"]["91-365"] == 0


@pytest.mark.parametrize(
    ("document", "fact", "expected"),
    [
        # 8일 전이지만 같은 달: "지난달"이 아니라 지난주
        (date(2026, 6, 20), date(2026, 6, 12), "지난주"),
        # 문서 날짜와 같은 날: 기준 없는 "당일"을 주지 않는다(파일럿 3차)
        (date(2026, 6, 20), date(2026, 6, 20), None),
        (date(2026, 6, 20), date(2026, 6, 16), "이번 주"),
        (date(2026, 6, 25), date(2026, 6, 3), "이달 초"),
        (date(2026, 6, 30), date(2026, 6, 14), "이달 중순"),
        # 전월: 5월 말 대 6월 중순
        (date(2026, 6, 20), date(2026, 5, 28), "지난달 말"),
        # 약 100일 전이지만 같은 해: "지난해"가 아니라 올해
        (date(2025, 11, 20), date(2025, 8, 5), "올해 8월"),
        # 1월 대 전년 12월: 연도는 바뀌었지만 전월
        (date(2026, 1, 20), date(2025, 12, 15), "지난달 중순"),
        # 1월 대 전년 11월: 전년
        (date(2026, 1, 20), date(2025, 11, 3), "지난해 11월"),
        # 월요일 문서, 전날(일요일)은 지난주
        (date(2026, 6, 22), date(2026, 6, 21), "지난주"),
    ],
)
def test_time_phrase_follows_calendar_boundaries(document, fact, expected) -> None:
    assert time_phrase(fact, document) == expected


def test_time_phrase_rejects_facts_after_the_document_date() -> None:
    with pytest.raises(ValueError):
        time_phrase(date(2026, 6, 21), date(2026, 6, 20))


def test_plans_carry_a_calendar_phrase_for_every_fact_dated_before_the_doc(
    built,
) -> None:
    seed, plans, _, _ = built
    for plan in plans:
        document_date = date.fromisoformat(plan.date)
        assert set(plan.fact_time_phrases) == {
            fact_id for fact_id, age in plan.fact_age_days.items() if age > 0
        }
        for fact_id, phrase in plan.fact_time_phrases.items():
            assert phrase == time_phrase(seed.facts[fact_id].date, document_date)


def test_committed_plan_has_no_same_day_phrase() -> None:
    rows = [
        json.loads(line)
        for line in (REPO_ROOT / "data/synthetic/plan.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    phrases = [phrase for row in rows for phrase in row["fact_time_phrases"].values()]
    assert "당일" not in phrases
    for row in rows:
        assert set(row["fact_time_phrases"]) == {
            fact_id for fact_id, age in row["fact_age_days"].items() if age > 0
        }
