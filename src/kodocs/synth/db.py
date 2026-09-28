"""Build the deterministic synthetic ticket and approval SQLite database."""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import random
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from kodocs.ingest.manifest import sha256_file
from kodocs.synth.identifiers import APPROVAL_RE, INCIDENT_RE, TICKET_RE
from kodocs.synth.seed import GRADES, TEAMS, Seed, SeedError, load_seed

DB_PATH = Path("data/synthetic/ledger.sqlite")
MANIFEST_PATH = Path("data/manifest/synth_db.json")
SCHEMA_VERSION = 1

_RANDOM_OFFSET = 2402
_KST = timezone(timedelta(hours=9))
_TABLE_COLUMNS = {
    "teams": ("team", "name_ko", "project_key"),
    "people": ("person_id", "name", "team", "grade", "grade_name", "role"),
    "services": ("service", "owner_team", "description"),
    "incidents": (
        "incident_id",
        "service",
        "severity",
        "started_at",
        "resolved_at",
        "owner_team",
    ),
    "tickets": (
        "ticket_id",
        "project_key",
        "owner_team",
        "title",
        "ticket_type",
        "status",
        "priority",
        "service",
        "incident_id",
        "reporter_id",
        "assignee_id",
        "created_at",
        "resolved_at",
        "story_points",
        "access_level",
        "access_teams",
        "access_min_grade",
        "is_anchor",
        "fact_ids",
    ),
    "approvals": (
        "approval_id",
        "owner_team",
        "category",
        "title",
        "amount_krw",
        "status",
        "requester_id",
        "approver_id",
        "submitted_at",
        "decided_at",
        "regulation_ref",
        "access_level",
        "access_teams",
        "access_min_grade",
        "is_anchor",
        "fact_ids",
    ),
}

_SCHEMA = """
CREATE TABLE teams (
    team TEXT PRIMARY KEY,
    name_ko TEXT NOT NULL,
    project_key TEXT NOT NULL UNIQUE
);
CREATE TABLE people (
    person_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    team TEXT NOT NULL REFERENCES teams(team),
    grade INTEGER NOT NULL,
    grade_name TEXT NOT NULL,
    role TEXT NOT NULL
);
CREATE TABLE services (
    service TEXT PRIMARY KEY,
    owner_team TEXT NOT NULL REFERENCES teams(team),
    description TEXT NOT NULL
);
CREATE TABLE incidents (
    incident_id TEXT PRIMARY KEY,
    service TEXT NOT NULL REFERENCES services(service),
    severity TEXT NOT NULL,
    started_at TEXT NOT NULL,
    resolved_at TEXT,
    owner_team TEXT NOT NULL REFERENCES teams(team)
);
CREATE TABLE tickets (
    ticket_id TEXT PRIMARY KEY,
    project_key TEXT NOT NULL,
    owner_team TEXT NOT NULL REFERENCES teams(team),
    title TEXT NOT NULL,
    ticket_type TEXT NOT NULL,
    status TEXT NOT NULL,
    priority TEXT NOT NULL,
    service TEXT NOT NULL REFERENCES services(service),
    incident_id TEXT REFERENCES incidents(incident_id),
    reporter_id TEXT NOT NULL REFERENCES people(person_id),
    assignee_id TEXT NOT NULL REFERENCES people(person_id),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    story_points INTEGER,
    access_level TEXT NOT NULL,
    access_teams TEXT NOT NULL,
    access_min_grade INTEGER NOT NULL,
    is_anchor INTEGER NOT NULL,
    fact_ids TEXT NOT NULL
);
CREATE TABLE approvals (
    approval_id TEXT PRIMARY KEY,
    owner_team TEXT NOT NULL REFERENCES teams(team),
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    amount_krw INTEGER NOT NULL,
    status TEXT NOT NULL,
    requester_id TEXT NOT NULL REFERENCES people(person_id),
    approver_id TEXT NOT NULL REFERENCES people(person_id),
    submitted_at TEXT NOT NULL,
    decided_at TEXT,
    regulation_ref TEXT,
    access_level TEXT NOT NULL,
    access_teams TEXT NOT NULL,
    access_min_grade INTEGER NOT NULL,
    is_anchor INTEGER NOT NULL,
    fact_ids TEXT NOT NULL
);
"""


def build_rows(seed: Seed) -> dict[str, list[dict[str, object]]]:
    """Build every table row in primary-key order from one deterministic RNG."""
    try:
        random_seed = int(seed.company["random_seed"])
        period = seed.company["period"]
        if not isinstance(period, Mapping):
            raise TypeError("company period must be a mapping")
        period_start = date.fromisoformat(str(period["start"]))
        period_end = date.fromisoformat(str(period["end"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise SeedError(f"invalid company generation settings: {exc}") from exc

    rng = random.Random(random_seed + _RANDOM_OFFSET)
    people_by_team = _people_by_team(seed)
    services_by_team = _services_by_team(seed)
    facts_by_subject = _fact_ids_by_subject(seed)
    ticket_anchors = [
        record
        for entity_id, record in seed.entities.items()
        if TICKET_RE.fullmatch(entity_id)
    ]
    approval_anchors = [
        record
        for entity_id, record in seed.entities.items()
        if APPROVAL_RE.fullmatch(entity_id)
    ]

    rows = {
        "teams": [
            {
                "team": team,
                "name_ko": details.name_ko,
                "project_key": details.project_key,
            }
            for team, details in sorted(TEAMS.items())
        ],
        "people": [
            {
                "person_id": person.person_id,
                "name": person.name,
                "team": person.team,
                "grade": person.grade,
                "grade_name": GRADES[person.grade],
                "role": person.role,
            }
            for person in sorted(seed.people, key=lambda item: item.person_id)
        ],
        "services": [
            {
                "service": service.name,
                "owner_team": service.owner_team,
                "description": service.description,
            }
            for service in sorted(seed.services, key=lambda item: item.name)
        ],
        "incidents": _incident_rows(seed),
        "tickets": _ticket_rows(
            seed,
            ticket_anchors,
            people_by_team,
            services_by_team,
            facts_by_subject,
            rng,
            period_start,
            period_end,
        ),
        "approvals": _approval_rows(
            seed,
            approval_anchors,
            people_by_team,
            facts_by_subject,
            rng,
            period_start,
            period_end,
        ),
    }
    return rows


def write_db(rows: Mapping[str, Sequence[Mapping[str, object]]], path: Path) -> None:
    """Write a complete SQLite database to a sibling temp file, then replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
        with sqlite3.connect(temporary) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(_SCHEMA)
            for table, columns in _TABLE_COLUMNS.items():
                placeholders = ", ".join(f":{column}" for column in columns)
                column_list = ", ".join(columns)
                connection.executemany(
                    f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})",
                    rows[table],
                )
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(
                    f"foreign key check failed: {violations[0]}"
                )
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def content_hash(rows: Mapping[str, Sequence[Mapping[str, object]]]) -> str:
    """Hash canonical JSON row contents rather than SQLite file bytes."""
    normalized = {
        table: [dict(sorted(row.items())) for row in table_rows]
        for table, table_rows in sorted(rows.items())
    }
    serialized = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    """Build the ledger DB and overwrite its deterministic manifest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    root = args.root.resolve()
    seed = load_seed(root)
    rows = build_rows(seed)
    database_path = root / DB_PATH
    manifest_path = root / MANIFEST_PATH
    write_db(rows, database_path)

    access_counts = {
        table: dict(sorted(Counter(row["access_level"] for row in rows[table]).items()))
        for table in ("tickets", "approvals")
    }
    anchor_counts = {
        table: sum(int(row["is_anchor"]) for row in rows[table])
        for table in ("tickets", "approvals")
    }
    seed_files = sorted((root / "data/seed").rglob("*.yaml"))
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "db_path": DB_PATH.as_posix(),
        "content_hash": content_hash(rows),
        "row_counts": {
            table: len(table_rows) for table, table_rows in sorted(rows.items())
        },
        "access_counts": access_counts,
        "anchor_counts": anchor_counts,
        "random_seed": int(seed.company["random_seed"]),
        "seed_files_sha256": {
            path.relative_to(root).as_posix(): sha256_file(path) for path in seed_files
        },
    }
    _write_json(manifest_path, manifest)
    print(
        f"built {database_path.relative_to(root)} "
        f"(content_hash={manifest['content_hash']})"
    )
    return 0


def _ticket_rows(
    seed: Seed,
    anchors: Sequence[Mapping[str, object]],
    people_by_team: Mapping[str, Sequence[str]],
    services_by_team: Mapping[str, Sequence[str]],
    facts_by_subject: Mapping[str, Sequence[str]],
    rng: random.Random,
    period_start: date,
    period_end: date,
) -> list[dict[str, object]]:
    rules = _rules(seed, "tickets")
    total = _positive_int(rules.get("total"), "tickets.total")
    access_levels = _background_access_levels("tickets", anchors, rules, total, rng)
    if len(anchors) > total:
        raise SeedError(f"anchor tickets count {len(anchors)} exceeds target {total}")

    rows = [
        _ticket_anchor_row(record, facts_by_subject)
        for record in sorted(anchors, key=_id)
    ]
    used_ids = {str(row["ticket_id"]) for row in rows}
    next_number = {details.project_key: 1001 for details in TEAMS.values()}
    for access_level in access_levels:
        owner_team = _weighted_choice(rng, rules.get("team_weights"), "team_weights")
        if owner_team not in people_by_team or owner_team not in services_by_team:
            raise SeedError(f"tickets team has no people or services: {owner_team}")
        project_key = TEAMS[owner_team].project_key
        ticket_id = _next_ticket_id(project_key, next_number, used_ids)
        service = rng.choice(services_by_team[owner_team])
        reporter_id = rng.choice(people_by_team[owner_team])
        assignee_id = rng.choice(people_by_team[owner_team])
        ticket_type = _weighted_choice(rng, rules.get("type_weights"), "type_weights")
        status = _weighted_choice(rng, rules.get("status_weights"), "status_weights")
        priority = _weighted_choice(
            rng, rules.get("priority_weights"), "priority_weights"
        )
        created = _random_instant(
            rng,
            rules.get("monthly_volume_weights"),
            period_start,
            period_end,
        )
        resolved = (
            _later_instant(rng, created, period_end)
            if status in {"resolved", "closed"}
            else None
        )
        rows.append(
            {
                "ticket_id": ticket_id,
                "project_key": project_key,
                "owner_team": owner_team,
                "title": _ticket_title(ticket_type, service),
                "ticket_type": ticket_type,
                "status": status,
                "priority": priority,
                "service": service,
                "incident_id": None,
                "reporter_id": reporter_id,
                "assignee_id": assignee_id,
                "created_at": _iso(created),
                "resolved_at": None if resolved is None else _iso(resolved),
                "story_points": (
                    rng.choice((1, 2, 3, 5, 8, 13))
                    if ticket_type in {"task", "improvement"}
                    else None
                ),
                **_access_columns(access_level, owner_team),
                "is_anchor": 0,
                "fact_ids": "",
            }
        )
    return sorted(rows, key=lambda row: str(row["ticket_id"]))


def _approval_rows(
    seed: Seed,
    anchors: Sequence[Mapping[str, object]],
    people_by_team: Mapping[str, Sequence[str]],
    facts_by_subject: Mapping[str, Sequence[str]],
    rng: random.Random,
    period_start: date,
    period_end: date,
) -> list[dict[str, object]]:
    rules = _rules(seed, "approvals")
    total = _positive_int(rules.get("total"), "approvals.total")
    access_levels = _background_access_levels("approvals", anchors, rules, total, rng)
    if len(anchors) > total:
        raise SeedError(f"anchor approvals count {len(anchors)} exceeds target {total}")

    heads = {
        team: next(
            (
                person.person_id
                for person in seed.people
                if person.team == team and person.grade == 5
            ),
            None,
        )
        for team in TEAMS
    }
    rows = [
        _approval_anchor_row(record, facts_by_subject)
        for record in sorted(anchors, key=_id)
    ]
    used_ids = {str(row["approval_id"]) for row in rows}
    next_number = {year: 1 for year in range(period_start.year, period_end.year + 1)}
    team_weights = _approval_team_weights(people_by_team)
    ranges = rules.get("category_amount_ranges")
    if not isinstance(ranges, Mapping):
        raise SeedError("approvals.category_amount_ranges must be a mapping")
    regulation_refs = _regulation_refs(seed)

    for access_level in access_levels:
        owner_team = _weighted_choice(rng, team_weights, "approval team weights")
        category = _weighted_choice(
            rng, rules.get("category_weights"), "category_weights"
        )
        bounds = ranges.get(category)
        if not isinstance(bounds, Mapping):
            raise SeedError(f"missing approval amount range: {category}")
        minimum = _positive_int(bounds.get("min"), f"{category}.min")
        maximum = _positive_int(bounds.get("max"), f"{category}.max")
        if minimum >= maximum:
            raise SeedError(f"invalid approval amount range: {category}")
        amount = rng.randint(minimum, maximum)
        submitted = _random_instant(
            rng,
            rules.get("monthly_volume_weights"),
            period_start,
            period_end,
        )
        approval_id = _next_approval_id(submitted.year, next_number, used_ids)
        status = _weighted_choice(rng, rules.get("status_weights"), "status_weights")
        decided = (
            _later_instant(rng, submitted, period_end)
            if status in {"approved", "rejected"}
            else None
        )
        requester_id = rng.choice(people_by_team[owner_team])
        amount_limit = maximum
        approver_team = "finance" if amount > amount_limit else owner_team
        approver_id = heads.get(approver_team)
        if approver_id is None:
            raise SeedError(f"approval team has no grade 5 head: {approver_team}")
        rows.append(
            {
                "approval_id": approval_id,
                "owner_team": owner_team,
                "category": category,
                "title": _approval_title(owner_team, category),
                "amount_krw": amount,
                "status": status,
                "requester_id": requester_id,
                "approver_id": approver_id,
                "submitted_at": _iso(submitted),
                "decided_at": None if decided is None else _iso(decided),
                "regulation_ref": (
                    None if not regulation_refs else rng.choice(regulation_refs)
                ),
                **_access_columns(access_level, owner_team),
                "is_anchor": 0,
                "fact_ids": "",
            }
        )
    return sorted(rows, key=lambda row: str(row["approval_id"]))


def _ticket_anchor_row(
    record: Mapping[str, object], facts_by_subject: Mapping[str, Sequence[str]]
) -> dict[str, object]:
    ticket_id = str(record["id"])
    owner_team = str(record["owner_team"])
    created = _anchor_instant(record["date"], 9)
    status = str(record["status"])
    resolved = (
        _anchor_instant(record["date"], 18)
        if status in {"resolved", "closed"}
        else None
    )
    return {
        "ticket_id": ticket_id,
        "project_key": TEAMS[owner_team].project_key,
        "owner_team": owner_team,
        "title": str(record["title"]),
        "ticket_type": str(record["ticket_type"]),
        "status": status,
        "priority": str(record["priority"]),
        "service": str(record["service"]),
        "incident_id": (
            None if record.get("incident_id") is None else str(record["incident_id"])
        ),
        "reporter_id": str(record["reporter_id"]),
        "assignee_id": str(record["assignee_id"]),
        "created_at": _iso(created),
        "resolved_at": None if resolved is None else _iso(resolved),
        "story_points": (
            None if record.get("story_points") is None else int(record["story_points"])
        ),
        **_anchor_access(record, owner_team),
        "is_anchor": 1,
        "fact_ids": ",".join(facts_by_subject.get(ticket_id, ())),
    }


def _approval_anchor_row(
    record: Mapping[str, object], facts_by_subject: Mapping[str, Sequence[str]]
) -> dict[str, object]:
    approval_id = str(record["id"])
    owner_team = str(record["owner_team"])
    submitted = _anchor_instant(record["date"], 9)
    status = str(record["status"])
    decided = (
        _anchor_instant(record["date"], 18)
        if status in {"approved", "rejected"}
        else None
    )
    return {
        "approval_id": approval_id,
        "owner_team": owner_team,
        "category": str(record["category"]),
        "title": str(record["title"]),
        "amount_krw": int(record["amount"]),
        "status": status,
        "requester_id": str(record["requester_id"]),
        "approver_id": str(record["approver_id"]),
        "submitted_at": _iso(submitted),
        "decided_at": None if decided is None else _iso(decided),
        "regulation_ref": (
            None
            if record.get("regulation_ref") is None
            else str(record["regulation_ref"])
        ),
        **_anchor_access(record, owner_team),
        "is_anchor": 1,
        "fact_ids": ",".join(facts_by_subject.get(approval_id, ())),
    }


def _incident_rows(seed: Seed) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for incident_id, record in sorted(seed.entities.items()):
        if not INCIDENT_RE.fullmatch(incident_id):
            continue
        instants = sorted(
            datetime.fromisoformat(str(fact.value)).replace(tzinfo=_KST)
            for fact in seed.facts.values()
            if fact.subject == incident_id and fact.kind == "datetime"
        )
        if instants:
            started = instants[0]
            resolved = instants[-1]
        else:
            started = _anchor_instant(record["date"], 9)
            resolved = _anchor_instant(record["date"], 18)
        service = record.get("service")
        if service is None:
            service = next(
                (
                    fact.value
                    for fact in seed.facts.values()
                    if fact.subject == incident_id and fact.kind == "service"
                ),
                None,
            )
        if service is None:
            raise SeedError(f"incident has no service: {incident_id}")
        rows.append(
            {
                "incident_id": incident_id,
                "service": str(service),
                "severity": str(record.get("severity", "unknown")),
                "started_at": _iso(started),
                "resolved_at": _iso(resolved),
                "owner_team": str(record["owner_team"]),
            }
        )
    return rows


def _background_access_levels(
    table: str,
    anchors: Sequence[Mapping[str, object]],
    rules: Mapping[str, object],
    total: int,
    rng: random.Random,
) -> list[str]:
    target = rules.get("access")
    if not isinstance(target, Mapping):
        raise SeedError(f"{table}.access must be a mapping")
    levels = ("public", "team", "restricted")
    try:
        target_counts = {level: int(target[level]) for level in levels}
    except (KeyError, TypeError, ValueError) as exc:
        raise SeedError(f"invalid {table} access target: {exc}") from exc
    if any(count < 0 for count in target_counts.values()):
        raise SeedError(f"negative {table} access target")
    if sum(target_counts.values()) != total:
        raise SeedError(f"{table} access targets do not sum to total {total}")
    anchor_counts = Counter(str(record.get("access_level")) for record in anchors)
    background: list[str] = []
    for level in levels:
        if anchor_counts[level] > target_counts[level]:
            raise SeedError(
                f"anchor {table} {level} count {anchor_counts[level]} "
                f"exceeds target {target_counts[level]}"
            )
        background.extend([level] * (target_counts[level] - anchor_counts[level]))
    rng.shuffle(background)
    return background


def _anchor_access(record: Mapping[str, object], owner_team: str) -> dict[str, object]:
    level = str(record.get("access_level"))
    teams = record.get("teams")
    if not isinstance(teams, Sequence) or isinstance(teams, (str, bytes)):
        raise SeedError(f"anchor access teams must be a list: {record.get('id')}")
    values = [str(team) for team in teams]
    minimum = int(record.get("min_grade", 0))
    expected = _access_columns(level, owner_team)
    actual = {
        "access_level": level,
        "access_teams": ",".join(values),
        "access_min_grade": minimum,
    }
    if actual != expected:
        raise SeedError(f"invalid anchor access: {record.get('id')}")
    return actual


def _access_columns(level: str, owner_team: str) -> dict[str, object]:
    if level == "public":
        return {
            "access_level": "public",
            "access_teams": "",
            "access_min_grade": 1,
        }
    if level == "team":
        return {
            "access_level": "team",
            "access_teams": owner_team,
            "access_min_grade": 1,
        }
    if level == "restricted":
        return {
            "access_level": "restricted",
            "access_teams": owner_team,
            "access_min_grade": 4,
        }
    raise SeedError(f"unknown access level: {level}")


def _people_by_team(seed: Seed) -> dict[str, tuple[str, ...]]:
    return {
        team: tuple(
            sorted(person.person_id for person in seed.people if person.team == team)
        )
        for team in TEAMS
        if any(person.team == team for person in seed.people)
    }


def _services_by_team(seed: Seed) -> dict[str, tuple[str, ...]]:
    return {
        team: tuple(
            sorted(
                service.name for service in seed.services if service.owner_team == team
            )
        )
        for team in TEAMS
        if any(service.owner_team == team for service in seed.services)
    }


def _fact_ids_by_subject(seed: Seed) -> dict[str, tuple[str, ...]]:
    subjects = {fact.subject for fact in seed.facts.values()}
    return {
        subject: tuple(
            sorted(
                fact.fact_id for fact in seed.facts.values() if fact.subject == subject
            )
        )
        for subject in subjects
    }


def _rules(seed: Seed, table: str) -> Mapping[str, object]:
    value = seed.ledger_rules.get(table)
    if not isinstance(value, Mapping):
        raise SeedError(f"ledger rules missing mapping: {table}")
    return value


def _weighted_choice(rng: random.Random, value: object, subject: str) -> str:
    if not isinstance(value, Mapping) or not value:
        raise SeedError(f"{subject} must be a non-empty mapping")
    try:
        pairs = sorted((str(key), float(weight)) for key, weight in value.items())
    except (TypeError, ValueError) as exc:
        raise SeedError(f"invalid {subject}: {exc}") from exc
    if (
        any(weight < 0 for _, weight in pairs)
        or sum(weight for _, weight in pairs) <= 0
    ):
        raise SeedError(f"invalid {subject} weights")
    names, weights = zip(*pairs, strict=True)
    return rng.choices(names, weights=weights, k=1)[0]


def _random_instant(
    rng: random.Random,
    raw_weights: object,
    period_start: date,
    period_end: date,
) -> datetime:
    months = _month_starts(period_start, period_end)
    if not isinstance(raw_weights, Sequence) or isinstance(raw_weights, (str, bytes)):
        raise SeedError("monthly_volume_weights must be a sequence")
    try:
        weights = [float(weight) for weight in raw_weights]
    except (TypeError, ValueError) as exc:
        raise SeedError(f"invalid monthly_volume_weights: {exc}") from exc
    if len(weights) != len(months) or any(weight < 0 for weight in weights):
        raise SeedError("monthly_volume_weights must match the company period")
    month = rng.choices(months, weights=weights, k=1)[0]
    last_day = calendar.monthrange(month.year, month.month)[1]
    first = max(
        period_start.day
        if month.year == period_start.year and month.month == period_start.month
        else 1,
        1,
    )
    last = min(
        period_end.day
        if month.year == period_end.year and month.month == period_end.month
        else last_day,
        last_day,
    )
    chosen = date(month.year, month.month, rng.randint(first, last))
    return datetime.combine(
        chosen,
        time(rng.randint(8, 17), rng.randint(0, 59), rng.randint(0, 59)),
        tzinfo=_KST,
    )


def _later_instant(rng: random.Random, started: datetime, period_end: date) -> datetime:
    candidate = started + timedelta(
        hours=rng.randint(1, 72), minutes=rng.randint(0, 59)
    )
    latest = datetime.combine(period_end, time(23, 59, 59), tzinfo=_KST)
    return min(candidate, latest)


def _month_starts(start: date, end: date) -> list[date]:
    months: list[date] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(date(year, month, 1))
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return months


def _next_ticket_id(
    project_key: str, next_number: dict[str, int], used_ids: set[str]
) -> str:
    number = next_number[project_key]
    while f"{project_key}-{number:04d}" in used_ids:
        number += 1
    if number > 9999:
        raise SeedError(f"ticket id range exhausted: {project_key}")
    identifier = f"{project_key}-{number:04d}"
    next_number[project_key] = number + 1
    used_ids.add(identifier)
    return identifier


def _next_approval_id(
    year: int, next_number: dict[int, int], used_ids: set[str]
) -> str:
    number = next_number[year]
    while f"EXP-{year}-{number:04d}" in used_ids:
        number += 1
    if number > 9999:
        raise SeedError(f"approval id range exhausted: {year}")
    identifier = f"EXP-{year}-{number:04d}"
    next_number[year] = number + 1
    used_ids.add(identifier)
    return identifier


def _approval_team_weights(
    people_by_team: Mapping[str, Sequence[str]],
) -> dict[str, float]:
    return {team: float(len(people)) for team, people in sorted(people_by_team.items())}


def _regulation_refs(seed: Seed) -> tuple[str, ...]:
    values = []
    for item in seed.regulation_refs:
        name = item.get("name", item.get("regulation_name"))
        article = item.get("article", item.get("article_key"))
        direct = item.get("ref", item.get("regulation_ref"))
        if isinstance(direct, str):
            values.append(direct)
        elif isinstance(name, str) and isinstance(article, str):
            values.append(f"{name} {article}")
    return tuple(sorted(set(values)))


def _ticket_title(ticket_type: str, service: str) -> str:
    endings = {
        "incident": "장애 원인 조사",
        "task": "운영 항목 점검",
        "improvement": "사용 흐름 개선",
        "request": "지원 요청 처리",
    }
    try:
        return f"{service} {endings[ticket_type]}"
    except KeyError as exc:
        raise SeedError(f"unknown ticket type: {ticket_type}") from exc


def _approval_title(owner_team: str, category: str) -> str:
    endings = {
        "travel": "업무 출장비 신청",
        "equipment": "업무 장비 구매",
        "education": "직무 교육 참가",
        "contract": "운영 계약 검토",
        "welfare": "복지 비용 신청",
    }
    try:
        return f"{TEAMS[owner_team].name_ko} {endings[category]}"
    except KeyError as exc:
        raise SeedError(f"unknown approval category: {category}") from exc


def _anchor_instant(value: object, hour: int) -> datetime:
    return datetime.combine(date.fromisoformat(str(value)), time(hour), tzinfo=_KST)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _positive_int(value: object, subject: str) -> int:
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise SeedError(f"{subject} must be an integer") from exc
    if result <= 0:
        raise SeedError(f"{subject} must be positive")
    return result


def _id(record: Mapping[str, object]) -> str:
    return str(record["id"])


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            )
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
