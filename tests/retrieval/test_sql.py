"""Tests for read-only, row-authorized SQLite queries."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kodocs.retrieval import sql as sql_module
from kodocs.retrieval.access import UserContext
from kodocs.retrieval.sql import SqlRejected, authorized_run_sql


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE tickets (
                ticket_id TEXT PRIMARY KEY,
                owner_team TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                access_level TEXT NOT NULL,
                access_teams TEXT NOT NULL,
                access_min_grade INTEGER NOT NULL
            );
            CREATE TABLE approvals (
                approval_id TEXT PRIMARY KEY,
                owner_team TEXT NOT NULL,
                amount_krw INTEGER NOT NULL,
                status TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                access_level TEXT NOT NULL,
                access_teams TEXT NOT NULL,
                access_min_grade INTEGER NOT NULL
            );
            CREATE TABLE people (person_id TEXT PRIMARY KEY, name TEXT NOT NULL);
            """
        )
        connection.executemany(
            "INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("PUB-1", "legal", "open", "2026-01-01", "public", "", 0),
                ("TEAM-P", "platform", "open", "2026-01-02", "team", "platform", 0),
                ("TEAM-F", "finance", "closed", "2026-01-03", "team", "finance", 0),
                (
                    "RES-P3",
                    "platform",
                    "closed",
                    "2026-01-04",
                    "restricted",
                    "sre,platform",
                    3,
                ),
                (
                    "RES-P5",
                    "platform",
                    "open",
                    "2026-01-05",
                    "restricted",
                    "platform",
                    5,
                ),
                ("RES-F", "finance", "open", "2026-01-06", "restricted", "finance", 2),
            ],
        )
        connection.executemany(
            "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("EXP-1", "legal", 100, "approved", "2026-01-01", "public", "", 0),
                (
                    "EXP-P",
                    "platform",
                    200,
                    "approved",
                    "2026-01-02",
                    "team",
                    "platform",
                    0,
                ),
                (
                    "EXP-P3",
                    "platform",
                    300,
                    "submitted",
                    "2026-01-03",
                    "restricted",
                    "platform",
                    3,
                ),
                (
                    "EXP-F",
                    "finance",
                    400,
                    "approved",
                    "2026-01-04",
                    "restricted",
                    "finance",
                    2,
                ),
            ],
        )
        connection.execute("INSERT INTO people VALUES ('P-1', '비공개 사용자')")
    return path


def test_sql_prefilters_rows_before_count_and_sum(ledger_path: Path) -> None:
    query = "SELECT COUNT(*) AS count FROM tickets"
    low = authorized_run_sql(ledger_path, UserContext("platform", 2), query)
    high = authorized_run_sql(ledger_path, UserContext("platform", 3), query)
    finance = authorized_run_sql(ledger_path, UserContext("finance", 2), query)

    assert low.rows == ((2,),)
    assert high.rows == ((3,),)
    assert finance.rows == ((3,),)

    sum_query = "SELECT SUM(amount_krw) AS amount_krw FROM approvals"
    assert authorized_run_sql(
        ledger_path, UserContext("platform", 2), sum_query
    ).rows == ((300,),)
    assert authorized_run_sql(
        ledger_path, UserContext("platform", 3), sum_query
    ).rows == ((600,),)
    assert authorized_run_sql(
        ledger_path, UserContext("finance", 2), sum_query
    ).rows == ((500,),)

    injected_team = UserContext("platform' OR 1=1 --", 99)
    assert authorized_run_sql(ledger_path, injected_team, query).rows == ((1,),)


def test_sql_allows_select_ctes_without_bypassing_filtered_views(
    ledger_path: Path,
) -> None:
    result = authorized_run_sql(
        ledger_path,
        UserContext("platform", 2),
        "WITH visible AS (SELECT ticket_id FROM tickets) "
        "SELECT COUNT(*) AS count FROM visible",
    )

    assert result.rows == ((2,),)


def test_sql_returns_columns_and_enforces_outer_limit(ledger_path: Path) -> None:
    result = authorized_run_sql(
        ledger_path,
        UserContext("platform", 9),
        "SELECT ticket_id, status FROM tickets ORDER BY ticket_id",
        limit=2,
    )

    assert result.columns == ("ticket_id", "status")
    assert result.rows == (("PUB-1", "open"), ("RES-P3", "closed"))
    assert result.truncated is True

    user_limited = authorized_run_sql(
        ledger_path,
        UserContext("platform", 9),
        "SELECT ticket_id FROM tickets ORDER BY ticket_id LIMIT 1",
        limit=2,
    )
    assert user_limited.rows == (("PUB-1",),)
    assert user_limited.truncated is False


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        ("SELECT 1; SELECT 2", "multi_statement"),
        ("INSERT INTO tickets (ticket_id) VALUES ('x')", "not_select"),
        ("UPDATE tickets SET status = 'closed'", "not_select"),
        ("DELETE FROM tickets", "not_select"),
        ("DROP TABLE tickets", "not_select"),
        ("ALTER TABLE tickets ADD COLUMN secret TEXT", "not_select"),
        ("ATTACH DATABASE ':memory:' AS other", "not_select"),
        ("PRAGMA table_info(tickets)", "not_select"),
        ("VACUUM", "not_select"),
        ("WITH one AS (SELECT 1) DELETE FROM tickets", "not_select"),
        ("SELECT * FROM people", "table_not_allowed"),
        ("SELECT name FROM sqlite_master", "table_not_allowed"),
        ("SELECT load_extension('not-a-library')", "forbidden_function"),
    ],
)
def test_sql_rejects_unsafe_statements_with_reason_code_only(
    ledger_path: Path, query: str, reason: str
) -> None:
    with pytest.raises(SqlRejected) as caught:
        authorized_run_sql(ledger_path, UserContext("platform", 9), query)

    assert str(caught.value) == reason
    assert caught.value.args == (reason,)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT COUNT(*) FROM main.tickets",
        "SELECT ticket_id FROM tickets UNION SELECT ticket_id FROM main.tickets",
        "SELECT * FROM (SELECT * FROM main.tickets)",
        "WITH tickets AS (SELECT * FROM main.tickets) SELECT * FROM tickets",
        "WITH approvals AS (SELECT * FROM main.approvals) SELECT * FROM approvals",
        (
            "WITH outer_cte AS ("
            "WITH tickets AS (SELECT * FROM main.tickets) SELECT * FROM tickets"
            ") SELECT * FROM outer_cte"
        ),
        "SELECT * FROM temp.sqlite_temp_master",
    ],
)
def test_sql_rejects_original_table_bypasses_and_view_shadowing(
    ledger_path: Path, query: str
) -> None:
    with pytest.raises(SqlRejected):
        authorized_run_sql(ledger_path, UserContext("platform", 9), query)


def test_sql_allows_keywords_in_literals_and_comments(ledger_path: Path) -> None:
    result = authorized_run_sql(
        ledger_path,
        UserContext("platform", 1),
        """
        /* DROP TABLE people; */
        SELECT 'DELETE FROM approvals' AS note, substr(created_at, 1, 7) AS month
        FROM tickets
        WHERE status = 'open' -- ATTACH is ordinary comment text
        ORDER BY ticket_id
        """,
    )

    assert result.columns == ("note", "month")
    assert result.rows == (("DELETE FROM approvals", "2026-01"),) * 2


def test_sql_uses_a_read_only_uri_connection(
    ledger_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, dict[str, object]]] = []
    readonly_failures: list[bool] = []
    real_connect = sqlite3.connect

    def recording_connect(database: object, *args: object, **kwargs: object):
        calls.append((database, dict(kwargs)))
        connection = real_connect(database, *args, **kwargs)
        try:
            connection.execute("UPDATE main.tickets SET status = status WHERE 0")
        except sqlite3.OperationalError:
            readonly_failures.append(True)
        else:
            readonly_failures.append(False)
        return connection

    monkeypatch.setattr(sql_module.sqlite3, "connect", recording_connect)

    authorized_run_sql(
        ledger_path,
        UserContext("platform", 1),
        "SELECT COUNT(*) FROM tickets",
    )

    assert calls == [(f"file:{ledger_path}?mode=ro", {"uri": True})]
    assert readonly_failures == [True]


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_sql_rejects_invalid_limits(ledger_path: Path, limit: object) -> None:
    with pytest.raises((TypeError, ValueError), match="limit"):
        authorized_run_sql(
            ledger_path,
            UserContext("platform", 1),
            "SELECT 1",
            limit=limit,  # type: ignore[arg-type]
        )
