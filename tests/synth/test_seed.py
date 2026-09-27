"""Fixture-based contract tests for the synthetic seed loader and validator."""

from __future__ import annotations

import copy
import shutil
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
import yaml

from kodocs.synth.seed import (
    GRADES,
    PERIOD_END,
    PERIOD_START,
    TEAMS,
    Access,
    Fact,
    SeedError,
    find_banned_names,
    load_banned_names,
    load_public_figures,
    load_seed,
    surface_forms,
    validate_seed,
)

FIXTURES = Path(__file__).parent / "fixtures"
SEED_ROOT = FIXTURES / "seed_min"
REGULATION_INDEX = {
    "rules": {
        "출장규칙": {
            "rule_id": "100",
            "serial": "current",
            "articles": {"제8조": "여비 및 여행일수의 계산"},
        }
    }
}


def _seed():
    return load_seed(SEED_ROOT)


def _fact(seed, kind: str) -> Fact:
    return next(fact for fact in seed.facts.values() if fact.kind == kind)


def test_fixed_period_team_grade_and_access_contracts() -> None:
    assert (PERIOD_START, PERIOD_END) == (date(2025, 1, 1), date(2026, 6, 30))
    assert list(TEAMS) == [
        "platform",
        "payments",
        "data-ai",
        "sre",
        "security",
        "hr",
        "finance",
        "legal",
    ]
    assert TEAMS["payments"].project_key == "PAY"
    assert GRADES == {
        1: "사원",
        2: "선임",
        3: "책임",
        4: "수석",
        5: "팀장",
        6: "임원",
    }
    assert Access("public", (), 1).level == "public"
    assert Access("team", ("payments",), 1).teams == ("payments",)
    assert Access("restricted", ("security",), 4).min_grade == 4

    with pytest.raises(ValueError):
        Access("public", ("payments",), 1)
    with pytest.raises(ValueError):
        Access("team", (), 1)
    with pytest.raises(ValueError):
        Access("restricted", ("security",), 3)


def test_load_seed_merges_sorted_files_and_fills_fact_provenance() -> None:
    seed = _seed()

    assert len(seed.people) == 4
    assert [person.person_id for person in seed.people] == [
        "p-003",
        "p-004",
        "p-001",
        "p-002",
    ]
    assert [service.name for service in seed.services] == ["billing-api"]
    assert "INC-2026-031" in seed.entities
    assert "PAY-1234" in seed.entities
    assert "EXP-2026-0412" in seed.entities
    assert seed.facts["f-inc-2026-031-cause"].subject == "INC-2026-031"
    assert seed.facts["f-inc-2026-031-cause"].source == (
        "data/seed/incidents/INC-2026-031.yaml"
    )
    assert seed.facts["f-event-2025-014-decision"].sensitivity == "restricted"
    assert seed.ledger_rules["tickets"]["total"] == 10
    assert validate_seed(seed, regulation_index=REGULATION_INDEX) == []


def test_loader_reports_duplicate_fact_and_entity_ids_together(tmp_path: Path) -> None:
    root = tmp_path / "fixture"
    shutil.copytree(SEED_ROOT, root)
    events_path = root / "data/seed/events/events.yaml"
    payload = yaml.safe_load(events_path.read_text(encoding="utf-8"))
    duplicate = copy.deepcopy(payload["events"][0])
    duplicate["id"] = "INC-2026-031"
    duplicate["facts"][0]["fact_id"] = "f-inc-2026-031-cause"
    payload["events"].append(duplicate)
    events_path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(SeedError) as caught:
        load_seed(root)

    message = str(caught.value)
    assert "duplicate entity id: INC-2026-031" in message
    assert "duplicate fact_id: f-inc-2026-031-cause" in message


def test_validate_seed_reports_identifier_uniqueness_and_format_violations() -> None:
    seed = _seed()
    bad_person = replace(seed.people[0], person_id="person-3")
    bad_fact = replace(_fact(seed, "text"), fact_id="F BAD")
    facts = dict(seed.facts)
    facts.pop(_fact(seed, "text").fact_id)
    facts[bad_fact.fact_id] = bad_fact
    broken = replace(
        seed, people=(bad_person, bad_person, *seed.people[1:]), facts=facts
    )

    errors = validate_seed(broken, regulation_index=REGULATION_INDEX)

    assert any("fact_id format" in error for error in errors)
    assert any("person_id format" in error for error in errors)
    assert any("duplicate person_id" in error for error in errors)


def test_validate_seed_checks_entity_identifier_relationships() -> None:
    seed = _seed()
    entities = copy.deepcopy(dict(seed.entities))
    incident = dict(entities.pop("INC-2026-031"))
    incident["id"] = "INC-2024-001"
    entities["INC-2024-001"] = incident
    entities["PAY-1234"]["owner_team"] = "platform"
    entities["EXP-2026-0412"]["date"] = "2025-04-12"

    errors = validate_seed(
        replace(seed, entities=entities), regulation_index=REGULATION_INDEX
    )

    assert any("incident id format" in error for error in errors)
    assert any("ticket project key" in error for error in errors)
    assert any("approval year" in error for error in errors)


def test_validate_seed_checks_period_for_facts_entities_but_allows_earlier_joining() -> (
    None
):
    seed = _seed()
    facts = dict(seed.facts)
    original = _fact(seed, "count")
    facts[original.fact_id] = replace(original, date=date(2024, 12, 31))
    entities = copy.deepcopy(dict(seed.entities))
    entities["ev-2025-014"]["date"] = "2026-07-01"
    people = (replace(seed.people[0], joined=date(2026, 7, 1)), *seed.people[1:])

    errors = validate_seed(
        replace(seed, facts=facts, entities=entities, people=people),
        regulation_index=REGULATION_INDEX,
    )

    assert sum("outside period" in error for error in errors) >= 3
    assert not any("p-001" in error and "joined" in error for error in errors)


def test_validate_seed_checks_all_reference_integrity() -> None:
    seed = _seed()
    facts = dict(seed.facts)
    person_fact = _fact(seed, "person")
    service_fact = _fact(seed, "service")
    facts[person_fact.fact_id] = replace(person_fact, value="p-999")
    facts[service_fact.fact_id] = replace(service_fact, value="missing-api")
    entities = copy.deepcopy(dict(seed.entities))
    entities["ev-2025-014"]["owner_team"] = "unknown"
    entities["ev-2025-014"]["assignee_id"] = "p-999"
    entities["PAY-1234"]["incident_id"] = "INC-2026-999"

    errors = validate_seed(
        replace(seed, facts=facts, entities=entities),
        regulation_index=REGULATION_INDEX,
    )

    assert any("unknown person" in error for error in errors)
    assert any("unknown service" in error for error in errors)
    assert any("unknown team" in error for error in errors)
    assert any("unknown incident" in error for error in errors)


@pytest.mark.parametrize(
    ("kind", "bad_value"),
    [
        ("id", "not-an-id"),
        ("date", "2026-02-30"),
        ("datetime", "2026-03-14 14:05"),
        ("amount", 0),
        ("person", "kim"),
        ("service", "Billing API"),
        ("count", -1),
        ("regulation", "출장규칙 8조"),
    ],
)
def test_validate_seed_checks_kind_specific_value_formats(
    kind: str, bad_value: object
) -> None:
    seed = _seed()
    facts = dict(seed.facts)
    original = _fact(seed, kind)
    facts[original.fact_id] = replace(original, value=bad_value)

    errors = validate_seed(
        replace(seed, facts=facts), regulation_index=REGULATION_INDEX
    )

    assert any(f"kind {kind}" in error for error in errors)


def test_validate_seed_checks_regulation_facts_and_reference_entries_against_index() -> (
    None
):
    seed = _seed()
    missing_article_index = {
        "rules": {
            "출장규칙": {
                "rule_id": "100",
                "serial": "current",
                "articles": {"제7조": "다른 제목"},
            }
        }
    }

    errors = validate_seed(seed, regulation_index=missing_article_index)

    assert any("regulation fact" in error for error in errors)
    assert any("regulation_refs" in error for error in errors)


def test_validate_seed_recursively_rejects_banned_names_in_seed_strings() -> None:
    seed = _seed()
    entities = copy.deepcopy(dict(seed.entities))
    entities["ev-2025-014"]["metadata"] = {"note": "NIA 공동 기록"}

    errors = validate_seed(
        replace(seed, entities=entities), regulation_index=REGULATION_INDEX
    )

    assert any("banned name" in error and "NIA" in error for error in errors)


def test_validate_seed_accumulates_independent_violations_in_one_result() -> None:
    seed = _seed()
    first = _fact(seed, "count")
    second = _fact(seed, "text")
    facts = dict(seed.facts)
    facts.pop(first.fact_id)
    facts["BAD"] = replace(first, fact_id="BAD")
    facts[second.fact_id] = replace(second, date=date(2024, 12, 31))

    errors = validate_seed(
        replace(seed, facts=facts), regulation_index=REGULATION_INDEX
    )

    assert len(errors) == 2
    assert any("fact_id format" in error for error in errors)
    assert any("outside period" in error for error in errors)


def test_surface_forms_cover_every_atomic_kind() -> None:
    seed = _seed()
    people = {person.person_id: person for person in seed.people}

    assert surface_forms(_fact(seed, "date"), people=people) == (
        "2026-03-14",
        "2026.3.14",
        "2026. 3. 14.",
        "2026년 3월 14일",
        "3월 14일",
    )
    datetime_forms = surface_forms(_fact(seed, "datetime"), people=people)
    assert "2026-03-14 14:05" in datetime_forms
    assert "2026년 3월 14일 14시 5분" in datetime_forms
    assert surface_forms(_fact(seed, "amount"), people=people) == (
        "1,250,000원",
        "1250000원",
        "125만 원",
        "125만원",
        "1,250,000 원",
    )
    assert surface_forms(_fact(seed, "person"), people=people) == (
        "이가은",
        "이가은 수석",
    )
    assert surface_forms(_fact(seed, "count"), people=people) == (
        "12",
        "12건",
        "12개",
        "12명",
        "12회",
    )
    assert surface_forms(_fact(seed, "id"), people=people) == ("PAY-1234",)
    assert surface_forms(_fact(seed, "service"), people=people) == ("billing-api",)
    assert surface_forms(_fact(seed, "regulation"), people=people) == (
        "출장규칙 제8조",
    )
    assert surface_forms(_fact(seed, "text"), people=people) == ()


def test_banned_name_loading_and_ascii_boundaries() -> None:
    names = load_banned_names(SEED_ROOT)
    assert "NIA" in names

    text = "NIA NIA2 xNIA (NIA)"
    found = find_banned_names(text, ("NIA",))

    assert [(name, text[start:end]) for name, start, end in found] == [
        ("NIA", "NIA"),
        ("NIA", "NIA"),
    ]
    assert [start for _, start, _ in found] == [0, 15]


def test_validate_seed_rejects_person_names_equal_to_public_figures() -> None:
    seed = _seed()
    assert load_public_figures(SEED_ROOT) == ("노태우",)
    assert seed.public_figures == ("노태우",)
    first, *rest = seed.people
    renamed = replace(first, name="노태우")

    errors = validate_seed(
        replace(seed, people=(renamed, *rest)), regulation_index=REGULATION_INDEX
    )

    assert f"person name matches public figure: {first.person_id}=노태우" in errors
    assert validate_seed(seed, regulation_index=REGULATION_INDEX) == []
