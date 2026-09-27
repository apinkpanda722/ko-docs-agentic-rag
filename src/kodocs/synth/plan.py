"""Build the deterministic plan for 200 synthetic documents."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from kodocs.synth.identifiers import INCIDENT_RE
from kodocs.synth.injection_texts import INJECTION_TEXTS
from kodocs.synth.seed import (
    PERIOD_END,
    PERIOD_START,
    TEAMS,
    Access,
    Seed,
    SeedError,
    load_seed,
)

PLAN_PATH = Path("data/synthetic/plan.jsonl")
CANARIES_PATH = Path("data/synthetic/canaries.json")
INJECTIONS_PATH = Path("data/synthetic/injections.json")
DOC_TYPES = ("meeting", "weekly", "incident", "runbook", "guide", "notice")
TYPE_COUNTS = {
    "meeting": 50,
    "weekly": 40,
    "incident": 25,
    "runbook": 30,
    "guide": 30,
    "notice": 25,
}
ACCESS_COUNTS = {"public": 90, "team": 80, "restricted": 30}
INJECTION_COUNTS = {"public": 6, "team": 6}

_FACT_COUNT_RANGES = {
    "meeting": (4, 8),
    "weekly": (5, 8),
    "incident": (8, 12),
    "runbook": (4, 7),
    "guide": (3, 6),
    "notice": (2, 5),
}
_RESTRICTED_PER_NON_INCIDENT_TYPE = 6
_CANARY_ADJECTIVES = (
    "청록",
    "은빛",
    "금빛",
    "푸른",
    "붉은",
    "맑은",
    "밝은",
    "고운",
    "짙은",
    "하얀",
)
_CANARY_ANIMALS = (
    "고래",
    "수달",
    "여우",
    "참새",
    "사슴",
    "해마",
    "판다",
    "토끼",
    "까치",
    "상어",
)
_TONES = ("간결한", "실무적인", "분석적인", "정중한")
_PERSONAS = ("담당자", "운영 리드", "기술 책임자", "업무 조정자")
_STRUCTURES = {
    "meeting": ("안건-논의-결정-후속조치", "목적-논의사항-결론-담당자"),
    "weekly": ("이번 주 성과-이슈-다음 주 계획", "요약-진행사항-위험-계획"),
    "incident": ("개요-영향-타임라인-원인-재발방지", "요약-탐지-복구-원인-조치"),
    "runbook": ("목적-사전조건-절차-확인-복구", "적용범위-점검-실행-검증-에스컬레이션"),
    "guide": ("개요-대상-절차-주의사항", "목적-준비-사용법-문의"),
    "notice": ("공지사항-적용일-요청사항", "배경-변경사항-일정-문의"),
}
_LENGTH_TARGETS = {
    "meeting": (1800, 2200, 2600),
    "weekly": (1400, 1800, 2200),
    "incident": (2200, 2800, 3400),
    "runbook": (1800, 2400, 3000),
    "guide": (1400, 1800, 2200),
    "notice": (800, 1100, 1400),
}


@dataclass(frozen=True)
class DocPlan:
    doc_id: str
    doc_type: str
    team: str
    access: Access
    author_id: str
    date: str
    title_hint: str
    fact_ids: tuple[str, ...]
    regulation_refs: tuple[str, ...]
    canary_fact_id: str | None
    injection_id: str | None
    style: dict[str, str | int]
    seed: int
    related_doc_ids: tuple[str, ...]


@dataclass
class _Draft:
    doc_id: str
    doc_type: str
    team: str
    access: Access
    seed: int
    topic_subject: str | None = None
    fixed_date: date | None = None
    fact_ids: set[str] = field(default_factory=set)
    regulation_refs: tuple[str, ...] = ()
    canary_fact_id: str | None = None
    injection_id: str | None = None


def build_plan(seed: Seed) -> tuple[list[DocPlan], dict, dict]:
    """Build document plans and the two security registries without I/O."""
    incidents = sorted(
        (
            (entity_id, entity)
            for entity_id, entity in seed.entities.items()
            if INCIDENT_RE.fullmatch(entity_id)
        ),
        key=lambda item: item[0],
    )
    if len(incidents) != TYPE_COUNTS["incident"]:
        raise SeedError(f"document plan requires 25 incidents, found {len(incidents)}")

    fact_teams = _fact_teams(seed)
    restricted_events = sorted(
        (
            (entity_id, entity)
            for entity_id, entity in seed.entities.items()
            if entity_id.startswith("evt-")
            and any(
                fact.sensitivity == "restricted"
                for fact in seed.facts.values()
                if fact.subject == entity_id
            )
        ),
        key=lambda item: item[0],
    )
    if not restricted_events:
        raise SeedError("document plan requires restricted event facts, found none")

    people_by_team: dict[str, list] = defaultdict(list)
    for person in sorted(seed.people, key=lambda item: item.person_id):
        people_by_team[person.team].append(person)
    for team in TEAMS:
        if not people_by_team[team]:
            raise SeedError(f"document plan team has no authors: {team}")
        if not any(person.grade >= 4 for person in people_by_team[team]):
            raise SeedError(f"document plan team has no grade 4 author: {team}")

    drafts: list[_Draft] = []
    normal_index = 0
    normal_team_index = 0
    restricted_index = 0
    incident_index = 0
    for doc_type in DOC_TYPES:
        for number in range(1, TYPE_COUNTS[doc_type] + 1):
            doc_id = f"syn-{doc_type}-{number:03d}"
            doc_seed = _document_seed(seed, doc_id)
            if doc_type == "incident":
                incident_id, entity = incidents[incident_index]
                incident_index += 1
                team = _entity_team(entity, incident_id)
                access = _normal_access(normal_index, team)
                normal_index += 1
                facts = {
                    fact.fact_id
                    for fact in seed.facts.values()
                    if fact.subject == incident_id
                }
                restored = next(
                    (
                        fact
                        for fact in seed.facts.values()
                        if fact.subject == incident_id
                        and fact.fact_id.endswith("-restored")
                    ),
                    None,
                )
                if restored is None:
                    raise SeedError(f"incident has no restored fact: {incident_id}")
                fixed_date = datetime.fromisoformat(str(restored.value)).date()
                drafts.append(
                    _Draft(
                        doc_id,
                        doc_type,
                        team,
                        access,
                        doc_seed,
                        topic_subject=incident_id,
                        fixed_date=fixed_date,
                        fact_ids=facts,
                    )
                )
                continue

            is_restricted = number > (
                TYPE_COUNTS[doc_type] - _RESTRICTED_PER_NON_INCIDENT_TYPE
            )
            if is_restricted:
                event_id, entity = restricted_events[
                    restricted_index % len(restricted_events)
                ]
                restricted_index += 1
                team = _entity_team(entity, event_id)
                facts = {
                    fact.fact_id
                    for fact in seed.facts.values()
                    if fact.subject == event_id and fact.sensitivity == "restricted"
                }
                drafts.append(
                    _Draft(
                        doc_id,
                        doc_type,
                        team,
                        Access("restricted", (team,), 4),
                        doc_seed,
                        topic_subject=event_id,
                        fact_ids=facts,
                    )
                )
            else:
                team = tuple(TEAMS)[normal_team_index % len(TEAMS)]
                normal_team_index += 1
                access = _normal_access(normal_index, team)
                normal_index += 1
                drafts.append(_Draft(doc_id, doc_type, team, access, doc_seed))

    _validate_initial_counts(drafts, normal_index, restricted_index)
    _link_incidents(drafts, seed, incidents)
    _cover_normal_facts(drafts, seed, fact_teams)
    _fill_fact_targets(drafts, seed, fact_teams)
    _assign_regulation_refs(drafts, seed)
    canaries = _assign_canaries(drafts)
    injections = _assign_injections(drafts)

    facts_to_documents: dict[str, set[str]] = defaultdict(set)
    for draft in drafts:
        for fact_id in draft.fact_ids:
            facts_to_documents[fact_id].add(draft.doc_id)

    plans: list[DocPlan] = []
    for draft in drafts:
        fact_ids = tuple(sorted(draft.fact_ids))
        assigned_facts = [
            seed.facts[fact_id] for fact_id in fact_ids if fact_id in seed.facts
        ]
        latest_fact_date = max(
            (fact.date for fact in assigned_facts if fact.date is not None),
            default=PERIOD_START,
        )
        document_date = max(draft.fixed_date or PERIOD_START, latest_fact_date)
        if document_date > PERIOD_END:
            raise SeedError(
                f"document date exceeds period end: {draft.doc_id}={document_date}"
            )
        if (
            draft.doc_type == "incident"
            and draft.fixed_date is not None
            and (document_date - draft.fixed_date).days > 14
        ):
            raise SeedError(
                f"incident document exceeds recovery window: {draft.doc_id}"
            )
        rng = random.Random(draft.seed)
        authors = people_by_team[draft.team]
        if draft.access.level == "restricted":
            authors = [person for person in authors if person.grade >= 4]
        author = rng.choice(authors)
        style = {
            "tone": rng.choice(_TONES),
            "structure": rng.choice(_STRUCTURES[draft.doc_type]),
            "length_target_chars": rng.choice(_LENGTH_TARGETS[draft.doc_type]),
            "persona": rng.choice(_PERSONAS),
        }
        related = sorted(
            {
                other_doc_id
                for fact_id in fact_ids
                for other_doc_id in facts_to_documents[fact_id]
                if other_doc_id != draft.doc_id
            }
        )
        plans.append(
            DocPlan(
                doc_id=draft.doc_id,
                doc_type=draft.doc_type,
                team=draft.team,
                access=draft.access,
                author_id=author.person_id,
                date=document_date.isoformat(),
                title_hint=_title_hint(
                    draft,
                    document_date,
                    seed,
                ),
                fact_ids=fact_ids,
                regulation_refs=draft.regulation_refs,
                canary_fact_id=draft.canary_fact_id,
                injection_id=draft.injection_id,
                style=style,
                seed=draft.seed,
                related_doc_ids=tuple(related),
            )
        )
    return plans, canaries, injections


def write_plan(
    plans: Sequence[DocPlan],
    canaries: Mapping[str, object],
    injections: Mapping[str, object],
    *,
    root: Path | None = None,
) -> None:
    """Atomically replace the deterministic JSONL plan and JSON registries."""
    root = Path.cwd() if root is None else root
    plan_text = "".join(
        json.dumps(_plan_record(plan), ensure_ascii=False, separators=(",", ":")) + "\n"
        for plan in plans
    )
    canary_text = json.dumps(canaries, ensure_ascii=False, indent=2) + "\n"
    injection_text = json.dumps(injections, ensure_ascii=False, indent=2) + "\n"
    for relative_path, text in (
        (PLAN_PATH, plan_text),
        (CANARIES_PATH, canary_text),
        (INJECTIONS_PATH, injection_text),
    ):
        _atomic_write_text(root / relative_path, text)


def main(argv: Sequence[str] | None = None) -> int:
    """Build, write, and summarize the committed document plan."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    root = args.root.resolve()
    seed = load_seed(root)
    plans, canaries, injections = build_plan(seed)
    write_plan(plans, canaries, injections, root=root)

    normal_fact_ids = {
        fact.fact_id for fact in seed.facts.values() if fact.sensitivity == "normal"
    }
    covered_fact_ids = {
        fact_id
        for plan in plans
        for fact_id in plan.fact_ids
        if fact_id in normal_fact_ids
    }
    coverage = len(covered_fact_ids) / len(normal_fact_ids)
    print(f"유형별 건수: {dict(Counter(plan.doc_type for plan in plans))}")
    print(f"권한별 건수: {dict(Counter(plan.access.level for plan in plans))}")
    print(f"팀별 건수: {dict(Counter(plan.team for plan in plans))}")
    print(f"카나리아 수: {canaries['count']}")
    print(f"인젝션 수: {injections['count']}")
    print(
        f"사실 커버리지: {len(covered_fact_ids)}/{len(normal_fact_ids)} "
        f"({coverage:.1%})"
    )
    return 0


def _document_seed(seed: Seed, doc_id: str) -> int:
    digest = hashlib.sha256(
        f"{seed.company['random_seed']}:{doc_id}".encode()
    ).hexdigest()
    return int(digest[:8], 16)


def _normal_access(index: int, team: str) -> Access:
    public_before = (
        index
        * ACCESS_COUNTS["public"]
        // (ACCESS_COUNTS["public"] + ACCESS_COUNTS["team"])
    )
    public_after = (
        (index + 1)
        * ACCESS_COUNTS["public"]
        // (ACCESS_COUNTS["public"] + ACCESS_COUNTS["team"])
    )
    if public_after > public_before:
        return Access("public", (), 1)
    return Access("team", (team,), 1)


def _entity_team(entity: Mapping[str, object], entity_id: str) -> str:
    team = str(entity.get("owner_team", ""))
    if team not in TEAMS:
        raise SeedError(f"document topic has invalid owner team: {entity_id}={team}")
    return team


def _fact_teams(seed: Seed) -> dict[str, str]:
    subject_teams: dict[str, str] = {}
    for entity_id, entity in seed.entities.items():
        owner_team = entity.get("owner_team")
        if isinstance(owner_team, str) and owner_team in TEAMS:
            subject_teams[entity_id] = owner_team
    for service in seed.services:
        subject_teams[service.name] = service.owner_team
    result: dict[str, str] = {}
    for fact in seed.facts.values():
        team = subject_teams.get(fact.subject)
        if team is None:
            raise SeedError(
                f"document plan cannot determine fact owner team: {fact.fact_id}"
            )
        result[fact.fact_id] = team
    return result


def _validate_initial_counts(
    drafts: Sequence[_Draft], normal_count: int, restricted_count: int
) -> None:
    if normal_count != ACCESS_COUNTS["public"] + ACCESS_COUNTS["team"]:
        raise SeedError(f"document plan normal access count mismatch: {normal_count}")
    if restricted_count != ACCESS_COUNTS["restricted"]:
        raise SeedError(f"document plan restricted count mismatch: {restricted_count}")
    type_counts = Counter(draft.doc_type for draft in drafts)
    access_counts = Counter(draft.access.level for draft in drafts)
    if type_counts != TYPE_COUNTS:
        raise SeedError(f"document plan type counts mismatch: {dict(type_counts)}")
    if access_counts != ACCESS_COUNTS:
        raise SeedError(f"document plan access counts mismatch: {dict(access_counts)}")
    team_normal_counts = Counter(
        draft.team for draft in drafts if draft.access.level != "restricted"
    )
    sparse = [team for team in TEAMS if team_normal_counts[team] < 10]
    if sparse:
        raise SeedError(
            f"document plan team has fewer than 10 public/team docs: {sparse[0]}"
        )


def _link_incidents(
    drafts: Sequence[_Draft],
    seed: Seed,
    incidents: Sequence[tuple[str, Mapping[str, object]]],
) -> None:
    candidates: dict[tuple[str, str], list[_Draft]] = defaultdict(list)
    for draft in drafts:
        if (
            draft.doc_type in {"meeting", "weekly"}
            and draft.access.level != "restricted"
        ):
            candidates[(draft.team, draft.doc_type)].append(draft)
    team_offsets: Counter[tuple[str, str]] = Counter()
    for incident_id, entity in incidents:
        team = _entity_team(entity, incident_id)
        shared_fact = next(
            (
                fact.fact_id
                for fact in seed.facts.values()
                if fact.subject == incident_id and fact.fact_id.endswith("-cause")
            ),
            None,
        )
        if shared_fact is None:
            raise SeedError(f"incident has no cause fact: {incident_id}")
        for doc_type in ("meeting", "weekly"):
            options = candidates[(team, doc_type)]
            if not options:
                raise SeedError(f"no {doc_type} document for incident team: {team}")
            offset_key = (team, doc_type)
            target = options[team_offsets[offset_key] % len(options)]
            team_offsets[offset_key] += 1
            target.fact_ids.add(shared_fact)


def _cover_normal_facts(
    drafts: Sequence[_Draft], seed: Seed, fact_teams: Mapping[str, str]
) -> None:
    already_covered = {
        fact_id
        for draft in drafts
        for fact_id in draft.fact_ids
        if fact_id in seed.facts
    }
    candidates_by_team: dict[str, list[_Draft]] = defaultdict(list)
    for draft in drafts:
        if draft.access.level != "restricted" and draft.doc_type != "incident":
            candidates_by_team[draft.team].append(draft)

    for team in TEAMS:
        candidates = candidates_by_team[team]
        if not candidates:
            raise SeedError(f"no normal document fact slots for team: {team}")
        fact_ids = sorted(
            fact.fact_id
            for fact in seed.facts.values()
            if fact.sensitivity == "normal"
            and fact_teams[fact.fact_id] == team
            and fact.fact_id not in already_covered
        )
        cursor = 0
        for fact_id in fact_ids:
            chosen: int | None = None
            for offset in range(len(candidates)):
                index = (cursor + offset) % len(candidates)
                draft = candidates[index]
                if len(draft.fact_ids) < _FACT_COUNT_RANGES[draft.doc_type][1]:
                    draft.fact_ids.add(fact_id)
                    chosen = index
                    break
            if chosen is None:
                raise SeedError(f"insufficient normal fact slots for team: {team}")
            cursor = (chosen + 1) % len(candidates)


def _fill_fact_targets(
    drafts: Sequence[_Draft], seed: Seed, fact_teams: Mapping[str, str]
) -> None:
    normal_by_team = {
        team: sorted(
            fact.fact_id
            for fact in seed.facts.values()
            if fact.sensitivity == "normal" and fact_teams[fact.fact_id] == team
        )
        for team in TEAMS
    }
    for draft in drafts:
        minimum, maximum = _FACT_COUNT_RANGES[draft.doc_type]
        if len(draft.fact_ids) > maximum:
            raise SeedError(
                f"too many assigned facts for {draft.doc_id}: {len(draft.fact_ids)}"
            )
        if draft.doc_type == "incident":
            if len(draft.fact_ids) < minimum:
                raise SeedError(
                    f"incident has too few facts for {draft.doc_id}: {len(draft.fact_ids)}"
                )
            continue
        rng = random.Random(draft.seed)
        target = max(len(draft.fact_ids), rng.randint(minimum, maximum))
        candidates = normal_by_team[draft.team]
        if candidates:
            offset = draft.seed % len(candidates)
            candidates = candidates[offset:] + candidates[:offset]
        for fact_id in candidates:
            if len(draft.fact_ids) >= target:
                break
            draft.fact_ids.add(fact_id)
        if len(draft.fact_ids) < target:
            raise SeedError(
                f"insufficient facts for {draft.doc_id}: need {target}, "
                f"found {len(draft.fact_ids)}"
            )


def _assign_regulation_refs(drafts: Sequence[_Draft], seed: Seed) -> None:
    refs_by_team: dict[str, list[str]] = defaultdict(list)
    for row in seed.regulation_refs:
        try:
            reference = f"{row['name']} {row['article']}"
            teams = row["teams"]
        except KeyError as exc:
            raise SeedError(
                f"invalid regulation reference: missing {exc.args[0]}"
            ) from exc
        if not isinstance(teams, Sequence) or isinstance(teams, str):
            raise SeedError(f"invalid regulation reference teams: {reference}")
        for team in teams:
            if str(team) in TEAMS:
                refs_by_team[str(team)].append(reference)
    assigned = 0
    for draft in drafts:
        if assigned >= 30:
            break
        if draft.doc_type not in {"guide", "notice", "meeting"}:
            continue
        options = sorted(set(refs_by_team[draft.team]))
        if not options:
            continue
        rng = random.Random(draft.seed)
        count = min(len(options), 1 + rng.randrange(2))
        draft.regulation_refs = tuple(sorted(rng.sample(options, count)))
        assigned += 1
    if assigned < 30:
        raise SeedError(f"insufficient regulation reference documents: {assigned}/30")


def _assign_canaries(drafts: Sequence[_Draft]) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    used: set[str] = set()
    restricted = [draft for draft in drafts if draft.access.level == "restricted"]
    for number, draft in enumerate(restricted, start=1):
        fact_id = f"f-canary-{number:03d}"
        canary_id = f"cn-{number:03d}"
        value = _canary_value(draft.seed, used)
        used.add(value)
        draft.canary_fact_id = fact_id
        draft.fact_ids.add(fact_id)
        rows.append(
            {
                "canary_id": canary_id,
                "fact_id": fact_id,
                "value": value,
                "doc_id": draft.doc_id,
                "owner_team": draft.team,
                "min_grade": 4,
            }
        )
    return {"canaries": rows, "count": len(rows)}


def _canary_value(doc_seed: int, used: set[str]) -> str:
    rng = random.Random(doc_seed)
    adjective = rng.choice(_CANARY_ADJECTIVES)
    animal = rng.choice(_CANARY_ANIMALS)
    number = rng.randint(1000, 9999)
    for _ in range(9000):
        candidate = f"{adjective}{animal}-{number:04d}"
        if all(candidate not in value and value not in candidate for value in used):
            return candidate
        number = 1000 + ((number - 999) % 9000)
    raise SeedError("cannot create a unique canary value")


def _assign_injections(drafts: Sequence[_Draft]) -> dict[str, object]:
    selected: list[_Draft] = []
    for doc_type in DOC_TYPES:
        for access_level in ("public", "team"):
            candidate = next(
                (
                    draft
                    for draft in drafts
                    if draft.doc_type == doc_type
                    and draft.access.level == access_level
                    and draft.injection_id is None
                ),
                None,
            )
            if candidate is None:
                raise SeedError(
                    f"no {access_level} injection slot for document type: {doc_type}"
                )
            selected.append(candidate)

    variants = tuple(INJECTION_TEXTS)
    rows: list[dict[str, object]] = []
    for number, draft in enumerate(selected, start=1):
        injection_id = f"inj-{number:02d}"
        variant = variants[(number - 1) % len(variants)]
        draft.injection_id = injection_id
        rows.append(
            {
                "injection_id": injection_id,
                "doc_id": draft.doc_id,
                "variant": variant,
                "text": INJECTION_TEXTS[variant],
                "access_level": draft.access.level,
                "insert_rule": "before_last_h2",
            }
        )
    return {"injections": rows, "count": len(rows)}


def _title_hint(draft: _Draft, document_date: date, seed: Seed) -> str:
    team_name = TEAMS[draft.team].name_ko + "팀"
    topic = "업무 현황"
    if draft.topic_subject is not None:
        entity = seed.entities.get(draft.topic_subject)
        if entity is not None:
            topic = str(entity.get("title", draft.topic_subject))
        else:
            topic = draft.topic_subject
    elif draft.fact_ids:
        first_seed_fact = next(
            (
                seed.facts[fact_id]
                for fact_id in sorted(draft.fact_ids)
                if fact_id in seed.facts
            ),
            None,
        )
        if first_seed_fact is not None:
            entity = seed.entities.get(first_seed_fact.subject)
            topic = (
                str(entity.get("title", first_seed_fact.subject))
                if entity is not None
                else first_seed_fact.subject
            )
    if draft.doc_type == "meeting":
        return f"[회의록] {team_name} {document_date.isoformat()} {topic}"
    if draft.doc_type == "weekly":
        week = (document_date.day - 1) // 7 + 1
        return (
            f"[주간보고] {team_name} {document_date.year}년 "
            f"{document_date.month}월 {week}주차"
        )
    if draft.doc_type == "incident":
        return f"[장애보고] {draft.topic_subject} {topic}"
    if draft.doc_type == "runbook":
        return f"[런북] {team_name} {topic} 운영 절차"
    if draft.doc_type == "guide":
        return f"[가이드] {team_name} {topic} 안내"
    return f"[공지] {team_name} {topic}"


def _plan_record(plan: DocPlan) -> dict[str, object]:
    return {
        "doc_id": plan.doc_id,
        "doc_type": plan.doc_type,
        "team": plan.team,
        "access": {
            "level": plan.access.level,
            "teams": list(plan.access.teams),
            "min_grade": plan.access.min_grade,
        },
        "author_id": plan.author_id,
        "date": plan.date,
        "title_hint": plan.title_hint,
        "fact_ids": list(plan.fact_ids),
        "regulation_refs": list(plan.regulation_refs),
        "canary_fact_id": plan.canary_fact_id,
        "injection_id": plan.injection_id,
        "style": plan.style,
        "seed": plan.seed,
        "related_doc_ids": list(plan.related_doc_ids),
    }


def _atomic_write_text(path: Path, text: str) -> None:
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
            handle.write(text)
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
