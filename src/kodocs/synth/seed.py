"""Load and validate the deterministic source-of-truth for synthetic data."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path
from types import MappingProxyType
from typing import Literal, NamedTuple

import yaml

from kodocs.synth.identifiers import (
    APPROVAL_RE,
    INCIDENT_RE,
    REGULATION_REF_RE,
    TICKET_RE,
    service_pattern,
)

PERIOD_START = date(2025, 1, 1)
PERIOD_END = date(2026, 6, 30)


class TeamCode(NamedTuple):
    """Stable Korean team name and ticket project key."""

    name_ko: str
    project_key: str


TEAMS: Mapping[str, TeamCode] = MappingProxyType(
    {
        "platform": TeamCode("플랫폼개발", "PLT"),
        "payments": TeamCode("결제서비스", "PAY"),
        "data-ai": TeamCode("데이터AI", "DAI"),
        "sre": TeamCode("SRE인프라", "SRE"),
        "security": TeamCode("정보보안", "SEC"),
        "hr": TeamCode("인사", "HRM"),
        "finance": TeamCode("재무", "FIN"),
        "legal": TeamCode("법무컴플라이언스", "LGL"),
    }
)
GRADES: Mapping[int, str] = MappingProxyType(
    {1: "사원", 2: "선임", 3: "책임", 4: "수석", 5: "팀장", 6: "임원"}
)

type AccessLevel = Literal["public", "team", "restricted"]
type Sensitivity = Literal["normal", "restricted"]
type FactKind = Literal[
    "id",
    "date",
    "datetime",
    "amount",
    "person",
    "service",
    "count",
    "text",
    "regulation",
]

_FACT_ID_RE = re.compile(r"^f-[a-z0-9]+(?:-[a-z0-9]+)*$")
_PERSON_ID_RE = re.compile(r"^p-\d{3}$")
_DATE_VALUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_VALUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
_SERVICE_VALUE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_PERSON_FIELDS = (
    "assignee",
    "assignee_id",
    "owner_person",
    "owner_person_id",
    "reporter",
    "reporter_id",
    "requester",
    "requester_id",
    "approver",
    "approver_id",
    "person_id",
)


@dataclass(frozen=True)
class Access:
    level: AccessLevel
    teams: tuple[str, ...]
    min_grade: int

    def __post_init__(self) -> None:
        if self.level == "public":
            valid = self.teams == () and self.min_grade == 1
        elif self.level == "team":
            valid = bool(self.teams) and self.min_grade == 1
        elif self.level == "restricted":
            valid = bool(self.teams) and self.min_grade >= 4
        else:
            valid = False
        if not valid:
            raise ValueError(
                "invalid access: public requires teams=(), min_grade=1; "
                "team requires teams and min_grade=1; restricted requires teams "
                "and min_grade>=4"
            )
        unknown = sorted(set(self.teams).difference(TEAMS))
        if unknown:
            raise ValueError(f"invalid access team: {unknown[0]}")


@dataclass(frozen=True)
class Person:
    person_id: str
    name: str
    team: str
    grade: int
    role: str
    joined: date


@dataclass(frozen=True)
class Fact:
    fact_id: str
    kind: FactKind
    value: object
    subject: str
    date: date | None
    sensitivity: Sensitivity
    source: str


@dataclass(frozen=True)
class Service:
    name: str
    owner_team: str
    description: str
    facts: tuple[Fact, ...]


@dataclass(frozen=True)
class Seed:
    company: Mapping[str, object]
    people: tuple[Person, ...]
    services: tuple[Service, ...]
    facts: Mapping[str, Fact]
    entities: Mapping[str, Mapping[str, object]]
    regulation_refs: tuple[Mapping[str, object], ...]
    banned_names: tuple[str, ...]
    ledger_rules: Mapping[str, object]
    public_figures: tuple[str, ...] = ()


class SeedError(ValueError):
    """One deterministic report containing every seed loading violation."""

    def __init__(self, errors: str | Sequence[str]) -> None:
        self.errors = (errors,) if isinstance(errors, str) else tuple(errors)
        super().__init__(
            "Seed errors:\n" + "\n".join(f"- {item}" for item in self.errors)
        )


def load_seed(root: Path) -> Seed:
    """Load ``root/data/seed`` in sorted order and fill derived fact fields."""
    seed_dir = root / "data/seed"
    errors: list[str] = []
    if not seed_dir.is_dir():
        raise SeedError(f"seed directory does not exist: {seed_dir}")

    company = _mapping_file(seed_dir / "company.yaml", errors)
    _check_company_period(company, errors)
    _check_team_file(seed_dir / "teams.yaml", errors)

    people: list[Person] = []
    for path in sorted((seed_dir / "people").glob("*.yaml")):
        payload = _yaml_file(path, errors)
        for index, record in enumerate(_records(payload, ("people",), path, errors)):
            try:
                people.append(
                    Person(
                        person_id=_required_string(record, "person_id"),
                        name=_required_string(record, "name"),
                        team=_required_string(record, "team"),
                        grade=int(record["grade"]),
                        role=_required_string(record, "role"),
                        joined=_parse_date(record["joined"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f"{_relative(path, root)} people[{index}]: {exc}")

    facts: dict[str, Fact] = {}
    services: list[Service] = []
    services_path = seed_dir / "services.yaml"
    services_payload = _yaml_file(services_path, errors)
    for index, record in enumerate(
        _records(services_payload, ("services",), services_path, errors)
    ):
        try:
            name = _required_string(record, "name")
            owner_team = _required_string(record, "owner_team")
            description = _required_string(record, "description")
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"{_relative(services_path, root)} services[{index}]: {exc}")
            continue
        service_facts = _load_facts(
            record,
            subject=name,
            inherited_sensitivity=str(record.get("sensitivity", "normal")),
            path=services_path,
            root=root,
            facts=facts,
            errors=errors,
        )
        services.append(Service(name, owner_team, description, service_facts))

    entities: dict[str, Mapping[str, object]] = {}
    entity_sources = [
        *(
            (path, ("incidents",))
            for path in sorted((seed_dir / "incidents").glob("*.yaml"))
        ),
        *((path, ("events",)) for path in sorted((seed_dir / "events").glob("*.yaml"))),
        (seed_dir / "ledger/tickets_anchor.yaml", ("tickets",)),
        (seed_dir / "ledger/approvals_anchor.yaml", ("approvals",)),
    ]
    for path, keys in entity_sources:
        payload = _yaml_file(path, errors)
        for index, record in enumerate(_records(payload, keys, path, errors)):
            try:
                entity_id = _required_string(record, "id")
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f"{_relative(path, root)} record[{index}]: {exc}")
                continue
            if entity_id in entities:
                errors.append(f"duplicate entity id: {entity_id}")
            else:
                entities[entity_id] = dict(record)
            _load_facts(
                record,
                subject=entity_id,
                inherited_sensitivity=str(record.get("sensitivity", "normal")),
                path=path,
                root=root,
                facts=facts,
                errors=errors,
            )

    refs_path = seed_dir / "regulation_refs.yaml"
    refs_payload = _yaml_file(refs_path, errors)
    regulation_refs = tuple(
        dict(record)
        for record in _records(refs_payload, ("regulation_refs",), refs_path, errors)
    )
    ledger_rules = _mapping_file(seed_dir / "ledger/rules.yaml", errors)
    try:
        banned_names = load_banned_names(root)
        public_figures = load_public_figures(root)
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        errors.append(f"data/seed/banned_names.yaml: {exc}")
        banned_names = ()
        public_figures = ()

    if errors:
        raise SeedError(errors)
    return Seed(
        company=company,
        people=tuple(people),
        services=tuple(services),
        facts=MappingProxyType(facts),
        entities=MappingProxyType(entities),
        regulation_refs=regulation_refs,
        banned_names=banned_names,
        ledger_rules=ledger_rules,
        public_figures=public_figures,
    )


def validate_seed(seed: Seed, *, regulation_index: Mapping[str, object]) -> list[str]:
    """Return every contract violation in deterministic check order."""
    errors: list[str] = []
    person_ids: set[str] = set()
    for person in seed.people:
        if not _PERSON_ID_RE.fullmatch(person.person_id):
            errors.append(f"person_id format: {person.person_id}")
        if person.person_id in person_ids:
            errors.append(f"duplicate person_id: {person.person_id}")
        person_ids.add(person.person_id)

    seen_fact_ids: set[str] = set()
    for fact in seed.facts.values():
        if not _FACT_ID_RE.fullmatch(fact.fact_id):
            errors.append(f"fact_id format: {fact.fact_id}")
        if fact.fact_id in seen_fact_ids:
            errors.append(f"duplicate fact_id: {fact.fact_id}")
        seen_fact_ids.add(fact.fact_id)

    record_ids: set[str] = set()
    for key, record in seed.entities.items():
        record_id = record.get("id")
        if not isinstance(record_id, str):
            errors.append(f"entity id missing or not string: {key}")
            continue
        if record_id in record_ids:
            errors.append(f"duplicate entity id: {record_id}")
        record_ids.add(record_id)
        if key != record_id:
            errors.append(f"entity key/id mismatch: {key} != {record_id}")
        _validate_entity_identifier(record, errors)

    for fact in seed.facts.values():
        if fact.date is not None and not _in_period(fact.date):
            errors.append(
                f"fact date outside period: {fact.fact_id}={fact.date.isoformat()}"
            )
    for entity_id, record in seed.entities.items():
        entity_date = _entity_date(record)
        if entity_date is None:
            errors.append(f"entity date missing or invalid: {entity_id}")
        elif not _in_period(entity_date):
            errors.append(
                f"entity date outside period: {entity_id}={entity_date.isoformat()}"
            )
    for person in seed.people:
        if person.joined > PERIOD_END:
            errors.append(
                f"person joined outside period end: {person.person_id}={person.joined.isoformat()}"
            )

    service_names = {service.name for service in seed.services}
    for person in seed.people:
        _check_team(person.team, f"person {person.person_id}", errors)
        if person.grade not in GRADES:
            errors.append(f"unknown grade: {person.person_id}={person.grade}")
    for service in seed.services:
        _check_team(service.owner_team, f"service {service.name}", errors)
        try:
            service_pattern((service.name,))
        except ValueError:
            errors.append(f"service name format: {service.name}")
    for entity_id, record in seed.entities.items():
        owner_team = record.get("owner_team")
        if not isinstance(owner_team, str) or owner_team not in TEAMS:
            errors.append(f"unknown team: entity {entity_id}={owner_team}")
        for field in _PERSON_FIELDS:
            value = record.get(field)
            if value is not None and value not in person_ids:
                errors.append(f"unknown person: entity {entity_id}.{field}={value}")
        service = record.get("service")
        if service is not None and service not in service_names:
            errors.append(f"unknown service: entity {entity_id}.service={service}")
        if TICKET_RE.fullmatch(entity_id):
            incident_id = record.get("incident_id")
            if incident_id is not None and incident_id not in seed.entities:
                errors.append(f"unknown incident: ticket {entity_id}={incident_id}")

    for fact in seed.facts.values():
        _validate_fact_value(fact, errors)
        if fact.kind == "person" and fact.value not in person_ids:
            errors.append(f"unknown person: fact {fact.fact_id}={fact.value}")
        if fact.kind == "service" and fact.value not in service_names:
            errors.append(f"unknown service: fact {fact.fact_id}={fact.value}")

    _validate_regulations(seed, regulation_index, errors)

    banned_values = {
        "company": seed.company,
        "people": [asdict(person) for person in seed.people],
        "services": [asdict(service) for service in seed.services],
        "entities": seed.entities,
        "regulation_refs": seed.regulation_refs,
        "ledger_rules": seed.ledger_rules,
    }
    for path, value in _string_values(banned_values):
        for name, _, _ in find_banned_names(value, seed.banned_names):
            errors.append(f"banned name in seed: {name} at {path}")
    public_figures = set(seed.public_figures)
    for person in seed.people:
        if person.name in public_figures:
            errors.append(
                f"person name matches public figure: {person.person_id}={person.name}"
            )
    return errors


def surface_forms(fact: Fact, *, people: Mapping[str, Person]) -> tuple[str, ...]:
    """Return the permitted literal renderings for one atomic fact."""
    if fact.kind == "date":
        return _date_forms(_parse_date(fact.value))
    if fact.kind == "datetime":
        date_text, time_text = str(fact.value).split("T")
        instant_date = date.fromisoformat(date_text)
        hour, minute = (int(part) for part in time_text.split(":"))
        time(hour, minute)
        time_forms = (f"{hour:02d}:{minute:02d}", f"{hour}시 {minute}분")
        return tuple(
            f"{date_form} {time_form}"
            for date_form in _date_forms(instant_date)
            for time_form in time_forms
        )
    if fact.kind == "amount":
        amount = int(fact.value)
        forms = [f"{amount:,}원", f"{amount}원"]
        if amount % 10_000 == 0:
            man = amount // 10_000
            forms.extend((f"{man}만 원", f"{man}만원"))
        forms.append(f"{amount:,} 원")
        return tuple(forms)
    if fact.kind == "person":
        person = people[str(fact.value)]
        return (person.name, f"{person.name} {GRADES[person.grade]}")
    if fact.kind == "count":
        count = str(fact.value)
        return (count, f"{count}건", f"{count}개", f"{count}명", f"{count}회")
    if fact.kind in {"id", "service", "regulation"}:
        return (str(fact.value),)
    return ()


def load_banned_names(root: Path) -> tuple[str, ...]:
    """Load the reviewed real-name denylist from the seed directory."""
    path = root / "data/seed/banned_names.yaml"
    with path.open(encoding="utf-8") as source:
        payload = yaml.safe_load(source)
    values = payload.get("names") if isinstance(payload, Mapping) else payload
    if not isinstance(values, list) or not all(
        isinstance(value, str) and value for value in values
    ):
        raise ValueError("banned_names.yaml must contain a non-empty string list")
    return tuple(values)


def load_public_figures(root: Path) -> tuple[str, ...]:
    """Load public-figure names that generated person names must not equal."""
    path = root / "data/seed/banned_names.yaml"
    with path.open(encoding="utf-8") as source:
        payload = yaml.safe_load(source)
    values = payload.get("public_figures", []) if isinstance(payload, Mapping) else []
    if not isinstance(values, list) or not all(
        isinstance(value, str) and value for value in values
    ):
        raise ValueError("banned_names.yaml public_figures must be a string list")
    return tuple(values)


def find_banned_names(text: str, names: Sequence[str]) -> list[tuple[str, int, int]]:
    """Find denied names; ASCII names require alphanumeric boundaries."""
    found: list[tuple[str, int, int]] = []
    for name in names:
        if not name:
            continue
        if name.isascii():
            pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])")
        else:
            pattern = re.compile(re.escape(name))
        found.extend(
            (name, match.start(), match.end()) for match in pattern.finditer(text)
        )
    return sorted(found, key=lambda item: (item[1], item[2], item[0]))


def _yaml_file(path: Path, errors: list[str]) -> object:
    try:
        with path.open(encoding="utf-8") as source:
            return yaml.safe_load(source)
    except (OSError, yaml.YAMLError) as exc:
        errors.append(f"{path.as_posix()}: {exc}")
        return None


def _mapping_file(path: Path, errors: list[str]) -> Mapping[str, object]:
    payload = _yaml_file(path, errors)
    if isinstance(payload, Mapping):
        return dict(payload)
    errors.append(f"{path.as_posix()}: expected mapping")
    return {}


def _records(
    payload: object,
    keys: Sequence[str],
    path: Path,
    errors: list[str],
) -> list[Mapping[str, object]]:
    raw_records: object
    if isinstance(payload, list):
        raw_records = payload
    elif isinstance(payload, Mapping) and "id" in payload:
        raw_records = [payload]
    elif isinstance(payload, Mapping):
        raw_records = next((payload[key] for key in keys if key in payload), None)
    else:
        raw_records = None
    if not isinstance(raw_records, list):
        errors.append(f"{path.as_posix()}: expected record list ({', '.join(keys)})")
        return []
    records: list[Mapping[str, object]] = []
    for index, record in enumerate(raw_records):
        if isinstance(record, Mapping):
            records.append(record)
        else:
            errors.append(f"{path.as_posix()} record[{index}]: expected mapping")
    return records


def _load_facts(
    record: Mapping[str, object],
    *,
    subject: str,
    inherited_sensitivity: str,
    path: Path,
    root: Path,
    facts: dict[str, Fact],
    errors: list[str],
) -> tuple[Fact, ...]:
    raw_facts = record.get("facts", [])
    if not isinstance(raw_facts, list):
        errors.append(f"{_relative(path, root)} {subject}.facts: expected list")
        return ()
    loaded: list[Fact] = []
    for index, raw_fact in enumerate(raw_facts):
        if not isinstance(raw_fact, Mapping):
            errors.append(
                f"{_relative(path, root)} {subject}.facts[{index}]: expected mapping"
            )
            continue
        try:
            fact_id = _required_string(raw_fact, "fact_id")
            kind = _required_string(raw_fact, "kind")
            if "value" not in raw_fact:
                raise KeyError("value")
            fact_date = (
                None if raw_fact.get("date") is None else _parse_date(raw_fact["date"])
            )
            sensitivity = str(raw_fact.get("sensitivity", inherited_sensitivity))
            fact = Fact(
                fact_id=fact_id,
                kind=kind,  # type: ignore[arg-type]
                value=_normalize_yaml_value(raw_fact["value"]),
                subject=subject,
                date=fact_date,
                sensitivity=sensitivity,  # type: ignore[arg-type]
                source=_relative(path, root),
            )
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"{_relative(path, root)} {subject}.facts[{index}]: {exc}")
            continue
        if fact.fact_id in facts:
            errors.append(f"duplicate fact_id: {fact.fact_id}")
        else:
            facts[fact.fact_id] = fact
        loaded.append(fact)
    return tuple(loaded)


def _check_company_period(company: Mapping[str, object], errors: list[str]) -> None:
    period = company.get("period")
    if period is None:
        return
    if not isinstance(period, Mapping):
        errors.append("company period must be a mapping")
        return
    try:
        actual = (_parse_date(period["start"]), _parse_date(period["end"]))
        if actual != (PERIOD_START, PERIOD_END):
            errors.append("company period must be 2025-01-01 through 2026-06-30")
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(f"company period: {exc}")


def _check_team_file(path: Path, errors: list[str]) -> None:
    payload = _yaml_file(path, errors)
    for index, record in enumerate(_records(payload, ("teams",), path, errors)):
        code = record.get("code")
        if not isinstance(code, str) or code not in TEAMS:
            errors.append(f"teams[{index}] unknown code: {code}")
            continue
        expected = TEAMS[code]
        if record.get("name_ko") != expected.name_ko:
            errors.append(f"teams[{index}] name mismatch: {code}")
        if record.get("project_key") != expected.project_key:
            errors.append(f"teams[{index}] project key mismatch: {code}")


def _validate_entity_identifier(
    record: Mapping[str, object], errors: list[str]
) -> None:
    entity_id = str(record["id"])
    owner_team = record.get("owner_team")
    if entity_id.startswith("INC-"):
        if INCIDENT_RE.fullmatch(entity_id) is None:
            errors.append(f"incident id format: {entity_id}")
    elif any(entity_id.startswith(f"{team.project_key}-") for team in TEAMS.values()):
        if TICKET_RE.fullmatch(entity_id) is None:
            errors.append(f"ticket id format: {entity_id}")
        elif isinstance(owner_team, str) and owner_team in TEAMS:
            expected = TEAMS[owner_team].project_key
            if entity_id.split("-", 1)[0] != expected:
                errors.append(
                    f"ticket project key does not match owner team: {entity_id}/{owner_team}"
                )
    elif entity_id.startswith("EXP-"):
        if APPROVAL_RE.fullmatch(entity_id) is None:
            errors.append(f"approval id format: {entity_id}")
        entity_date = _entity_date(record)
        if entity_date is not None and entity_id.split("-")[1] != str(entity_date.year):
            errors.append(f"approval year does not match submitted date: {entity_id}")


def _validate_fact_value(fact: Fact, errors: list[str]) -> None:
    value = fact.value
    valid = True
    if fact.kind == "id":
        valid = isinstance(value, str) and any(
            pattern.fullmatch(value)
            for pattern in (TICKET_RE, APPROVAL_RE, INCIDENT_RE)
        )
    elif fact.kind == "date":
        valid = _valid_date_value(value)
    elif fact.kind == "datetime":
        valid = _valid_datetime_value(value)
    elif fact.kind in {"amount", "count"}:
        valid = isinstance(value, int) and not isinstance(value, bool) and value > 0
    elif fact.kind == "person":
        valid = isinstance(value, str) and _PERSON_ID_RE.fullmatch(value) is not None
    elif fact.kind == "service":
        valid = (
            isinstance(value, str) and _SERVICE_VALUE_RE.fullmatch(value) is not None
        )
    elif fact.kind == "regulation":
        valid = (
            isinstance(value, str) and REGULATION_REF_RE.fullmatch(value) is not None
        )
    elif fact.kind == "text":
        valid = isinstance(value, str) and bool(value.strip())
    else:
        valid = False
    if not valid:
        errors.append(f"invalid value for kind {fact.kind}: {fact.fact_id}={value!r}")
    if fact.sensitivity not in {"normal", "restricted"}:
        errors.append(f"invalid sensitivity: {fact.fact_id}={fact.sensitivity}")


def _validate_regulations(
    seed: Seed,
    regulation_index: Mapping[str, object],
    errors: list[str],
) -> None:
    rules_value = regulation_index.get("rules", {})
    rules = rules_value if isinstance(rules_value, Mapping) else {}
    for fact in seed.facts.values():
        if (
            fact.kind == "regulation"
            and isinstance(fact.value, str)
            and not _regulation_exists(fact.value, rules)
        ):
            errors.append(
                f"regulation fact not in current index: {fact.fact_id}={fact.value}"
            )
    for index, item in enumerate(seed.regulation_refs):
        reference = _reference_value(item)
        if reference is None or not _regulation_exists(reference, rules):
            errors.append(f"regulation_refs[{index}] not in current index: {reference}")
        teams = item.get("teams", item.get("team", ()))
        if isinstance(teams, str):
            teams = (teams,)
        if isinstance(teams, Sequence):
            for team in teams:
                if team not in TEAMS:
                    errors.append(f"unknown team: regulation_refs[{index}]={team}")
    for entity_id, record in seed.entities.items():
        reference = record.get("regulation_ref")
        if isinstance(reference, str) and not _regulation_exists(reference, rules):
            errors.append(
                f"entity regulation_ref not in current index: {entity_id}={reference}"
            )


def _reference_value(item: Mapping[str, object]) -> str | None:
    direct = item.get("ref") or item.get("regulation_ref")
    if isinstance(direct, str):
        return direct
    name = item.get("name", item.get("regulation_name"))
    article = item.get("article", item.get("article_key"))
    if isinstance(name, str) and isinstance(article, str):
        return f"{name} {article}"
    return None


def _regulation_exists(reference: str, rules: Mapping[object, object]) -> bool:
    match = REGULATION_REF_RE.fullmatch(reference)
    if match is None:
        return False
    name = match.group("name")
    branch = match.group("branch")
    article = f"제{match.group('num')}조" + (f"의{branch}" if branch else "")
    rule = rules.get(name)
    if not isinstance(rule, Mapping):
        return False
    articles = rule.get("articles")
    return isinstance(articles, Mapping) and article in articles


def _entity_date(record: Mapping[str, object]) -> date | None:
    for key in ("date", "start", "submitted_at", "created_at"):
        value = record.get(key)
        if value is None:
            continue
        try:
            if isinstance(value, datetime):
                return value.date()
            if isinstance(value, date):
                return value
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None
    return None


def _parse_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _valid_date_value(value: object) -> bool:
    if not isinstance(value, str) or _DATE_VALUE_RE.fullmatch(value) is None:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _valid_datetime_value(value: object) -> bool:
    if not isinstance(value, str) or _DATETIME_VALUE_RE.fullmatch(value) is None:
        return False
    try:
        date_text, time_text = value.split("T")
        date.fromisoformat(date_text)
        hour, minute = (int(part) for part in time_text.split(":"))
        time(hour, minute)
    except ValueError:
        return False
    return True


def _date_forms(value: date) -> tuple[str, ...]:
    return (
        value.isoformat(),
        f"{value.year}.{value.month}.{value.day}",
        f"{value.year}. {value.month}. {value.day}.",
        f"{value.year}년 {value.month}월 {value.day}일",
        f"{value.month}월 {value.day}일",
    )


def _required_string(record: Mapping[str, object], key: str) -> str:
    value = record[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _normalize_yaml_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat(timespec="minutes")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _in_period(value: date) -> bool:
    return PERIOD_START <= value <= PERIOD_END


def _check_team(team: str, subject: str, errors: list[str]) -> None:
    if team not in TEAMS:
        errors.append(f"unknown team: {subject}={team}")


def _string_values(value: object, path: str = "seed") -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, Mapping):
        for key, nested in value.items():
            yield from _string_values(nested, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            yield from _string_values(nested, f"{path}[{index}]")
