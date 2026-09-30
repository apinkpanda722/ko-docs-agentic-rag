"""Read-only SQLite queries over row-authorized temporary views."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from kodocs.retrieval.access import UserContext

ALLOWED_TABLES = ("tickets", "approvals")
DEFAULT_LIMIT = 100

_CONTEXT_TABLE = "_kodocs_authorization_context"
_ALLOWED_FUNCTIONS = frozenset(
    {
        "abs",
        "avg",
        "coalesce",
        "count",
        "date",
        "datetime",
        "format",
        "glob",
        "group_concat",
        "hex",
        "ifnull",
        "iif",
        "instr",
        "julianday",
        "length",
        "like",
        "likely",
        "likelihood",
        "lower",
        "ltrim",
        "max",
        "min",
        "nullif",
        "printf",
        "quote",
        "replace",
        "round",
        "rtrim",
        "sign",
        "strftime",
        "substr",
        "substring",
        "sum",
        "time",
        "total",
        "trim",
        "typeof",
        "unicode",
        "unixepoch",
        "unlikely",
        "upper",
    }
)


class SqlRejected(ValueError):
    """A SQL statement rejected with a non-sensitive reason code."""


@dataclass(frozen=True)
class SqlResult:
    """Columns and immutable rows returned by one authorized query."""

    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]
    truncated: bool


def authorized_run_sql(
    db_path: Path,
    ctx: UserContext,
    sql: str,
    *,
    limit: int = DEFAULT_LIMIT,
) -> SqlResult:
    """Execute one SELECT against pre-filtered ticket and approval views."""
    if not isinstance(db_path, Path):
        raise TypeError("db_path must be a Path")
    if not isinstance(ctx.team, str) or not ctx.team:
        raise ValueError("team must be a non-empty string")
    if not isinstance(ctx.grade, int) or isinstance(ctx.grade, bool):
        raise TypeError("grade must be an integer")
    if not isinstance(sql, str):
        raise TypeError("sql must be a string")
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise TypeError("limit must be a positive integer")
    if limit <= 0:
        raise ValueError("limit must be a positive integer")

    statement = _single_select_statement(sql)
    uri = f"file:{db_path}?mode=ro"
    connection: sqlite3.Connection | None = None
    denial_reason: list[str | None] = [None]
    try:
        connection = sqlite3.connect(uri, uri=True)
        _create_authorized_views(connection, ctx)
        connection.set_authorizer(_authorizer(denial_reason))
        connection.execute(f"EXPLAIN {statement}")
        wrapped = f'SELECT * FROM (\n{statement}\n) AS "_kodocs_limited" LIMIT ?'
        cursor = connection.execute(wrapped, (limit + 1,))
        columns = tuple(item[0] for item in cursor.description or ())
        fetched = cursor.fetchall()
        truncated = len(fetched) > limit
        rows = tuple(tuple(row) for row in fetched[:limit])
        return SqlResult(columns=columns, rows=rows, truncated=truncated)
    except SqlRejected:
        raise
    except sqlite3.Error:
        raise SqlRejected(denial_reason[0] or "invalid_sql") from None
    finally:
        if connection is not None:
            connection.set_authorizer(None)
            connection.close()


def _create_authorized_views(connection: sqlite3.Connection, ctx: UserContext) -> None:
    connection.execute(
        f"""
        CREATE TEMP TABLE {_CONTEXT_TABLE} (
            team TEXT NOT NULL,
            grade INTEGER NOT NULL CHECK (typeof(grade) = 'integer')
        )
        """
    )
    connection.execute(
        f"INSERT INTO {_CONTEXT_TABLE} (team, grade) VALUES (?, ?)",
        (ctx.team, ctx.grade),
    )
    for table in ALLOWED_TABLES:
        connection.execute(
            f"""
            CREATE TEMP VIEW {table} AS
            SELECT source.*
            FROM main.{table} AS source
            WHERE source.access_level = 'public'
               OR (
                    source.access_level = 'team'
                    AND instr(
                        (SELECT team FROM temp.{_CONTEXT_TABLE}), ','
                    ) = 0
                    AND instr(
                        ',' || source.access_teams || ',',
                        ',' || (
                            SELECT team FROM temp.{_CONTEXT_TABLE}
                        ) || ','
                    ) > 0
               )
               OR (
                    source.access_level = 'restricted'
                    AND instr(
                        (SELECT team FROM temp.{_CONTEXT_TABLE}), ','
                    ) = 0
                    AND instr(
                        ',' || source.access_teams || ',',
                        ',' || (
                            SELECT team FROM temp.{_CONTEXT_TABLE}
                        ) || ','
                    ) > 0
                    AND (
                        SELECT grade FROM temp.{_CONTEXT_TABLE}
                    ) >= source.access_min_grade
               )
            """
        )


def _authorizer(denial_reason: list[str | None]):
    def authorize(
        action: int,
        arg1: str | None,
        arg2: str | None,
        database: str | None,
        source: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            allowed_read = (
                (database == "main" and arg1 in ALLOWED_TABLES and source == arg1)
                or (database == "temp" and arg1 in ALLOWED_TABLES)
                or (
                    database == "temp"
                    and arg1 == _CONTEXT_TABLE
                    and source in ALLOWED_TABLES
                )
            )
            if allowed_read:
                return sqlite3.SQLITE_OK
            return _deny(denial_reason, "table_not_allowed")
        if action == sqlite3.SQLITE_FUNCTION:
            function = (arg2 or "").lower()
            if function in _ALLOWED_FUNCTIONS:
                return sqlite3.SQLITE_OK
            return _deny(denial_reason, "forbidden_function")
        return _deny(denial_reason, "not_select")

    return authorize


def _deny(denial_reason: list[str | None], reason: str) -> int:
    if denial_reason[0] is None:
        denial_reason[0] = reason
    return sqlite3.SQLITE_DENY


def _single_select_statement(sql: str) -> str:
    tokens, semicolons = _lex_sql(sql)
    if not tokens:
        raise SqlRejected("not_select")
    if len(semicolons) > 1 or (
        semicolons and any(token[1] > semicolons[0] for token in tokens)
    ):
        raise SqlRejected("multi_statement")
    first_keyword = tokens[0][0].upper()
    if first_keyword not in {"SELECT", "WITH"}:
        raise SqlRejected("not_select")
    if first_keyword == "WITH":
        cte_names, final_keyword = _cte_info(sql)
        if set(cte_names) & set(ALLOWED_TABLES):
            raise SqlRejected("table_not_allowed")
        if final_keyword != "select":
            raise SqlRejected("not_select")
    if semicolons:
        return sql[: semicolons[0]]
    return sql


def _lex_sql(sql: str) -> tuple[list[tuple[str, int]], list[int]]:
    """Return unquoted word tokens and semicolons, ignoring strings/comments."""
    tokens: list[tuple[str, int]] = []
    semicolons: list[int] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        following = sql[index + 1] if index + 1 < length else ""
        if char.isspace():
            index += 1
        elif char == "-" and following == "-":
            newline = sql.find("\n", index + 2)
            index = length if newline < 0 else newline + 1
        elif char == "/" and following == "*":
            closing = sql.find("*/", index + 2)
            index = length if closing < 0 else closing + 2
        elif char in {"'", '"', "`"}:
            index = _skip_quoted(sql, index, char)
        elif char == "[":
            index = _skip_quoted(sql, index, "]")
        elif char == ";":
            semicolons.append(index)
            index += 1
        elif char.isalpha() or char == "_":
            end = index + 1
            while end < length and (sql[end].isalnum() or sql[end] == "_"):
                end += 1
            tokens.append((sql[index:end], index))
            index = end
        else:
            index += 1
    return tokens, semicolons


def _skip_quoted(sql: str, index: int, closing: str) -> int:
    index += 1
    while index < len(sql):
        if sql[index] != closing:
            index += 1
            continue
        if index + 1 < len(sql) and sql[index + 1] == closing:
            index += 2
            continue
        return index + 1
    return len(sql)


def _cte_info(sql: str) -> tuple[tuple[str, ...], str | None]:
    """Return CTE names and the statement keyword after their definitions."""
    tokens = _syntax_tokens(sql)
    if not tokens or tokens[0].upper() != "WITH":
        return (), None
    leading_names, final_keyword = _parse_cte(tokens, 0)
    names = list(leading_names)
    for index, token in enumerate(tokens[1:], start=1):
        if token.upper() == "WITH":
            nested_names, _ = _parse_cte(tokens, index)
            names.extend(nested_names)
    return tuple(names), final_keyword


def _parse_cte(tokens: list[str], with_index: int) -> tuple[list[str], str | None]:
    index = with_index + 1
    if index < len(tokens) and tokens[index].upper() == "RECURSIVE":
        index += 1
    names: list[str] = []
    while index < len(tokens):
        names.append(tokens[index].lower())
        index += 1
        if index < len(tokens) and tokens[index] == "(":
            index = _after_balanced_parentheses(tokens, index)
        if index >= len(tokens) or tokens[index].upper() != "AS":
            break
        index += 1
        if index < len(tokens) and tokens[index].upper() == "NOT":
            index += 1
        if index < len(tokens) and tokens[index].upper() == "MATERIALIZED":
            index += 1
        if index >= len(tokens) or tokens[index] != "(":
            break
        index = _after_balanced_parentheses(tokens, index)
        if index >= len(tokens) or tokens[index] != ",":
            break
        index += 1
    final_keyword = tokens[index].lower() if index < len(tokens) else None
    return names, final_keyword


def _syntax_tokens(sql: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    while index < len(sql):
        char = sql[index]
        following = sql[index + 1] if index + 1 < len(sql) else ""
        if char.isspace():
            index += 1
        elif char == "-" and following == "-":
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline < 0 else newline + 1
        elif char == "/" and following == "*":
            closing = sql.find("*/", index + 2)
            index = len(sql) if closing < 0 else closing + 2
        elif char == "'":
            index = _skip_quoted(sql, index, char)
        elif char in {'"', "`"}:
            end = _skip_quoted(sql, index, char)
            tokens.append(sql[index + 1 : max(index + 1, end - 1)])
            index = end
        elif char == "[":
            end = _skip_quoted(sql, index, "]")
            tokens.append(sql[index + 1 : max(index + 1, end - 1)])
            index = end
        elif char.isalpha() or char == "_":
            end = index + 1
            while end < len(sql) and (sql[end].isalnum() or sql[end] == "_"):
                end += 1
            tokens.append(sql[index:end])
            index = end
        elif char in {"(", ")", ","}:
            tokens.append(char)
            index += 1
        else:
            index += 1
    return tokens


def _after_balanced_parentheses(tokens: list[str], index: int) -> int:
    depth = 0
    while index < len(tokens):
        if tokens[index] == "(":
            depth += 1
        elif tokens[index] == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return index
