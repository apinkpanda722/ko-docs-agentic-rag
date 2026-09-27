"""Name-pool, rename, and roster tests for the people bootstrap script."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from kodocs.synth.bootstrap_people import (
    DEFAULT_TEAM_SIZES,
    GIVEN_NAME_POOL,
    SURNAME_POOL,
    format_roster,
    generate_people,
    main,
    rename_people,
)
from kodocs.synth.seed import TEAMS

REPO_ROOT = Path(__file__).parents[2]
PEOPLE_DIR = REPO_ROOT / "data/seed/people"


def test_given_name_pool_is_unique_two_syllable_hangul() -> None:
    assert len(GIVEN_NAME_POOL) == len(set(GIVEN_NAME_POOL))
    assert all(re.fullmatch(r"[가-힣]{2}", name) for name in GIVEN_NAME_POOL)
    assert len(SURNAME_POOL) * len(GIVEN_NAME_POOL) >= 10 * sum(
        DEFAULT_TEAM_SIZES.values()
    )


def test_generated_names_are_surname_plus_pooled_given_name() -> None:
    people = generate_people(random_seed=20250101, team_sizes=DEFAULT_TEAM_SIZES)

    for records in people.values():
        for person in records:
            name = str(person["name"])
            assert name[0] in SURNAME_POOL
            assert name[1:] in GIVEN_NAME_POOL


def _people() -> dict[str, list[dict]]:
    return generate_people(random_seed=20250101, team_sizes=DEFAULT_TEAM_SIZES)


def test_rename_changes_only_the_requested_person_and_is_deterministic() -> None:
    people = _people()
    target = people["payments"][2]["person_id"]

    first, changes = rename_people(people, [target], random_seed=20250101)
    second, _ = rename_people(people, [target], random_seed=20250101)

    assert first == second
    assert [person_id for person_id, _, _ in changes] == [target]
    before = [person for team in TEAMS for person in people[team]]
    after = [person for team in TEAMS for person in first[team]]
    differing = [
        (old, new) for old, new in zip(before, after, strict=True) if old != new
    ]
    assert len(differing) == 1
    old, new = differing[0]
    assert old["person_id"] == target
    assert {k: v for k, v in old.items() if k != "name"} == {
        k: v for k, v in new.items() if k != "name"
    }
    names = [person["name"] for person in after]
    assert len(names) == len(set(names))
    assert people["payments"][2]["name"] == old["name"]  # input not mutated


def test_rename_result_does_not_depend_on_other_renamed_people() -> None:
    people = _people()
    first_id = people["platform"][4]["person_id"]
    second_id = people["legal"][1]["person_id"]

    alone, _ = rename_people(people, [first_id], random_seed=20250101)
    together, _ = rename_people(people, [first_id, second_id], random_seed=20250101)

    assert alone["platform"][4]["name"] == together["platform"][4]["name"]


def test_rename_rejects_unknown_person_id() -> None:
    with pytest.raises(ValueError, match="p-999"):
        rename_people(_people(), ["p-999"], random_seed=20250101)


def test_roster_lists_every_person_grouped_by_team() -> None:
    people = _people()

    roster = format_roster(people)

    for team in TEAMS:
        assert f"[{team}] {DEFAULT_TEAM_SIZES[team]}명" in roster
    assert all(
        f"{person['person_id']} {person['name']}(" in roster
        for records in people.values()
        for person in records
    )
    assert roster.endswith("총 200명")


def test_cli_rename_rewrites_only_one_record_and_list_prints_roster(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "people"
    shutil.copytree(PEOPLE_DIR, output)
    before = {path.name: path.read_bytes() for path in output.glob("*.yaml")}

    assert main(["--out", str(output), "--rename", "p-043"]) == 0
    changed = [
        path.name
        for path in output.glob("*.yaml")
        if path.read_bytes() != before[path.name]
    ]
    assert len(changed) == 1
    assert "p-043:" in capsys.readouterr().out

    assert main(["--out", str(output), "--list"]) == 0
    assert "총 200명" in capsys.readouterr().out
    assert main(["--out", str(output), "--rename", "p-999"]) == 1
