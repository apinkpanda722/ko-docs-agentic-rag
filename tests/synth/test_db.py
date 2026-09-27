"""Deterministic SQLite ledger generation tests."""

from __future__ import annotations

import copy
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from kodocs.synth.db import build_rows, content_hash, write_db
from kodocs.synth.identifiers import APPROVAL_RE, TICKET_RE
from kodocs.synth.seed import PERIOD_END, PERIOD_START, TEAMS, SeedError, load_seed

REPO_ROOT = Path(__file__).parents[2]
MIN_ROOT = Path(__file__).parent / "fixtures/seed_min"
EXPECTED_ACCESS = {
    "tickets": {"public": 300, "restricted": 25, "team": 175},
    "approvals": {"public": 30, "restricted": 30, "team": 90},
}


def _table_contents(path: Path) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(path) as connection:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        return {
            name: connection.execute(f'SELECT * FROM "{name}" ORDER BY 1').fetchall()
            for name in names
        }


def _access_counts(connection: sqlite3.Connection, table: str) -> dict[str, int]:
    return dict(
        connection.execute(
            f"SELECT access_level, COUNT(*) FROM {table} GROUP BY access_level"
        ).fetchall()
    )


def test_small_fixture_build_is_deterministic_and_preserves_anchor_facts() -> None:
    seed = load_seed(MIN_ROOT)

    first = build_rows(seed)
    second = build_rows(seed)

    assert first == second
    assert content_hash(first) == content_hash(second)
    assert len(first["tickets"]) == 10
    assert len(first["approvals"]) == 5
    ticket = next(row for row in first["tickets"] if row["ticket_id"] == "PAY-1234")
    approval = next(
        row for row in first["approvals"] if row["approval_id"] == "EXP-2026-0412"
    )
    assert ticket["fact_ids"] == "f-ticket-pay-1234-status"
    assert approval["fact_ids"] == "f-approval-exp-2026-0412-amount"
    assert ticket["title"] == "청구 처리 지연 조사"
    assert approval["amount_krw"] == 1_250_000
    assert ticket["is_anchor"] == approval["is_anchor"] == 1


def test_committed_rows_and_two_databases_are_deterministic(tmp_path: Path) -> None:
    seed = load_seed(REPO_ROOT)
    first = build_rows(seed)
    second = build_rows(seed)

    assert content_hash(first) == content_hash(second)
    assert len(first["tickets"]) == 500
    assert len(first["approvals"]) == 150

    first_path = tmp_path / "first.sqlite"
    second_path = tmp_path / "nested/second.sqlite"
    write_db(first, first_path)
    write_db(second, second_path)

    assert _table_contents(first_path) == _table_contents(second_path)


def test_access_counts_and_restricted_contract_in_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    write_db(build_rows(load_seed(REPO_ROOT)), path)

    with sqlite3.connect(path) as connection:
        for table, expected in EXPECTED_ACCESS.items():
            assert _access_counts(connection, table) == expected
            bad_restricted = connection.execute(
                f"SELECT COUNT(*) FROM {table} "
                "WHERE access_level = 'restricted' "
                "AND (access_min_grade != 4 OR access_teams != owner_team)"
            ).fetchone()
            assert bad_restricted == (0,)


def test_anchor_values_and_fact_ids_are_preserved(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    write_db(build_rows(load_seed(REPO_ROOT)), path)

    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        ticket = connection.execute(
            "SELECT * FROM tickets WHERE ticket_id = 'PAY-1234'"
        ).fetchone()
        approval = connection.execute(
            "SELECT * FROM approvals WHERE approval_id = 'EXP-2026-0412'"
        ).fetchone()

    assert ticket is not None
    expected_ticket_values = {
        "ticket_id": "PAY-1234",
        "project_key": "PAY",
        "owner_team": "payments",
        "title": "청구 처리 지연 조사",
        "ticket_type": "incident",
        "status": "resolved",
        "priority": "critical",
        "service": "billing-api",
        "incident_id": "INC-2026-031",
        "reporter_id": "p-043",
        "assignee_id": "p-043",
        "access_level": "public",
        "access_teams": "",
        "access_min_grade": 1,
        "is_anchor": 1,
        "fact_ids": "",
    }
    assert {
        key: ticket[key] for key in expected_ticket_values
    } == expected_ticket_values
    assert approval is not None
    assert approval["approval_id"] == "EXP-2026-0412"
    assert approval["owner_team"] == "finance"
    assert approval["title"] == "출장비 정산"
    assert approval["amount_krw"] == 1_250_000
    assert approval["regulation_ref"] == "출장규칙 제8조"
    assert approval["fact_ids"] == ""
    assert approval["is_anchor"] == 1


def test_identifiers_dates_relationships_foreign_keys_and_uniqueness(
    tmp_path: Path,
) -> None:
    seed = load_seed(REPO_ROOT)
    rows = build_rows(seed)
    path = tmp_path / "ledger.sqlite"
    write_db(rows, path)

    ticket_ids = [str(row["ticket_id"]) for row in rows["tickets"]]
    approval_ids = [str(row["approval_id"]) for row in rows["approvals"]]
    assert len(ticket_ids) == len(set(ticket_ids))
    assert len(approval_ids) == len(set(approval_ids))
    assert all(TICKET_RE.fullmatch(identifier) for identifier in ticket_ids)
    assert all(APPROVAL_RE.fullmatch(identifier) for identifier in approval_ids)
    assert all(
        row["project_key"] == TEAMS[str(row["owner_team"])].project_key
        for row in rows["tickets"]
    )

    kst = timedelta(hours=9)
    for row in rows["tickets"]:
        created = datetime.fromisoformat(str(row["created_at"]))
        assert created.utcoffset() == kst
        assert PERIOD_START <= created.date() <= PERIOD_END
        if row["resolved_at"] is not None:
            resolved = datetime.fromisoformat(str(row["resolved_at"]))
            assert created <= resolved
            assert resolved.date() <= PERIOD_END
        if row["status"] == "open":
            assert row["resolved_at"] is None
    for row in rows["approvals"]:
        submitted = datetime.fromisoformat(str(row["submitted_at"]))
        assert submitted.utcoffset() == kst
        assert PERIOD_START <= submitted.date() <= PERIOD_END
        if row["decided_at"] is not None:
            decided = datetime.fromisoformat(str(row["decided_at"]))
            assert submitted <= decided
            assert decided.date() <= PERIOD_END
        if row["status"] == "submitted":
            assert row["decided_at"] is None

    background_people = {row["person_id"]: row for row in rows["people"]}
    service_owners = {row["service"]: row["owner_team"] for row in rows["services"]}
    for row in rows["tickets"]:
        if not row["is_anchor"]:
            assert service_owners[str(row["service"])] == row["owner_team"]
            assert (
                background_people[str(row["reporter_id"])]["team"] == row["owner_team"]
            )
            assert (
                background_people[str(row["assignee_id"])]["team"] == row["owner_team"]
            )
    for row in rows["approvals"]:
        if not row["is_anchor"]:
            assert (
                background_people[str(row["requester_id"])]["team"] == row["owner_team"]
            )
            approver = background_people[str(row["approver_id"])]
            amount_limit = seed.ledger_rules["approvals"]["category_amount_ranges"][
                str(row["category"])
            ]["max"]
            expected_team = (
                "finance"
                if int(row["amount_krw"]) > int(amount_limit)
                else row["owner_team"]
            )
            assert approver["team"] == expected_team
            assert approver["grade"] == 5

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_anchor_access_over_target_raises_seed_error() -> None:
    seed = load_seed(MIN_ROOT)
    rules = copy.deepcopy(dict(seed.ledger_rules))
    rules["tickets"]["access"] = {"public": 0, "team": 9, "restricted": 1}
    broken = replace(seed, ledger_rules=rules)

    with pytest.raises(SeedError, match="anchor tickets public count"):
        build_rows(broken)


def test_committed_manifest_content_hash_matches_current_seed() -> None:
    manifest = json.loads(
        (REPO_ROOT / "data/manifest/synth_db.json").read_text(encoding="utf-8")
    )
    rows = build_rows(load_seed(REPO_ROOT))

    assert manifest["schema_version"] == 1
    assert manifest["db_path"] == "data/synthetic/ledger.sqlite"
    assert manifest["content_hash"] == content_hash(rows)
    assert manifest["row_counts"]["tickets"] == 500
    assert manifest["row_counts"]["approvals"] == 150
    assert manifest["access_counts"] == EXPECTED_ACCESS
    assert "built_at" not in manifest
    assert "build_time" not in manifest
