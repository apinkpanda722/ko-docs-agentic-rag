"""Acceptance tests for the reviewed, committed synthetic seed."""

from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path

import yaml

from kodocs.synth.bootstrap_people import generate_people, main
from kodocs.synth.regulations import load_index
from kodocs.synth.seed import (
    TEAMS,
    find_banned_names,
    load_public_figures,
    load_seed,
    validate_seed,
)

REPO_ROOT = Path(__file__).parents[2]
SEED_DIR = REPO_ROOT / "data/seed"
TEAM_SIZES = {
    "platform": 42,
    "payments": 34,
    "data-ai": 30,
    "sre": 26,
    "security": 18,
    "hr": 18,
    "finance": 20,
    "legal": 12,
}
EVENT_TYPES = {"project", "decision", "release", "policy", "org", "sensitive"}


def _yaml(path: Path) -> object:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _records(path: Path, key: str) -> list[dict[str, object]]:
    payload = _yaml(path)
    assert isinstance(payload, dict)
    records = payload[key]
    assert isinstance(records, list)
    return records


def _incident_records() -> list[dict[str, object]]:
    records = []
    for path in sorted((SEED_DIR / "incidents").glob("*.yaml")):
        payload = _yaml(path)
        assert isinstance(payload, dict)
        records.append(payload)
    return records


def _event_records() -> list[dict[str, object]]:
    records = []
    for path in sorted((SEED_DIR / "events").glob("*.yaml")):
        records.extend(_records(path, "events"))
    return records


def test_committed_seed_loads_and_passes_the_fixed_contract() -> None:
    seed = load_seed(REPO_ROOT)

    assert validate_seed(seed, regulation_index=load_index(REPO_ROOT)) == []
    assert seed.company["name_ko"] == "해온클라우드"
    assert seed.company["name_en"] == "Haeon Cloud"
    assert seed.company["random_seed"] == 20250101
    assert seed.company["period"] == {
        "start": "2025-01-01",
        "end": "2026-06-30",
    }


def test_people_counts_leadership_and_bootstrap_are_deterministic(
    tmp_path: Path,
) -> None:
    first = generate_people(random_seed=20250101, team_sizes=TEAM_SIZES)
    second = generate_people(random_seed=20250101, team_sizes=TEAM_SIZES)
    different = generate_people(random_seed=20250102, team_sizes=TEAM_SIZES)

    assert first == second
    assert first != different
    assert {team: len(records) for team, records in first.items()} == TEAM_SIZES
    assert sum(map(len, first.values())) == 200
    assert all(
        sum(person["grade"] == 5 for person in people) == 1 for people in first.values()
    )
    assert (
        sum(person["grade"] == 6 for people in first.values() for person in people) == 2
    )
    assert sum(person["grade"] == 6 for person in first["platform"]) == 1
    assert sum(person["grade"] == 6 for person in first["finance"]) == 1
    names = [person["name"] for people in first.values() for person in people]
    assert len(names) == len(set(names)) == 200
    ordered = [person for team in TEAMS for person in first[team]]
    assert [person["person_id"] for person in ordered] == [
        f"p-{number:03d}" for number in range(1, 201)
    ]
    assert all(
        people == sorted(people, key=lambda person: -int(person["grade"]))
        for people in first.values()
    )

    output = tmp_path / "people"
    assert main(["--out", str(output)]) == 0
    original = (output / "platform.yaml").read_text(encoding="utf-8")
    assert main(["--out", str(output)]) == 1
    assert (output / "platform.yaml").read_text(encoding="utf-8") == original
    assert main(["--out", str(output), "--force"]) == 0


def test_committed_people_and_teams_match_exact_quantity_basis() -> None:
    seed = load_seed(REPO_ROOT)
    team_records = _records(SEED_DIR / "teams.yaml", "teams")

    assert len(seed.people) == 200
    assert Counter(person.team for person in seed.people) == TEAM_SIZES
    assert len(team_records) == 8
    assert {record["code"] for record in team_records} == set(TEAMS)
    for team in TEAMS:
        people = [person for person in seed.people if person.team == team]
        assert sum(person.grade == 5 for person in people) == 1


def test_services_meet_fact_and_cross_link_requirements() -> None:
    seed = load_seed(REPO_ROOT)
    incidents = _incident_records()

    assert len(seed.services) >= 12
    assert "billing-api" in {service.name for service in seed.services}
    assert all(service.owner_team in TEAMS for service in seed.services)
    assert all(len(service.facts) >= 4 for service in seed.services)
    service_incidents = Counter(str(record["service"]) for record in incidents)
    assert sum(count >= 2 for count in service_incidents.values()) >= 3


def test_incident_counts_facts_dates_and_quarter_coverage() -> None:
    paths = sorted((SEED_DIR / "incidents").glob("*.yaml"))
    incidents = _incident_records()
    ids = [str(record["id"]) for record in incidents]

    assert len(paths) == len(incidents) == 25
    assert sum(identifier.startswith("INC-2025-") for identifier in ids) == 10
    assert sum(identifier.startswith("INC-2026-") for identifier in ids) == 15
    assert "INC-2026-031" in ids
    assert all(len(record["facts"]) >= 6 for record in incidents)
    for record in incidents:
        kinds = {fact["kind"] for fact in record["facts"]}
        assert {"datetime", "service", "text", "person", "id"} <= kinds

    quarters = Counter()
    for record in incidents:
        occurred = date.fromisoformat(str(record["date"]))
        quarters[(occurred.year, (occurred.month - 1) // 3 + 1)] += 1
    assert quarters == {
        (2025, 1): 2,
        (2025, 2): 2,
        (2025, 3): 2,
        (2025, 4): 4,
        (2026, 1): 4,
        (2026, 2): 11,
    }


def test_events_and_restricted_facts_meet_exact_counting_method() -> None:
    seed = load_seed(REPO_ROOT)
    events = _event_records()

    assert len(events) >= 60
    assert EVENT_TYPES <= {str(record["type"]) for record in events}
    assert all(len(record["facts"]) >= 3 for record in events)
    restricted = [
        fact for fact in seed.facts.values() if fact.sensitivity == "restricted"
    ]
    restricted_teams = {
        str(seed.entities[fact.subject]["owner_team"])
        for fact in restricted
        if fact.subject in seed.entities
    }
    assert len(restricted) >= 30
    assert len(restricted_teams) >= 6

    linked = {
        str(fact["value"])
        for event in events
        if event["type"] in {"decision", "project"}
        for fact in event["facts"]
        if fact["kind"] == "id" and str(fact["value"]).startswith("INC-")
    }
    assert len(linked) >= 5


def test_anchor_ledgers_meet_counts_links_examples_and_access_contract() -> None:
    tickets = _records(SEED_DIR / "ledger/tickets_anchor.yaml", "tickets")
    approvals = _records(SEED_DIR / "ledger/approvals_anchor.yaml", "approvals")
    incident_ids = {str(record["id"]) for record in _incident_records()}

    assert len(tickets) >= 60
    assert len(approvals) >= 30
    assert "PAY-1234" in {record["id"] for record in tickets}
    assert "EXP-2026-0412" in {record["id"] for record in approvals}
    assert incident_ids <= {
        str(record["incident_id"])
        for record in tickets
        if record.get("incident_id") is not None
    }
    assert all("regulation_ref" in record for record in approvals)
    assert all(
        record["access_level"] in {"public", "team", "restricted"}
        for record in tickets + approvals
    )
    for record in tickets + approvals:
        if record["access_level"] == "restricted":
            assert record["min_grade"] == 4
            assert record["teams"] == [record["owner_team"]]

    rules = _yaml(SEED_DIR / "ledger/rules.yaml")
    assert isinstance(rules, dict)
    assert rules["tickets"]["total"] == 500
    assert rules["tickets"]["access"] == {
        "public": 300,
        "team": 175,
        "restricted": 25,
    }
    assert rules["approvals"]["total"] == 150
    assert rules["approvals"]["access"] == {
        "public": 30,
        "team": 90,
        "restricted": 30,
    }
    for ledger, anchors in (("tickets", tickets), ("approvals", approvals)):
        access = rules[ledger]["access"]
        assert sum(access.values()) == rules[ledger]["total"]
        anchor_counts = Counter(str(record["access_level"]) for record in anchors)
        assert all(anchor_counts[level] <= count for level, count in access.items())

    assert set(rules["tickets"]["team_weights"]) == set(TEAMS)
    for key in (
        "team_weights",
        "status_weights",
        "priority_weights",
        "type_weights",
    ):
        assert sum(rules["tickets"][key].values()) == 1
    assert sum(rules["tickets"]["monthly_volume_weights"]) == 1
    for key in ("category_weights", "status_weights"):
        assert sum(rules["approvals"][key].values()) == 1
    assert sum(rules["approvals"]["monthly_volume_weights"]) == 1
    assert all(
        bounds["min"] < bounds["max"]
        for bounds in rules["approvals"]["category_amount_ranges"].values()
    )


def test_regulation_references_and_denylist_meet_quantity_basis() -> None:
    refs = _records(SEED_DIR / "regulation_refs.yaml", "regulation_refs")
    denied = _yaml(SEED_DIR / "banned_names.yaml")

    assert len(refs) >= 30
    assert len({record["name"] for record in refs}) >= 10
    assert any(
        record["name"] == "출장규칙" and record["article"] == "제8조" for record in refs
    )
    assert isinstance(denied, dict)
    assert len(denied["names"]) >= 7

    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(SEED_DIR.rglob("*"))
        if path.is_file() and path.name != "banned_names.yaml"
    )
    assert find_banned_names(text, tuple(denied["names"])) == []


def test_public_figures_list_is_reviewed_and_absent_from_people() -> None:
    figures = load_public_figures(REPO_ROOT)
    people_names = {person.name for person in load_seed(REPO_ROOT).people}

    assert len(figures) >= 30
    assert len(figures) == len(set(figures))
    assert people_names.isdisjoint(figures)


def test_bootstrap_replaces_only_the_excluded_person_name() -> None:
    baseline = generate_people(random_seed=20250101, team_sizes=TEAM_SIZES)
    excluded = baseline["payments"][3]["name"]

    replaced = generate_people(
        random_seed=20250101, team_sizes=TEAM_SIZES, excluded_names=(excluded,)
    )
    again = generate_people(
        random_seed=20250101, team_sizes=TEAM_SIZES, excluded_names=(excluded,)
    )

    assert replaced == again
    before = [person for team in TEAMS for person in baseline[team]]
    after = [person for team in TEAMS for person in replaced[team]]
    changed = [(old, new) for old, new in zip(before, after, strict=True) if old != new]
    assert len(changed) == 1
    old, new = changed[0]
    assert old["name"] == excluded and new["name"] != excluded
    assert {key: value for key, value in old.items() if key != "name"} == {
        key: value for key, value in new.items() if key != "name"
    }
    names = [person["name"] for person in after]
    assert len(names) == len(set(names))


def test_bootstrap_with_committed_public_figures_emits_none_of_them() -> None:
    figures = load_public_figures(REPO_ROOT)
    people = generate_people(
        random_seed=20250101, team_sizes=TEAM_SIZES, excluded_names=figures
    )

    assert {
        person["name"] for records in people.values() for person in records
    }.isdisjoint(figures)
