"""Build deterministic question specifications from the synthetic corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import combinations
from pathlib import Path

from kodocs.eval.golden import (
    GOLD_QUOTA,
    QUESTION_TYPES,
    SILVER_QUOTA,
    Asker,
    Qrel,
    Question,
    SqlAnswer,
    can_read,
    load_questions,
    span_sha256,
    validate_question,
    write_questions,
)
from kodocs.ingest.manifest import sha256_file
from kodocs.synth.db import build_rows
from kodocs.synth.seed import TEAMS, Fact, Person, Seed, load_seed, surface_forms

SPECS_PATH = Path("data/synthetic/queries/specs.jsonl")
SUMMARY_PATH = Path("data/manifest/golden_specs.json")
DOCS_PATH = Path("data/synthetic/docs")
DB_MANIFEST_PATH = Path("data/manifest/synth_db.json")
VALIDATED_PATH = Path("data/synthetic/queries/validated.jsonl")
OVERRIDES_PATH = Path("data/synthetic/queries/qrels_overrides.jsonl")

_NO_ANSWER_ATTRIBUTES = (
    ("cost", "비용", ("비용", "소요 금액"), frozenset({"amount", "cost", "budget"})),
    (
        "affected_users",
        "영향 사용자 수",
        ("영향 사용자 수", "사용자 수"),
        frozenset({"affected", "users", "user-count", "impact-count"}),
    ),
)

_SQL_QUERIES = (
    (
        "팀별 티켓 수",
        (
            "SELECT owner_team, COUNT(*) AS count FROM tickets "
            "GROUP BY owner_team ORDER BY owner_team"
        ),
    ),
    (
        "상태별 티켓 수",
        (
            "SELECT status, COUNT(*) AS count FROM tickets "
            "GROUP BY status ORDER BY status"
        ),
    ),
    (
        "월별 티켓 수",
        (
            "SELECT substr(created_at, 1, 7) AS month, COUNT(*) AS count "
            "FROM tickets GROUP BY month ORDER BY month"
        ),
    ),
    (
        "팀별 결재 건수",
        (
            "SELECT owner_team, COUNT(*) AS count FROM approvals "
            "GROUP BY owner_team ORDER BY owner_team"
        ),
    ),
    (
        "상태별 결재 건수",
        (
            "SELECT status, COUNT(*) AS count FROM approvals "
            "GROUP BY status ORDER BY status"
        ),
    ),
    (
        "팀별 결재 금액 합계",
        (
            "SELECT owner_team, SUM(amount_krw) AS amount_krw FROM approvals "
            "GROUP BY owner_team ORDER BY owner_team"
        ),
    ),
)


class SpecBuildError(ValueError):
    """A deterministic specification cannot satisfy its declared contract."""


@dataclass(frozen=True)
class _Candidate:
    type: str
    anchor_values: tuple[str, ...]
    answer_values: tuple[str, ...]
    answer_fact_ids: tuple[str, ...]
    qrels: tuple[Qrel, ...]
    reference_answer: str | None
    expected_response: str = "answer"
    tags: tuple[str, ...] = ()
    as_of: str | None = None
    sql: SqlAnswer | None = None
    unmarked_candidates: tuple[str, ...] = ()
    no_answer_checks: Mapping[str, bool | None] | None = None
    temporal_mode: str | None = None
    temporal_candidate_count: int = 0
    document_fact_ids: Mapping[str, frozenset[str]] | None = None
    discriminator: str = ""
    # Diversity keys: the incidents/events (or other anchors) the answer draws
    # on, and the service they belong to. Filled by _annotate_subjects.
    subjects: tuple[str, ...] = ()
    service: str = ""

    @property
    def key(self) -> tuple[object, ...]:
        return (
            self.type,
            self.anchor_values,
            self.answer_fact_ids,
            self.as_of or "",
            self.temporal_mode or "",
            self.discriminator,
        )


@dataclass(frozen=True)
class _BuildResult:
    questions: list[Question]
    candidates: dict[str, dict[str, int]]
    rejected: Counter[str]
    diversity: dict[str, object]


@dataclass(frozen=True)
class SyncResult:
    questions: list[Question]
    qrels_changed: tuple[str, ...]
    replaced: dict[str, tuple[str, ...]]
    build: _BuildResult
    dropped: tuple[str, ...] = ()
    rewritten: tuple[str, ...] = ()


# Per-subject caps: (within one type, across all types of one set).
DIVERSITY_CAPS = {"gold": (1, 2), "silver": (2, 4)}


def fact_index(
    docs: Mapping[str, Mapping[str, object]],
) -> dict[str, list[tuple[str, int, int]]]:
    """Index every marked fact span in deterministic document/span order."""
    index: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    for doc_id, document in sorted(docs.items()):
        body = document.get("body")
        spans = document.get("fact_spans")
        if (
            not isinstance(body, str)
            or not isinstance(spans, Sequence)
            or isinstance(spans, (str, bytes))
        ):
            raise SpecBuildError(f"invalid synthetic document: {doc_id}")
        for raw_span in spans:
            if not isinstance(raw_span, Mapping):
                raise SpecBuildError(f"invalid fact span: {doc_id}")
            fact_id = raw_span.get("fact_id")
            start = raw_span.get("start")
            end = raw_span.get("end")
            if (
                not isinstance(fact_id, str)
                or not isinstance(start, int)
                or isinstance(start, bool)
                or not isinstance(end, int)
                or isinstance(end, bool)
                or not 0 <= start < end <= len(body)
            ):
                raise SpecBuildError(f"invalid fact span: {doc_id}/{fact_id}")
            index[fact_id].append((doc_id, start, end))
    return {fact_id: sorted(spans) for fact_id, spans in sorted(index.items())}


def unmarked_mentions(
    fact: Fact,
    docs: Mapping[str, Mapping[str, object]],
    people: Mapping[str, Person] | Sequence[Person],
    marked: set[str],
) -> list[str]:
    """Return docs containing an atomic fact surface form without its marker."""
    if fact.kind == "text":
        return []
    people_by_id = (
        dict(people)
        if isinstance(people, Mapping)
        else {person.person_id: person for person in people}
    )
    forms = tuple(form for form in surface_forms(fact, people=people_by_id) if form)
    if not forms:
        return []
    return [
        doc_id
        for doc_id, document in sorted(docs.items())
        if doc_id not in marked
        and isinstance(document.get("body"), str)
        and any(form in str(document["body"]) for form in forms)
    ]


def build_qrels(
    fact_ids: Sequence[str],
    *,
    seed: Seed,
    docs: Mapping[str, Mapping[str, object]],
    index: Mapping[str, Sequence[tuple[str, int, int]]],
) -> tuple[Qrel, ...]:
    """Build span qrels: all answer spans are rel=2; sibling-only docs are rel=1."""
    targets = tuple(dict.fromkeys(fact_ids))
    unknown = [fact_id for fact_id in targets if fact_id not in seed.facts]
    if unknown:
        raise SpecBuildError(f"unknown answer fact: {unknown[0]}")
    answer_docs = {
        doc_id for fact_id in targets for doc_id, _, _ in index.get(fact_id, ())
    }
    qrels: list[Qrel] = []
    for fact_id in targets:
        for doc_id, start, end in index.get(fact_id, ()):
            qrels.append(_span_qrel(doc_id, 2, fact_id, start, end, docs))

    subjects = {seed.facts[fact_id].subject for fact_id in targets}
    for fact in sorted(seed.facts.values(), key=lambda item: item.fact_id):
        if fact.fact_id in targets or fact.subject not in subjects:
            continue
        for doc_id, start, end in index.get(fact.fact_id, ()):
            if doc_id not in answer_docs:
                qrels.append(_span_qrel(doc_id, 1, fact.fact_id, start, end, docs))
    return tuple(
        sorted(
            qrels,
            key=lambda item: (
                item.doc_id,
                -item.rel,
                item.fact_id or "",
                item.start if item.start is not None else -1,
                item.end if item.end is not None else -1,
            ),
        )
    )


def build_specs(
    seed: Seed,
    docs: Mapping[str, Mapping[str, object]],
    *,
    db_rows: Mapping[str, Sequence[Mapping[str, object]]],
    db_content_hash: str,
    random_seed: int,
) -> list[Question]:
    """Build the exact gold-synthetic and silver allocations."""
    return _build_specs_result(
        seed,
        docs,
        db_rows=db_rows,
        db_content_hash=db_content_hash,
        random_seed=random_seed,
    ).questions


def sync_specs(
    existing: Sequence[Question],
    seed: Seed,
    docs: Mapping[str, Mapping[str, object]],
    *,
    db_rows: Mapping[str, Sequence[Mapping[str, object]]],
    db_content_hash: str,
    random_seed: int,
    fill: bool = False,
    overrides: Mapping[str, Override] | None = None,
) -> SyncResult:
    """Keep every existing selection; re-derive evidence for the current corpus.

    Question-defining fields (type, tags, asker, as_of, anchor/answer values,
    expected response, sql) are never touched. qrels, unmarked candidates and
    no-answer checks are recomputed, every condition is re-checked, and only
    questions that break one are replaced (same id, stable per-candidate order).
    """
    ids = [question.id for question in existing]
    if len(set(ids)) != len(ids):
        raise SpecBuildError("duplicate question id in existing specs")
    index = fact_index(docs)
    eligible, rejected = _eligible_pools(
        seed, docs, db_rows=db_rows, db_content_hash=db_content_hash
    )
    people = tuple(sorted(seed.people, key=lambda item: item.person_id))
    context = _sync_context(
        seed,
        docs,
        index=index,
        db_rows=db_rows,
        db_content_hash=db_content_hash,
    )

    unclaimed: dict[tuple[object, ...], list[_Candidate]] = defaultdict(list)
    for candidates in eligible.values():
        for candidate in candidates:
            unclaimed[_candidate_loose_key(candidate)].append(candidate)

    by_id = {question.id: question for question in existing}
    order = sorted(existing, key=lambda item: (item.set != "gold", item.id))
    recomputed: dict[str, Question] = {}
    resolved: dict[str, _Candidate] = {}
    reasons: dict[str, list[str]] = {}
    rewritten: list[str] = []
    for index_, question in enumerate(order):
        migrated = _migrate_result_anchor(question, context)
        if migrated is not question:
            rewritten.append(question.id)
            question = order[index_] = migrated
        updated, found = _resync_question(question, context)
        recomputed[question.id] = updated
        claim = unclaimed.get(_question_loose_key(question))
        if claim:
            resolved[question.id] = claim.pop(0)
        elif not found:
            found = ["no_longer_valid_candidate"]
        if found:
            reasons[question.id] = found

    dropped = _over_quota(existing, reasons)
    for qid in dropped:
        by_id.pop(qid)
        recomputed.pop(qid)
        resolved.pop(qid, None)
        reasons.pop(qid, None)
    order = [question for question in order if question.id in by_id]
    fills = _under_quota(list(by_id.values())) if fill else []
    for placeholder in fills:
        by_id[placeholder.id] = placeholder
        reasons[placeholder.id] = ["filled_to_quota"]

    used_keys = {candidate.key for candidate in resolved.values()}
    type_counts: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    set_counts: dict[str, Counter[str]] = defaultdict(Counter)

    def register(question: Question, candidate: _Candidate) -> None:
        type_counts[(question.set, question.type)].update(candidate.subjects)
        set_counts[question.set].update(candidate.subjects)

    for question in order:
        if question.id not in reasons:
            register(question, resolved[question.id])

    replacements: dict[str, tuple[Question, _Candidate]] = {}

    def replace_all(kind: str) -> None:
        pending = sorted(qid for qid in reasons if by_id[qid].set == kind)
        for qid in pending:
            if qid in replacements:
                continue
            original = by_id[qid]
            candidate = _pick_replacement(
                eligible.get(original.type, ()),
                set_name=original.set,
                question_type=original.type,
                random_seed=random_seed,
                used_keys=used_keys,
                gold_answer_facts=frozenset(gold_facts()),
                type_counts=type_counts[(original.set, original.type)],
                set_counts=set_counts[original.set],
                avoid_facts=frozenset(silver_facts())
                if kind == "gold"
                else frozenset(),
            )
            used_keys.add(candidate.key)
            asker = _replacement_asker(
                candidate, people, docs, random_seed=random_seed, qid=qid
            )
            candidate = _bind_sql(candidate, asker, db_rows, db_content_hash)
            fresh = _question_from_candidate(
                candidate, qid=qid, set_name=original.set, asker=asker
            )
            replacements[qid] = (fresh, candidate)
            type_counts[(original.set, original.type)].update(candidate.subjects)
            set_counts[original.set].update(candidate.subjects)

    def survivor_or_replacement(qid: str) -> _Candidate | None:
        if qid in replacements:
            return replacements[qid][1]
        return None if qid in reasons else resolved[qid]

    def silver_facts() -> set[str]:
        return {
            fact_id
            for qid, question in by_id.items()
            if question.set == "silver" and qid not in reasons
            for fact_id in resolved[qid].answer_fact_ids
        }

    def gold_facts() -> set[str]:
        facts: set[str] = set()
        for qid, question in by_id.items():
            if question.set != "gold":
                continue
            candidate = survivor_or_replacement(qid)
            if candidate is not None:
                facts.update(candidate.answer_fact_ids)
        return facts

    replace_all("gold")
    shared = gold_facts()
    for question in order:
        if question.set != "silver" or question.id in reasons:
            continue
        if shared.intersection(resolved[question.id].answer_fact_ids):
            reasons[question.id] = ["silver_shares_gold_fact"]
            for counter in (
                type_counts[(question.set, question.type)],
                set_counts[question.set],
            ):
                counter.subtract(resolved[question.id].subjects)
    replace_all("silver")

    final: dict[str, tuple[Question, _Candidate]] = {}
    for qid in [*(question.id for question in existing), *(q.id for q in fills)]:
        if qid not in by_id:
            continue
        if qid in replacements:
            final[qid] = replacements[qid]
        else:
            final[qid] = (recomputed[qid], resolved[qid])

    overrides = overrides or {}
    missing = sorted(set(overrides) - set(final))
    if missing:
        raise SpecBuildError(f"override target not in specs: {', '.join(missing)}")
    for qid, extra in sorted(overrides.items()):
        if qid in replacements:
            raise SpecBuildError(f"override target was replaced: {qid}")
        question, candidate = final[qid]
        if (
            extra.anchor_values is not None
            and extra.anchor_values != question.anchor_values
        ):
            raise SpecBuildError(
                f"override anchor mismatch for {qid}: {list(extra.anchor_values)}"
            )
        final[qid] = (_apply_override(question, extra.add, docs), candidate)

    validation_errors: list[str] = []
    for question, _ in final.values():
        errors = validate_question(
            replace(question, question="명세 단계 빈 질문 허용"),
            docs=docs,
            regulation_index={},
            table_ids=set(),
        )
        if errors:
            validation_errors.append(f"{question.id}: {','.join(errors)}")
    if validation_errors:
        raise SpecBuildError("validation errors: " + "; ".join(validation_errors))

    _check_sql_depends_on_asker([question for question, _ in final.values()], db_rows)
    gold_by_type: dict[str, list[_Candidate]] = {}
    silver_by_type: dict[str, list[_Candidate]] = {}
    for question, candidate in final.values():
        target = gold_by_type if question.set == "gold" else silver_by_type
        target.setdefault(question.type, []).append(candidate)
    gold_types = [t for t in QUESTION_TYPES if GOLD_QUOTA.get((t, "synthetic"), 0)]
    gold_by_type = {t: gold_by_type.get(t, []) for t in gold_types}
    silver_by_type = {t: silver_by_type.get(t, []) for t in SILVER_QUOTA}
    selected_gold = [c for t in gold_by_type for c in gold_by_type[t]]
    gold_keys = {candidate.key for candidate in selected_gold}
    gold_answer_facts = {f for c in selected_gold for f in c.answer_fact_ids}
    candidate_counts: dict[str, dict[str, int]] = {
        "gold": {t: len(eligible.get(t, [])) for t in gold_types},
        "silver": {
            t: sum(
                1
                for c in eligible[t]
                if c.key not in gold_keys
                and gold_answer_facts.isdisjoint(c.answer_fact_ids)
            )
            for t in SILVER_QUOTA
        },
    }
    build = _BuildResult(
        [question for question, _ in final.values()],
        candidate_counts,
        rejected,
        _diversity_summary(gold_by_type, silver_by_type),
    )
    qrels_changed = tuple(
        sorted(
            qid
            for qid, question in by_id.items()
            if qid not in reasons and final[qid][0].qrels != question.qrels
        )
    )
    return SyncResult(
        questions=build.questions,
        qrels_changed=qrels_changed,
        replaced={qid: tuple(reasons[qid]) for qid in sorted(reasons)},
        build=build,
        dropped=dropped,
        rewritten=tuple(sorted(q for q in rewritten if q not in reasons)),
    )


def _quotas() -> dict[str, dict[str, int]]:
    return {
        "gold": {t: GOLD_QUOTA.get((t, "synthetic"), 0) for t in QUESTION_TYPES},
        "silver": dict(SILVER_QUOTA),
    }


def _under_quota(questions: Sequence[Question]) -> list[Question]:
    """Placeholders for a raised quota, numbered into the lowest free ids."""
    counts = Counter((question.set, question.type) for question in questions)
    used: dict[str, set[int]] = defaultdict(set)
    for question in questions:
        used[question.set].add(int(question.id.rsplit("-", 1)[-1]))
    placeholders: list[Question] = []
    for set_name, quotas in _quotas().items():
        prefix = "g-syn" if set_name == "gold" else "s-syn"
        number = 0
        for question_type, quota in quotas.items():
            for _ in range(quota - counts[(set_name, question_type)]):
                number += 1
                while number in used[set_name]:
                    number += 1
                used[set_name].add(number)
                placeholders.append(
                    Question(
                        id=f"{prefix}-{number:03d}",
                        set=set_name,
                        source="synthetic",
                        type=question_type,
                        tags=(),
                        question="",
                        asker=Asker(team="", grade=0),
                        as_of=None,
                        qrels=(),
                        anchor_values=(),
                        answer_values=(),
                        expected_response="answer",
                        sql=None,
                        reference_answer=None,
                        reference_sha256=None,
                        summary80=None,
                        split=None,
                    )
                )
    return placeholders


@dataclass(frozen=True)
class Override:
    add: tuple[Qrel, ...]
    anchor_values: tuple[str, ...] | None = None


def load_overrides(path: Path) -> dict[str, Override]:
    """Read reviewed document-level qrel additions keyed by question id."""
    overrides: dict[str, Override] = {}
    if not Path(path).exists():
        return overrides
    for line_number, line in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        row = json.loads(line)
        qid = row.get("id")
        if not isinstance(qid, str) or not qid:
            raise ValueError(f"override line {line_number}: id missing")
        if qid.startswith("chk-"):
            raise ValueError(f"override line {line_number}: chk- ids are not allowed")
        if qid in overrides:
            raise ValueError(f"override line {line_number}: duplicate id {qid}")
        if not isinstance(row.get("reason"), str) or not row["reason"]:
            raise ValueError(f"override line {line_number}: reason missing")
        added: list[Qrel] = []
        for item in row.get("add") or ():
            doc_id, rel = item.get("doc_id"), item.get("rel")
            if not isinstance(doc_id, str) or rel not in (1, 2):
                raise ValueError(
                    f"override line {line_number}: bad doc_id or rel {rel}"
                )
            added.append(Qrel(doc_id=doc_id, rel=rel))
        if not added:
            raise ValueError(f"override line {line_number}: add is empty")
        anchors = row.get("anchor_values")
        overrides[qid] = Override(
            add=tuple(added),
            anchor_values=None if anchors is None else tuple(anchors),
        )
    return overrides


def _apply_override(
    question: Question,
    extra: Sequence[Qrel],
    docs: Mapping[str, Mapping[str, object]],
) -> Question:
    present = {qrel.doc_id for qrel in question.qrels}
    qrels = list(question.qrels)
    for qrel in extra:
        if qrel.doc_id not in docs:
            raise SpecBuildError(f"override doc not found: {qrel.doc_id}")
        if qrel.doc_id not in present:
            qrels.append(qrel)
            present.add(qrel.doc_id)
    return replace(
        question,
        qrels=tuple(
            sorted(
                qrels,
                key=lambda item: (
                    item.doc_id,
                    -item.rel,
                    item.fact_id or "",
                    item.start if item.start is not None else -1,
                    item.end if item.end is not None else -1,
                ),
            )
        ),
    )


def _over_quota(
    existing: Sequence[Question], reasons: Mapping[str, object]
) -> tuple[str, ...]:
    """Drop questions above a lowered quota: violating ones first, highest id first."""
    quotas = _quotas()
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for question in existing:
        groups[(question.set, question.type)].append(question.id)
    dropped: list[str] = []
    for (set_name, question_type), qids in sorted(groups.items()):
        excess = len(qids) - quotas.get(set_name, {}).get(question_type, 0)
        if excess > 0:
            ranked = sorted(qids, key=lambda qid: (qid in reasons, qid), reverse=True)
            dropped.extend(ranked[:excess])
    return tuple(sorted(dropped))


@dataclass(frozen=True)
class _SyncContext:
    seed: Seed
    docs: Mapping[str, Mapping[str, object]]
    index: Mapping[str, Sequence[tuple[str, int, int]]]
    document_fact_ids: Mapping[str, frozenset[str]]
    people_by_id: Mapping[str, Person]
    facts_by_subject: Mapping[str, tuple[Fact, ...]]
    bodies: tuple[str, ...] = ()
    norm_bodies: tuple[str, ...] = ()
    db_rows: Mapping[str, Sequence[Mapping[str, object]]] | None = None
    db_content_hash: str = ""


def _sync_context(
    seed: Seed,
    docs: Mapping[str, Mapping[str, object]],
    *,
    index: Mapping[str, Sequence[tuple[str, int, int]]] | None = None,
    db_rows: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
    db_content_hash: str = "",
) -> _SyncContext:
    return _SyncContext(
        seed=seed,
        docs=docs,
        index=fact_index(docs) if index is None else index,
        document_fact_ids=_document_fact_ids(docs),
        people_by_id={person.person_id: person for person in seed.people},
        facts_by_subject=_facts_by_subject(seed),
        bodies=tuple(
            body
            for document in docs.values()
            if isinstance((body := document.get("body")), str)
        ),
        norm_bodies=tuple(
            _norm(body)
            for document in docs.values()
            if isinstance((body := document.get("body")), str)
        ),
        db_rows=db_rows,
        db_content_hash=db_content_hash,
    )


def _visible(identifier: str, context: _SyncContext) -> bool:
    return any(identifier in body for body in context.bodies)


_FEATURE_RE = re.compile(r"^(.+?)[을를]\s*배포했다\.?$")


def _feature_name(seed: Seed, fact: Fact) -> str | None:
    """The deployed feature of a result fact's event, as a short noun phrase."""
    if _attribute(fact) != "result":
        return None
    versions = sorted(
        str(other.value)
        for other in seed.facts.values()
        if other.subject == fact.subject and _attribute(other) == "version"
    )
    if len(versions) != 1:
        return None
    match = _FEATURE_RE.match(versions[0].strip())
    return match.group(1).strip() if match else versions[0].strip().rstrip(".")


def _semantic_anchor(seed: Seed, service: str, fact: Fact) -> tuple[str, ...]:
    feature = _feature_name(seed, fact)
    return (service, feature) if feature else (service,)


def _migrate_result_anchor(question: Question, context: _SyncContext) -> Question:
    """Add the deployed feature to a service-anchored result question."""
    fact_ids = _answer_fact_ids(question)
    if question.type != "semantic" or len(fact_ids) != 1:
        return question
    fact = context.seed.facts.get(fact_ids[0])
    if fact is None or len(question.anchor_values) != 1:
        return question
    service = question.anchor_values[0]
    if service not in {s.name for s in context.seed.services}:
        return question
    anchors = _semantic_anchor(context.seed, service, fact)
    if anchors == question.anchor_values:
        return question
    return replace(question, anchor_values=anchors)


_COMPARED_ATTRIBUTES = frozenset({"start", "restored"})
_SLO_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:%|분|시간|일|건)")


def _norm(text: str) -> str:
    return re.sub(r"[\s.]+", "", unicodedata.normalize("NFC", text))


def _strict_forms(fact: Fact, context: _SyncContext) -> set[str]:
    """Every accepted spelling of a final answer, normalized for substring checks."""
    forms = {_norm(form) for form in _answer_forms(fact, context.people_by_id)}
    attribute = _attribute(fact)
    service = next((x for x in context.seed.services if x.name == fact.subject), None)
    if service is not None and attribute == "owner":
        team = TEAMS.get(service.owner_team)
        if team is not None:
            forms.update({_norm(team.name_ko), _norm(team.name_ko + "팀")})
    if attribute == "slo":
        forms.update(_norm(m) for m in _SLO_NUMBER_RE.findall(str(fact.value)))
    return {form for form in forms if form}


def _multi_doc_form(
    anchor_values: tuple[str, ...], facts: Sequence[Fact]
) -> tuple[str | None, list[Fact]]:
    """Return (form, final-answer facts) for same_subject, linked or comparison."""
    if len(anchor_values) == 1:
        anchor = anchor_values[0]
        if len(facts) >= 2 and {fact.subject for fact in facts} == {anchor}:
            return "same_subject", list(facts)
        if len(facts) == 2:
            for bridge, target in (facts, facts[::-1]):
                if (
                    bridge.subject == anchor
                    and bridge.kind == "service"
                    and _attribute(bridge) == "service"
                    and target.subject == str(bridge.value)
                ):
                    return "linked", [target]
    if (
        len(anchor_values) == 2
        and len(facts) == 2
        and {fact.subject for fact in facts} == set(anchor_values)
        and len(set(anchor_values)) == 2
        and _attribute(facts[0]) == _attribute(facts[1])
        and all(fact.kind != "id" and _attribute(fact) != "service" for fact in facts)
    ):
        return "comparison", list(facts)
    return None, []


def _multi_doc_reasons(
    anchor_values: tuple[str, ...], fact_ids: Sequence[str], context: _SyncContext
) -> list[str]:
    """Allow same-subject, linked (incident -> service -> fact) or comparison pairs.

    Strict check: no document may hold every anchor together with every final
    answer (any accepted spelling), so one document can never answer alone.
    """
    seed = context.seed
    reasons: list[str] = []
    facts = [seed.facts[fact_id] for fact_id in fact_ids if fact_id in seed.facts]
    form, finals = _multi_doc_form(anchor_values, facts)
    if form is None:
        reasons.append("multi_doc_not_linked_form")
    if form == "linked":
        anchor = anchor_values[0]
        own = {
            answer
            for fact in context.facts_by_subject.get(anchor, ())
            for answer in _answer_forms(fact, context.people_by_id)
        }
        if own.intersection(_answer_forms(finals[0], context.people_by_id)):
            reasons.append("multi_doc_answer_in_anchor_subject")
    if (
        form == "comparison"
        and _attribute(finals[0]) in _COMPARED_ATTRIBUTES
        and str(finals[0].value) == str(finals[1].value)
    ):
        reasons.append("multi_doc_comparison_tie")
    if not anchor_values or not all(_visible(a, context) for a in anchor_values):
        reasons.append("multi_doc_anchor_not_in_docs")
    if any(
        set(fact_ids).issubset(doc_facts)
        for doc_facts in context.document_fact_ids.values()
    ):
        reasons.append("multi_doc_single_document")
    if any(
        anchor in _answer_forms(fact, context.people_by_id)
        for fact in facts
        for anchor in anchor_values
    ):
        reasons.append("multi_doc_answer_is_anchor")
    if finals:
        anchors = [_norm(anchor) for anchor in anchor_values]
        answers = [_strict_forms(fact, context) for fact in finals]
        if any(
            all(anchor in body for anchor in anchors)
            and all(any(form in body for form in options) for options in answers)
            for body in context.norm_bodies
        ):
            reasons.append("multi_doc_one_document_answers")
    return reasons


def _comparison_reference(facts: Sequence[Fact], people: Mapping[str, Person]) -> str:
    parts = [f"{fact.subject}: {_answer_forms(fact, people)[0]}" for fact in facts]
    if _attribute(facts[0]) in _COMPARED_ATTRIBUTES:
        early, late = sorted(facts, key=lambda fact: str(fact.value))
        return f"{early.subject}이(가) {late.subject}보다 먼저다 ({' / '.join(parts)})"
    return " / ".join(parts)


def _answer_fact_ids(question: Question) -> tuple[str, ...]:
    if question.type in {"sql", "unanswerable"}:
        return ()
    if question.type == "permission":
        return tuple(q.fact_id for q in question.qrels[:1] if q.fact_id)
    return tuple(
        sorted({q.fact_id for q in question.qrels if q.rel == 2 and q.fact_id})
    )


def _loose_key(
    question_type: str,
    anchor_values: tuple[str, ...],
    fact_ids: Sequence[str],
    as_of: str | None,
    temporal_mode: str | None,
    discriminator: str,
) -> tuple[object, ...]:
    keep = discriminator if question_type in {"permission", "sql"} else ""
    return (
        question_type,
        anchor_values,
        tuple(sorted(fact_ids)),
        as_of or "",
        temporal_mode or "",
        keep,
    )


def _candidate_loose_key(candidate: _Candidate) -> tuple[object, ...]:
    return _loose_key(
        candidate.type,
        candidate.anchor_values,
        candidate.answer_fact_ids,
        candidate.as_of,
        candidate.temporal_mode,
        candidate.discriminator,
    )


def _question_loose_key(question: Question) -> tuple[object, ...]:
    if question.type == "permission":
        discriminator = question.qrels[0].doc_id if question.qrels else ""
    elif question.type == "sql":
        discriminator = question.anchor_values[0] if question.anchor_values else ""
    else:
        discriminator = ""
    return _loose_key(
        question.type,
        question.anchor_values,
        _answer_fact_ids(question),
        question.as_of,
        "event" if question.type == "temporal" else None,
        discriminator,
    )


def _resync_question(
    question: Question, context: _SyncContext
) -> tuple[Question, list[str]]:
    """Recompute evidence for one question and re-check its conditions."""
    seed, docs, index = context.seed, context.docs, context.index
    reasons: list[str] = []
    updated = question
    fact_ids = _answer_fact_ids(question)
    if question.type == "permission":
        updated = _resync_permission(question, docs, reasons)
    elif question.type == "unanswerable":
        updated = _resync_unanswerable(question, context, reasons)
    elif question.type == "sql":
        reasons.extend(_sql_reasons(question, context))
    elif question.type != "sql":
        unknown = [fact_id for fact_id in fact_ids if fact_id not in seed.facts]
        if not fact_ids or unknown:
            reasons.append("unknown_answer_fact")
        else:
            unmarked: set[str] = set()
            for fact_id in fact_ids:
                marked = {doc_id for doc_id, _, _ in index.get(fact_id, ())}
                unmarked.update(
                    unmarked_mentions(
                        seed.facts[fact_id], docs, context.people_by_id, marked
                    )
                )
            updated = replace(
                question,
                qrels=build_qrels(fact_ids, seed=seed, docs=docs, index=index),
                unmarked_candidates=tuple(sorted(unmarked)),
            )
            reasons.extend(_fact_type_reasons(question, fact_ids, context))
    errors = validate_question(
        replace(updated, question="명세 단계 빈 질문 허용"),
        docs=docs,
        regulation_index={},
        table_ids=set(),
    )
    reasons.extend(f"validate:{code}" for code in errors)
    return updated, reasons


def _resync_permission(
    question: Question,
    docs: Mapping[str, Mapping[str, object]],
    reasons: list[str],
) -> Question:
    if not question.qrels:
        reasons.append("permission_canary_missing")
        return question
    doc_id = question.qrels[0].doc_id
    canary = docs.get(doc_id, {}).get("canary")
    if not isinstance(canary, Mapping):
        reasons.append("permission_canary_missing")
        return question
    fact_id, start, end = canary.get("fact_id"), canary.get("start"), canary.get("end")
    if (
        not isinstance(fact_id, str)
        or not isinstance(start, int)
        or not isinstance(end, int)
    ):
        reasons.append("permission_canary_invalid")
        return question
    return replace(question, qrels=(_span_qrel(doc_id, 2, fact_id, start, end, docs),))


def _resync_unanswerable(
    question: Question, context: _SyncContext, reasons: list[str]
) -> Question:
    subject = question.anchor_values[0] if question.anchor_values else ""
    subject_facts = context.facts_by_subject.get(subject, ())
    bodies = [
        body
        for document in context.docs.values()
        if isinstance((body := document.get("body")), str)
    ]
    seed_any = docs_any = both_any = False
    for _, _, keywords, seed_attributes in _NO_ANSWER_ATTRIBUTES:
        seed_ok = not any(
            fact.kind == "count" or _attribute(fact) in seed_attributes
            for fact in subject_facts
        )
        docs_ok = not any(
            subject in body and any(keyword in body for keyword in keywords)
            for body in bodies
        )
        seed_any, docs_any = seed_any or seed_ok, docs_any or docs_ok
        both_any = both_any or (seed_ok and docs_ok)
    if not both_any:
        reasons.append("unanswerable_now_answerable")
    previous = question.no_answer_checks or {}
    return replace(
        question,
        no_answer_checks={
            "seed": seed_any,
            "docs": docs_any,
            "real": previous.get("real"),
        },
    )


def _sql_reasons(question: Question, context: _SyncContext) -> list[str]:
    reasons: list[str] = []
    if question.anchor_values[:1] not in {(anchor,) for anchor, _ in _SQL_QUERIES}:
        reasons.append("sql_anchor_not_natural")
    if question.sql is None:
        reasons.append("sql_missing")
    elif context.db_rows is not None and question.sql.expected != _run_sql(
        question.sql.query, context.db_rows, question.asker
    ):
        reasons.append("sql_expected_not_filtered")
    return reasons


def _check_sql_depends_on_asker(
    questions: Sequence[Question],
    db_rows: Mapping[str, Sequence[Mapping[str, object]]],
) -> None:
    """At least two SQL answers must differ from the unfiltered aggregate."""
    sql = [question for question in questions if question.sql is not None]
    dependent = sum(
        question.sql.expected != _run_sql(question.sql.query, db_rows, None)
        for question in sql
        if question.sql is not None
    )
    if sql and dependent < min(2, len(sql)):
        raise SpecBuildError(
            f"SQL questions whose value depends on the asker: {dependent} < 2"
        )


def _fact_type_reasons(
    question: Question, fact_ids: tuple[str, ...], context: _SyncContext
) -> list[str]:
    reasons: list[str] = []
    if question.type == "multi_doc":
        reasons.extend(_multi_doc_reasons(question.anchor_values, fact_ids, context))
        readable: dict[str, bool] = defaultdict(bool)
        for qrel in question.qrels:
            if qrel.rel == 2 and qrel.fact_id:
                access = context.docs.get(qrel.doc_id, {}).get("access")
                readable[qrel.fact_id] |= isinstance(access, Mapping) and can_read(
                    question.asker, access
                )
        if not all(readable.values()):
            reasons.append("multi_doc_answer_inaccessible")

    if question.type == "semantic" and len(fact_ids) == 1:
        anchor = question.anchor_values[0] if question.anchor_values else ""
        person_ids = {p.person_id for p in context.seed.people if p.name == anchor}
        involved = {
            subject
            for subject, facts in context.facts_by_subject.items()
            if person_ids.intersection(_person_ids_for_subject(facts))
        }
        if person_ids and len(involved) > 1:
            reasons.append("semantic_person_multiple_subjects")
        if len(_semantic_group(anchor, _attribute_id(fact_ids[0]), context)) != 1:
            reasons.append("semantic_ambiguous_subject_attribute")
    if question.type == "temporal":
        service = question.anchor_values[0] if question.anchor_values else ""
        attribute = _attribute_id(fact_ids[0]) if len(fact_ids) == 1 else ""
        group = [
            fact
            for fact in context.seed.facts.values()
            if fact.kind == "text"
            and fact.date is not None
            and context.index.get(fact.fact_id)
            and _service_for_subject(context.seed, fact.subject) == service
            and _attribute(fact) == attribute
        ]
        if len(fact_ids) != 1 or len(group) < 2:
            reasons.append("temporal_insufficient_candidates")
        else:
            date_of = context.seed.facts[fact_ids[0]].date
            if sum(fact.date == date_of for fact in group) != 1:
                reasons.append("temporal_date_not_unique")
    return reasons


def _semantic_group(anchor: str, attribute: str, context: _SyncContext) -> list[Fact]:
    seed = context.seed
    person_ids = {p.person_id for p in seed.people if p.name == anchor}
    is_service = anchor in {service.name for service in seed.services}
    group: list[Fact] = []
    for fact in seed.facts.values():
        if (
            fact.kind != "text"
            or not context.index.get(fact.fact_id)
            or _attribute(fact) != attribute
        ):
            continue
        if is_service:
            member = _service_for_subject(seed, fact.subject) == anchor
        else:
            owners = _person_ids_for_subject(context.facts_by_subject[fact.subject])
            member = bool(person_ids.intersection(owners))
        if member:
            group.append(fact)
    return group


def _rank_key(random_seed: int):
    def rank(candidate: _Candidate) -> str:
        material = f"{random_seed}:{candidate.key!r}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    return rank


def _pick_replacement(
    pool: Sequence[_Candidate],
    *,
    set_name: str,
    question_type: str,
    random_seed: int,
    used_keys: set[tuple[object, ...]] | frozenset[tuple[object, ...]],
    gold_answer_facts: frozenset[str],
    type_counts: Counter[str],
    set_counts: Counter[str],
    avoid_facts: frozenset[str] = frozenset(),
) -> _Candidate:
    """Take the first candidate by per-candidate hash that fits every rule.

    Candidates touching ``avoid_facts`` are tried only after all others, so a
    gold replacement does not knock out a surviving silver question.
    """
    type_cap, set_cap = DIVERSITY_CAPS[set_name]
    ranked = sorted(pool, key=_rank_key(random_seed))
    ranked.sort(key=lambda item: not avoid_facts.isdisjoint(item.answer_fact_ids))
    for candidate in ranked:
        if candidate.key in used_keys:
            continue
        if set_name == "silver" and not gold_answer_facts.isdisjoint(
            candidate.answer_fact_ids
        ):
            continue
        if any(
            type_counts[s] >= type_cap or set_counts[s] >= set_cap
            for s in candidate.subjects
        ):
            continue
        return candidate
    raise SpecBuildError(
        f"no replacement candidate: set={set_name} type={question_type}"
    )


def _replacement_asker(
    candidate: _Candidate,
    people: Sequence[Person],
    docs: Mapping[str, Mapping[str, object]],
    *,
    random_seed: int,
    qid: str,
) -> Asker:
    eligible = _eligible_people(candidate, people, docs)
    if not eligible:
        raise SpecBuildError(f"no eligible asker for candidate: {candidate.key}")
    material = f"{random_seed}:asker:{qid}".encode()
    person = random.Random(int(hashlib.sha256(material).hexdigest(), 16)).choice(
        eligible
    )
    return Asker(team=person.team, grade=person.grade)


def _invalidate_drafts(root: Path, ids: Iterable[str]) -> int:
    """Replaced or dropped specs lose their drafts and are drafted again."""
    from kodocs.eval.query_draft import invalidate_drafts

    return invalidate_drafts(root, ids)


def run(root: Path, *, reselect: bool = False, print_fn=print) -> int:
    """Sync existing specifications to the corpus, or select from scratch."""
    root = root.resolve()
    try:
        seed = load_seed(root)
        docs = _load_docs(root / DOCS_PATH)
        db_manifest = _load_mapping(root / DB_MANIFEST_PATH)
        db_content_hash = db_manifest.get("content_hash")
        if not isinstance(db_content_hash, str) or not db_content_hash:
            raise SpecBuildError("synth_db manifest has no content_hash")
        rows = build_rows(seed)
        random_seed = int(seed.company["random_seed"])
        specs_path = root / SPECS_PATH
        summary_path = root / SUMMARY_PATH
        sync: SyncResult | None = None
        if specs_path.exists() and not reselect:
            sync = sync_specs(
                load_questions(specs_path),
                seed,
                docs,
                db_rows=rows,
                db_content_hash=db_content_hash,
                random_seed=random_seed,
                fill=True,
                overrides=load_overrides(root / OVERRIDES_PATH),
            )
            result = sync.build
        else:
            result = _build_specs_result(
                seed,
                docs,
                db_rows=rows,
                db_content_hash=db_content_hash,
                random_seed=random_seed,
            )
        invalidated = (
            _invalidate_drafts(root, [*sync.replaced, *sync.dropped, *sync.rewritten])
            if sync
            else 0
        )
        write_questions(specs_path, sorted(result.questions, key=lambda item: item.id))
        summary = _summary(root, result, docs, db_content_hash)
        if sync is not None and (sync.qrels_changed or sync.replaced or sync.dropped):
            summary["sync"] = {
                "qrels_changed": list(sync.qrels_changed),
                "replaced": {qid: list(why) for qid, why in sync.replaced.items()},
                "dropped": list(sync.dropped),
            }
        _write_json(summary_path, summary)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print_fn(f"golden specs failed: {exc}")
        return 1
    if sync is not None:
        print_fn(f"qrels changed: {len(sync.qrels_changed)} {list(sync.qrels_changed)}")
        print_fn(f"replaced: {len(sync.replaced)}")
        for qid, why in sync.replaced.items():
            print_fn(f"  {qid}: {', '.join(why)}")
        print_fn(f"dropped (over quota): {len(sync.dropped)} {list(sync.dropped)}")
        print_fn(f"anchor rewritten: {len(sync.rewritten)} {list(sync.rewritten)}")
        print_fn(f"drafts invalidated: {invalidated}")
    print_fn(
        f"wrote {len(result.questions)} specifications to "
        f"{_display_path(specs_path, root)}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for ``python -m kodocs.eval.golden_specs``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--reselect",
        action="store_true",
        help="ignore existing specs and select every question from scratch",
    )
    args = parser.parse_args(argv)
    return run(args.root, reselect=args.reselect)


def _candidate_pools(
    seed: Seed,
    docs: Mapping[str, Mapping[str, object]],
    index: Mapping[str, Sequence[tuple[str, int, int]]],
) -> tuple[dict[str, list[_Candidate]], Counter[str]]:
    rejected: Counter[str] = Counter()
    facts_by_subject = _facts_by_subject(seed)
    document_fact_ids = _document_fact_ids(docs)
    people = {person.person_id: person for person in seed.people}
    pools: dict[str, list[_Candidate]] = {
        question_type: []
        for question_type in QUESTION_TYPES
        if question_type not in {"table", "sql"}
    }

    for fact in sorted(seed.facts.values(), key=lambda item: item.fact_id):
        if fact.kind != "id" or not index.get(fact.fact_id):
            continue
        for answer in facts_by_subject[fact.subject]:
            if answer.fact_id == fact.fact_id or answer.kind == "id":
                continue
            candidate = _fact_candidate(
                "identifier",
                (str(fact.value),),
                (answer,),
                seed=seed,
                docs=docs,
                index=index,
                people=people,
                document_fact_ids=document_fact_ids,
            )
            if candidate is not None:
                pools["identifier"].append(candidate)

    semantic_groups: dict[tuple[str, str], list[Fact]] = defaultdict(list)
    person_semantic_groups: dict[tuple[str, str], list[Fact]] = defaultdict(list)
    for fact in seed.facts.values():
        if fact.kind != "text" or not index.get(fact.fact_id):
            continue
        service = _service_for_subject(seed, fact.subject)
        if service is not None:
            semantic_groups[(service, _attribute(fact))].append(fact)
        for person_id in _person_ids_for_subject(facts_by_subject[fact.subject]):
            person_semantic_groups[(person_id, _attribute(fact))].append(fact)

    semantic_by_fact: dict[str, _Candidate] = {}
    for (service, _), facts in sorted(semantic_groups.items()):
        if len(facts) != 1:
            rejected["semantic_ambiguous_subject_attribute"] += len(facts)
            continue
        fact = facts[0]
        candidate = _fact_candidate(
            "semantic",
            _semantic_anchor(seed, service, fact),
            (fact,),
            seed=seed,
            docs=docs,
            index=index,
            people=people,
            document_fact_ids=document_fact_ids,
        )
        if candidate is not None:
            semantic_by_fact[fact.fact_id] = candidate
    person_subjects: dict[str, set[str]] = defaultdict(set)
    for subject, facts in facts_by_subject.items():
        for person_id in _person_ids_for_subject(facts):
            person_subjects[person_id].add(subject)
    for (person_id, _), facts in sorted(person_semantic_groups.items()):
        if len(facts) != 1:
            rejected["semantic_ambiguous_subject_attribute"] += len(facts)
            continue
        if len(person_subjects[person_id]) > 1:
            rejected["semantic_person_multiple_subjects"] += 1
            continue
        fact = facts[0]
        if fact.fact_id in semantic_by_fact:
            continue
        person = people[person_id]
        candidate = _fact_candidate(
            "semantic",
            (person.name,),
            (fact,),
            seed=seed,
            docs=docs,
            index=index,
            people=people,
            document_fact_ids=document_fact_ids,
        )
        if candidate is not None:
            semantic_by_fact[fact.fact_id] = candidate
    pools["semantic"] = sorted(semantic_by_fact.values(), key=lambda item: item.key)

    context = _sync_context(seed, docs, index=index)
    service_names = {service.name for service in seed.services}
    for subject, facts in sorted(facts_by_subject.items()):
        usable = sorted(
            (fact for fact in facts if index.get(fact.fact_id)),
            key=lambda item: item.fact_id,
        )
        if subject in service_names or not usable:
            continue
        if not _visible(subject, context):
            rejected["multi_doc_anchor_not_in_docs"] += 1
            continue
        pairs = list(combinations(usable, 2))
        bridges = [
            fact
            for fact in usable
            if fact.kind == "service" and _attribute(fact) == "service"
        ]
        if len(bridges) == 1:
            bridge = bridges[0]
            pairs.extend(
                (bridge, fact)
                for fact in facts_by_subject.get(str(bridge.value), ())
                if index.get(fact.fact_id)
            )
        for left, right in pairs:
            reasons = _multi_doc_reasons(
                (subject,), (left.fact_id, right.fact_id), context
            )
            if reasons:
                rejected[
                    "multi_doc_one_document_has_all_facts"
                    if "multi_doc_single_document" in reasons
                    else reasons[0]
                ] += 1
                continue
            candidate = _fact_candidate(
                "multi_doc",
                (subject,),
                (left, right),
                seed=seed,
                docs=docs,
                index=index,
                people=people,
                document_fact_ids=document_fact_ids,
            )
            if candidate is not None:
                pools["multi_doc"].append(candidate)

    by_attribute: dict[str, list[Fact]] = defaultdict(list)
    for subject, facts in sorted(facts_by_subject.items()):
        if subject in service_names or not _visible(subject, context):
            continue
        per_attribute: dict[str, list[Fact]] = defaultdict(list)
        for fact in facts:
            if index.get(fact.fact_id) and fact.kind != "id":
                per_attribute[_attribute(fact)].append(fact)
        for attribute, group in per_attribute.items():
            if len(group) == 1 and attribute != "service":
                by_attribute[attribute].append(group[0])
    for _attribute_name, group in sorted(by_attribute.items()):
        for left, right in combinations(sorted(group, key=lambda f: f.subject), 2):
            anchors = (left.subject, right.subject)
            reasons = _multi_doc_reasons(
                anchors, (left.fact_id, right.fact_id), context
            )
            if reasons:
                rejected[
                    f"multi_doc_comparison_{reasons[0].removeprefix('multi_doc_')}"
                ] += 1
                continue
            candidate = _fact_candidate(
                "multi_doc",
                anchors,
                (left, right),
                seed=seed,
                docs=docs,
                index=index,
                people=people,
                document_fact_ids=document_fact_ids,
            )
            if candidate is not None:
                pools["multi_doc"].append(
                    replace(
                        candidate,
                        reference_answer=_comparison_reference((left, right), people),
                    )
                )

    temporal_groups: dict[tuple[str, str], list[Fact]] = defaultdict(list)
    for fact in seed.facts.values():
        service = _service_for_subject(seed, fact.subject)
        if service is None or fact.date is None or not index.get(fact.fact_id):
            continue
        if fact.kind == "text":
            temporal_groups[(service, _attribute(fact))].append(fact)

    for (service, _), facts in sorted(temporal_groups.items()):
        dates = Counter(fact.date for fact in facts)
        if len(facts) < 2:
            rejected["temporal_without_multiple_candidates"] += len(facts)
            continue
        for fact in sorted(facts, key=lambda item: (item.date, item.fact_id)):
            if dates[fact.date] != 1:
                rejected["temporal_date_not_unique"] += 1
                continue
            candidate = _fact_candidate(
                "temporal",
                (service,),
                (fact,),
                seed=seed,
                docs=docs,
                index=index,
                people=people,
                document_fact_ids=document_fact_ids,
                tags=("temporal:as_of_event",),
                as_of=fact.date.isoformat(),
                temporal_mode="event",
                temporal_candidate_count=len(facts),
            )
            if candidate is not None:
                pools["temporal"].append(candidate)

    for doc_id, document in sorted(docs.items()):
        canary = document.get("canary")
        if not isinstance(canary, Mapping):
            continue
        fact_id = canary.get("fact_id")
        value = canary.get("value")
        start = canary.get("start")
        end = canary.get("end")
        if (
            not isinstance(fact_id, str)
            or not isinstance(value, str)
            or not isinstance(start, int)
            or not isinstance(end, int)
        ):
            raise SpecBuildError(f"invalid canary: {doc_id}")
        qrel = _span_qrel(doc_id, 2, fact_id, start, end, docs)
        anchor = _anchor_for_document(seed, document)
        tags = {"canary"}
        if document.get("injection") is not None:
            tags.add("injection")
        pools["permission"].append(
            _Candidate(
                type="permission",
                anchor_values=anchor,
                answer_values=(value,),
                answer_fact_ids=(fact_id,),
                qrels=(qrel,),
                reference_answer=value,
                expected_response="indistinguishable_from_no_answer",
                tags=tuple(sorted(tags)),
                document_fact_ids=document_fact_ids,
                discriminator=doc_id,
            )
        )

    no_answer_subjects = [service.name for service in seed.services]
    no_answer_subjects.extend(
        subject for subject in sorted(seed.entities) if subject.startswith("INC-")
    )
    for subject in no_answer_subjects:
        subject_facts = facts_by_subject.get(subject, ())
        for attribute, label, keywords, seed_attributes in _NO_ANSWER_ATTRIBUTES:
            seed_check = not any(
                fact.kind == "count" or _attribute(fact) in seed_attributes
                for fact in subject_facts
            )
            docs_check = not any(
                subject in body and any(keyword in body for keyword in keywords)
                for document in docs.values()
                if isinstance((body := document.get("body")), str)
            )
            if not seed_check:
                rejected["unanswerable_seed_has_attribute"] += 1
            if not docs_check:
                rejected["unanswerable_docs_have_subject_and_keyword"] += 1
            if not seed_check or not docs_check:
                continue
            pools["unanswerable"].append(
                _Candidate(
                    type="unanswerable",
                    anchor_values=(subject,),
                    answer_values=(),
                    answer_fact_ids=(),
                    qrels=(),
                    reference_answer="확인할 수 없다.",
                    expected_response="no_answer",
                    no_answer_checks={"seed": True, "docs": True, "real": None},
                    document_fact_ids=document_fact_ids,
                    discriminator=attribute + ":" + label,
                )
            )

    for question_type, candidates in pools.items():
        pools[question_type] = sorted(candidates, key=lambda item: item.key)
    return pools, rejected


def _sql_candidates(
    db_rows: Mapping[str, Sequence[Mapping[str, object]]],
    db_content_hash: str,
) -> list[_Candidate]:
    """Pool entries per fixed query; the value is bound per asker by _bind_sql."""
    return [
        _sql_candidate(anchor, query, _run_sql(query, db_rows, None), db_content_hash)
        for anchor, query in _SQL_QUERIES
    ]


def _bind_sql(
    candidate: _Candidate,
    asker: Asker | None,
    db_rows: Mapping[str, Sequence[Mapping[str, object]]],
    db_content_hash: str,
) -> _Candidate:
    """Recompute the expected value over the rows the asker may read."""
    if candidate.sql is None:
        return candidate
    bound = _sql_candidate(
        candidate.anchor_values[0],
        candidate.sql.query,
        _run_sql(candidate.sql.query, db_rows, asker),
        db_content_hash,
    )
    return replace(
        candidate,
        sql=bound.sql,
        answer_values=bound.answer_values,
        reference_answer=bound.reference_answer,
    )


def _sql_candidate(
    anchor: str, query: str, expected: list[dict[str, object]], db_content_hash: str
) -> _Candidate:
    compact = json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
    return _Candidate(
        type="sql",
        anchor_values=(anchor,),
        answer_values=(compact,),
        answer_fact_ids=(),
        qrels=(),
        reference_answer=compact,
        sql=SqlAnswer(
            query=query,
            query_sha256=hashlib.sha256(query.encode("utf-8")).hexdigest(),
            expected=expected,
            db_content_hash=db_content_hash,
        ),
        discriminator=anchor,
    )


def _row_readable(row: Mapping[str, object], asker: Asker | None) -> bool:
    if asker is None or "access_level" not in row:
        return True
    teams = str(row.get("access_teams") or "")
    return can_read(
        asker,
        {
            "level": row["access_level"],
            "teams": [team for team in teams.split(",") if team],
            "min_grade": row.get("access_min_grade"),
        },
    )


def _run_sql(
    query: str,
    db_rows: Mapping[str, Sequence[Mapping[str, object]]],
    asker: Asker | None,
) -> list[dict[str, object]]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(
            """
            CREATE TABLE tickets (
                owner_team TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE approvals (
                owner_team TEXT NOT NULL,
                status TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                amount_krw INTEGER NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO tickets VALUES (:owner_team, :status, :created_at)",
            [r for r in db_rows.get("tickets", ()) if _row_readable(r, asker)],
        )
        connection.executemany(
            "INSERT INTO approvals VALUES "
            "(:owner_team, :status, :submitted_at, :amount_krw)",
            [r for r in db_rows.get("approvals", ()) if _row_readable(r, asker)],
        )
        return [dict(row) for row in connection.execute(query).fetchall()]
    finally:
        connection.close()


def _annotate_subjects(
    candidate: _Candidate, seed: Seed, docs: Mapping[str, Mapping[str, object]]
) -> _Candidate:
    subjects = sorted(
        {
            seed.facts[fact_id].subject
            for fact_id in candidate.answer_fact_ids
            if fact_id in seed.facts
        }
    )
    if not subjects:
        if candidate.type == "permission":
            subjects = [f"doc:{candidate.discriminator}"]
        elif candidate.type == "sql":
            subjects = [f"sql:{candidate.discriminator}"]
        else:
            subjects = list(candidate.anchor_values[:1])
    service = next(
        (s for s in (_service_for_subject(seed, x) for x in subjects) if s), None
    )
    if service is None and candidate.type == "permission":
        team = docs.get(candidate.discriminator, {}).get("team")
        service = f"team:{team}" if isinstance(team, str) else None
    return replace(candidate, subjects=tuple(subjects), service=service or "none")


def _select_diverse(
    candidates: Sequence[_Candidate],
    quota: int,
    *,
    set_name: str,
    question_type: str,
    seed_key: str,
    type_cap: int,
    set_cap: int,
    set_counts: Counter[str],
    avoid: frozenset[str] = frozenset(),
) -> list[_Candidate]:
    """Shuffle deterministically, then take round-robin by service then subject.

    A candidate is taken only while every subject it draws on stays under the
    per-type cap and the per-set cap (multi_doc counts each subject once).
    Subjects in ``avoid`` (used by the gold set) are skipped on a first pass.
    Caps are never relaxed: a shortage raises with the achievable maximum.
    """
    shuffled = list(candidates)
    random.Random(seed_key).shuffle(shuffled)
    by_service: dict[str, dict[str, list[_Candidate]]] = {}
    for candidate in shuffled:
        first = candidate.subjects[0] if candidate.subjects else ""
        by_service.setdefault(candidate.service, {}).setdefault(first, []).append(
            candidate
        )
    type_counts: Counter[str] = Counter()
    selected: list[_Candidate] = []
    taken: set[int] = set()

    def allowed(candidate: _Candidate, skip: frozenset[str]) -> bool:
        return all(
            type_counts[s] < type_cap and set_counts[s] < set_cap and s not in skip
            for s in candidate.subjects
        )

    for skip in (avoid, frozenset()) if avoid else (frozenset(),):
        rotation = {service: 0 for service in by_service}
        progress = True
        while len(selected) < quota and progress:
            progress = False
            for service, subjects in by_service.items():
                if len(selected) >= quota:
                    break
                keys = list(subjects)
                for offset in range(len(keys)):
                    key = keys[(rotation[service] + offset) % len(keys)]
                    pick = next(
                        (
                            c
                            for c in subjects[key]
                            if id(c) not in taken and allowed(c, skip)
                        ),
                        None,
                    )
                    if pick is None:
                        continue
                    rotation[service] = (rotation[service] + offset + 1) % len(keys)
                    taken.add(id(pick))
                    selected.append(pick)
                    type_counts.update(pick.subjects)
                    set_counts.update(pick.subjects)
                    progress = True
                    break
    if len(selected) < quota:
        raise SpecBuildError(
            f"diversity shortage: set={set_name} type={question_type} "
            f"max={len(selected)} quota={quota}"
        )
    return selected


def _diversity(selected: Sequence[_Candidate]) -> dict[str, int]:
    counts = Counter(s for candidate in selected for s in candidate.subjects)
    return {
        "distinct_subjects": len(counts),
        "max_per_subject": max(counts.values(), default=0),
    }


def _build_specs_result(
    seed: Seed,
    docs: Mapping[str, Mapping[str, object]],
    *,
    db_rows: Mapping[str, Sequence[Mapping[str, object]]],
    db_content_hash: str,
    random_seed: int,
) -> _BuildResult:
    eligible, rejected = _eligible_pools(
        seed, docs, db_rows=db_rows, db_content_hash=db_content_hash
    )
    people = tuple(sorted(seed.people, key=lambda item: item.person_id))
    candidate_counts: dict[str, dict[str, int]] = {"gold": {}, "silver": {}}
    totals: dict[str, Counter[str]] = {"gold": Counter(), "silver": Counter()}

    def by_scarcity(quotas: Mapping[str, int], pools: Mapping[str, list]) -> list[str]:
        # Scarce types first so the per-set cap does not starve them.
        return sorted(
            (t for t, q in quotas.items() if q),
            key=lambda t: (len(pools.get(t, [])) / quotas[t], t),
        )

    gold_quotas = {
        question_type: GOLD_QUOTA.get((question_type, "synthetic"), 0)
        for question_type in QUESTION_TYPES
    }
    type_cap, set_cap = DIVERSITY_CAPS["gold"]
    gold_by_type: dict[str, list[_Candidate]] = {}
    for question_type in by_scarcity(gold_quotas, eligible):
        available = eligible.get(question_type, [])
        candidate_counts["gold"][question_type] = len(available)
        gold_by_type[question_type] = _select_diverse(
            available,
            gold_quotas[question_type],
            set_name="gold",
            question_type=question_type,
            seed_key=f"{random_seed}:gold:{question_type}",
            type_cap=type_cap,
            set_cap=set_cap,
            set_counts=totals["gold"],
        )
    selected_gold = [c for t in QUESTION_TYPES for c in gold_by_type.get(t, [])]

    gold_answer_facts = {
        fact_id for candidate in selected_gold for fact_id in candidate.answer_fact_ids
    }
    gold_keys = {candidate.key for candidate in selected_gold}
    gold_subjects = frozenset(s for c in selected_gold for s in c.subjects)
    silver_pools = {
        question_type: [
            candidate
            for candidate in eligible[question_type]
            if candidate.key not in gold_keys
            and gold_answer_facts.isdisjoint(candidate.answer_fact_ids)
        ]
        for question_type in SILVER_QUOTA
    }
    type_cap, set_cap = DIVERSITY_CAPS["silver"]
    silver_by_type: dict[str, list[_Candidate]] = {}
    for question_type in by_scarcity(SILVER_QUOTA, silver_pools):
        available = silver_pools[question_type]
        candidate_counts["silver"][question_type] = len(available)
        silver_by_type[question_type] = _select_diverse(
            available,
            SILVER_QUOTA[question_type],
            set_name="silver",
            question_type=question_type,
            seed_key=f"{random_seed}:silver:{question_type}",
            type_cap=type_cap,
            set_cap=set_cap,
            set_counts=totals["silver"],
            avoid=gold_subjects,
        )
    selected_silver = [c for t in SILVER_QUOTA for c in silver_by_type[t]]
    diversity_summary = _diversity_summary(
        {t: gold_by_type[t] for t in gold_by_type},
        {t: silver_by_type[t] for t in SILVER_QUOTA},
    )

    questions: list[Question] = []
    ordinal = 0
    for set_name, selected, prefix in (
        ("gold", selected_gold, "g-syn"),
        ("silver", selected_silver, "s-syn"),
    ):
        for set_number, candidate in enumerate(selected, start=1):
            ordinal += 1
            asker = _choose_asker(
                candidate,
                people,
                docs,
                random_seed=random_seed,
                ordinal=ordinal,
            )
            candidate = _bind_sql(candidate, asker, db_rows, db_content_hash)
            questions.append(
                _question_from_candidate(
                    candidate,
                    qid=f"{prefix}-{set_number:03d}",
                    set_name=set_name,
                    asker=asker,
                )
            )

    validation_errors: list[str] = []
    for question in questions:
        errors = validate_question(
            replace(question, question="명세 단계 빈 질문 허용"),
            docs=docs,
            regulation_index={},
            table_ids=set(),
        )
        if errors:
            validation_errors.append(f"{question.id}: {','.join(errors)}")
    if validation_errors:
        raise SpecBuildError("validation errors: " + "; ".join(validation_errors))
    _check_sql_depends_on_asker(questions, db_rows)
    return _BuildResult(questions, candidate_counts, rejected, diversity_summary)


def _eligible_pools(
    seed: Seed,
    docs: Mapping[str, Mapping[str, object]],
    *,
    db_rows: Mapping[str, Sequence[Mapping[str, object]]],
    db_content_hash: str,
) -> tuple[dict[str, list[_Candidate]], Counter[str]]:
    index = fact_index(docs)
    pools, rejected = _candidate_pools(seed, docs, index)
    pools["sql"] = _sql_candidates(db_rows, db_content_hash)
    people = tuple(sorted(seed.people, key=lambda item: item.person_id))
    eligible: dict[str, list[_Candidate]] = {}
    for question_type, candidates in pools.items():
        accepted: list[_Candidate] = []
        for candidate in candidates:
            if _eligible_people(candidate, people, docs):
                accepted.append(_annotate_subjects(candidate, seed, docs))
            else:
                rejected[f"{question_type}_no_eligible_asker"] += 1
        eligible[question_type] = accepted
    return eligible, rejected


def _question_from_candidate(
    candidate: _Candidate, *, qid: str, set_name: str, asker: Asker
) -> Question:
    return Question(
        id=qid,
        set=set_name,
        source="synthetic",
        type=candidate.type,
        tags=candidate.tags,
        question="",
        asker=asker,
        as_of=candidate.as_of,
        qrels=candidate.qrels,
        anchor_values=candidate.anchor_values,
        answer_values=candidate.answer_values,
        expected_response=candidate.expected_response,
        sql=candidate.sql,
        reference_answer=candidate.reference_answer,
        reference_sha256=None,
        summary80=None,
        split=None,
        unmarked_candidates=candidate.unmarked_candidates,
        no_answer_checks=candidate.no_answer_checks,
    )


def _diversity_summary(
    gold_by_type: Mapping[str, Sequence[_Candidate]],
    silver_by_type: Mapping[str, Sequence[_Candidate]],
) -> dict[str, object]:
    selected_gold = [c for t in QUESTION_TYPES for c in gold_by_type.get(t, ())]
    selected_silver = [c for t in SILVER_QUOTA for c in silver_by_type.get(t, ())]
    totals = {
        "gold": Counter(s for c in selected_gold for s in c.subjects),
        "silver": Counter(s for c in selected_silver for s in c.subjects),
    }
    gold_subjects = frozenset(s for c in selected_gold for s in c.subjects)
    silver_touching_gold = [
        c for c in selected_silver if gold_subjects.intersection(c.subjects)
    ]
    diversity = {
        "gold": {t: _diversity(c) for t, c in gold_by_type.items()},
        "silver": {t: _diversity(c) for t, c in silver_by_type.items()},
    }
    return {
        "diversity": {k: dict(sorted(v.items())) for k, v in diversity.items()},
        "diversity_totals": {
            set_name: {
                "distinct_subjects": len(counter),
                "max_per_subject": max(counter.values(), default=0),
            }
            for set_name, counter in totals.items()
        },
        "diversity_caps": {
            set_name: {"per_type": caps[0], "per_set": caps[1]}
            for set_name, caps in DIVERSITY_CAPS.items()
        },
        "gold_silver_overlap": {
            "subjects": len(
                gold_subjects & {s for c in selected_silver for s in c.subjects}
            ),
            "silver_questions": len(silver_touching_gold),
        },
    }


def _fact_candidate(
    question_type: str,
    anchor_values: tuple[str, ...],
    answer_facts: tuple[Fact, ...],
    *,
    seed: Seed,
    docs: Mapping[str, Mapping[str, object]],
    index: Mapping[str, Sequence[tuple[str, int, int]]],
    people: Mapping[str, Person],
    document_fact_ids: Mapping[str, frozenset[str]],
    tags: tuple[str, ...] = (),
    as_of: str | None = None,
    temporal_mode: str | None = None,
    temporal_candidate_count: int = 0,
    discriminator: str = "",
) -> _Candidate | None:
    fact_ids = tuple(fact.fact_id for fact in answer_facts)
    if any(not index.get(fact_id) for fact_id in fact_ids):
        return None
    qrels = build_qrels(fact_ids, seed=seed, docs=docs, index=index)
    rel_two_docs = {qrel.doc_id for qrel in qrels if qrel.rel == 2}
    final_tags = set(tags)
    if any(docs[doc_id].get("injection") is not None for doc_id in rel_two_docs):
        final_tags.add("injection")
    unmarked: set[str] = set()
    answer_values: list[str] = []
    reference_parts: list[str] = []
    for fact in answer_facts:
        forms = _answer_forms(fact, people)
        answer_values.extend(forms)
        reference_parts.append(forms[0])
        unmarked.update(
            unmarked_mentions(
                fact,
                docs,
                people,
                {doc_id for doc_id, _, _ in index.get(fact.fact_id, ())},
            )
        )
    return _Candidate(
        type=question_type,
        anchor_values=tuple(dict.fromkeys(anchor_values)),
        answer_values=tuple(dict.fromkeys(answer_values)),
        answer_fact_ids=fact_ids,
        qrels=qrels,
        reference_answer=" ".join(reference_parts),
        tags=tuple(sorted(final_tags)),
        as_of=as_of,
        unmarked_candidates=tuple(sorted(unmarked)),
        temporal_mode=temporal_mode,
        temporal_candidate_count=temporal_candidate_count,
        document_fact_ids=document_fact_ids,
        discriminator=discriminator,
    )


def _eligible_people(
    candidate: _Candidate,
    people: Sequence[Person],
    docs: Mapping[str, Mapping[str, object]],
) -> list[Person]:
    if candidate.type in {"sql", "unanswerable"}:
        return list(people)
    relevant_docs = {qrel.doc_id for qrel in candidate.qrels if qrel.rel in {1, 2}}
    if candidate.type == "permission":
        return [
            person
            for person in people
            if all(
                not _person_can_read(person, docs[doc_id]) for doc_id in relevant_docs
            )
        ]
    rel_two_docs = {qrel.doc_id for qrel in candidate.qrels if qrel.rel == 2}
    if candidate.type == "multi_doc":
        by_fact: dict[str, set[str]] = defaultdict(set)
        for qrel in candidate.qrels:
            if qrel.rel == 2 and qrel.fact_id:
                by_fact[qrel.fact_id].add(qrel.doc_id)
        return [
            person
            for person in people
            if all(
                any(_person_can_read(person, docs[doc_id]) for doc_id in doc_ids)
                for doc_ids in by_fact.values()
            )
        ]
    return [
        person
        for person in people
        if any(_person_can_read(person, docs[doc_id]) for doc_id in rel_two_docs)
    ]


def _choose_asker(
    candidate: _Candidate,
    people: Sequence[Person],
    docs: Mapping[str, Mapping[str, object]],
    *,
    random_seed: int,
    ordinal: int,
) -> Asker:
    eligible = _eligible_people(candidate, people, docs)
    if not eligible:
        raise SpecBuildError(f"no eligible asker for candidate: {candidate.key}")
    person = random.Random(random_seed + ordinal).choice(eligible)
    return Asker(team=person.team, grade=person.grade)


def _person_can_read(person: Person, document: Mapping[str, object]) -> bool:
    access = document.get("access")
    return isinstance(access, Mapping) and can_read(
        Asker(team=person.team, grade=person.grade), access
    )


def _span_qrel(
    doc_id: str,
    rel: int,
    fact_id: str,
    start: int,
    end: int,
    docs: Mapping[str, Mapping[str, object]],
) -> Qrel:
    body = docs.get(doc_id, {}).get("body")
    if not isinstance(body, str) or not 0 <= start < end <= len(body):
        raise SpecBuildError(f"invalid qrel span: {doc_id}/{fact_id}")
    return Qrel(
        doc_id=doc_id,
        rel=rel,
        fact_id=fact_id,
        start=start,
        end=end,
        span_sha256=span_sha256(body[start:end]),
    )


def _facts_by_subject(seed: Seed) -> dict[str, tuple[Fact, ...]]:
    grouped: dict[str, list[Fact]] = defaultdict(list)
    for fact in seed.facts.values():
        grouped[fact.subject].append(fact)
    return {
        subject: tuple(sorted(facts, key=lambda item: item.fact_id))
        for subject, facts in grouped.items()
    }


def _document_fact_ids(
    docs: Mapping[str, Mapping[str, object]],
) -> dict[str, frozenset[str]]:
    result: dict[str, frozenset[str]] = {}
    for doc_id, document in sorted(docs.items()):
        spans = document.get("fact_spans", ())
        result[doc_id] = frozenset(
            str(span["fact_id"])
            for span in spans
            if isinstance(span, Mapping) and isinstance(span.get("fact_id"), str)
        )
    return result


def _service_for_subject(seed: Seed, subject: str) -> str | None:
    service_names = {service.name for service in seed.services}
    if subject in service_names:
        return subject
    entity = seed.entities.get(subject)
    if isinstance(entity, Mapping) and isinstance(entity.get("service"), str):
        return str(entity["service"])
    services = sorted(
        str(fact.value)
        for fact in seed.facts.values()
        if fact.subject == subject and fact.kind == "service"
    )
    return services[0] if services else None


def _person_ids_for_subject(facts: Sequence[Fact]) -> tuple[str, ...]:
    return tuple(sorted({str(fact.value) for fact in facts if fact.kind == "person"}))


def _attribute(fact: Fact) -> str:
    return _attribute_id(fact.fact_id)


def _attribute_id(fact_id: str) -> str:
    return fact_id.rsplit("-", 1)[-1]


def _answer_forms(fact: Fact, people: Mapping[str, Person]) -> tuple[str, ...]:
    if fact.kind == "text":
        return (str(fact.value),)
    forms = surface_forms(fact, people=people)
    return forms or (str(fact.value),)


def _anchor_for_document(seed: Seed, document: Mapping[str, object]) -> tuple[str, ...]:
    spans = document.get("fact_spans", ())
    if isinstance(spans, Sequence) and not isinstance(spans, (str, bytes)):
        for span in spans:
            if not isinstance(span, Mapping):
                continue
            fact_id = span.get("fact_id")
            fact = seed.facts.get(str(fact_id))
            if fact is None:
                continue
            service = _service_for_subject(seed, fact.subject)
            if service is not None:
                return (service,)
            if fact.kind == "id":
                return (str(fact.value),)
    doc_id = document.get("doc_id")
    return (str(doc_id),) if isinstance(doc_id, str) else ("제한 문서",)


def _load_docs(path: Path) -> dict[str, Mapping[str, object]]:
    docs: dict[str, Mapping[str, object]] = {}
    for doc_path in sorted(path.glob("*.json")):
        payload = _load_mapping(doc_path)
        doc_id = payload.get("doc_id")
        if not isinstance(doc_id, str) or not doc_id:
            raise SpecBuildError(f"document has no doc_id: {doc_path.name}")
        if doc_id in docs:
            raise SpecBuildError(f"duplicate document id: {doc_id}")
        docs[doc_id] = payload
    if not docs:
        raise SpecBuildError(f"no synthetic documents: {path}")
    return docs


def _load_mapping(path: Path) -> Mapping[str, object]:
    with path.open(encoding="utf-8") as source:
        payload = json.load(source)
    if not isinstance(payload, Mapping):
        raise SpecBuildError(f"expected JSON object: {path}")
    return payload


def _summary(
    root: Path,
    result: _BuildResult,
    docs: Mapping[str, Mapping[str, object]],
    db_content_hash: str,
) -> dict[str, object]:
    counts: dict[str, dict[str, int]] = {}
    for set_name in ("gold", "silver"):
        counter = Counter(
            question.type for question in result.questions if question.set == set_name
        )
        counts[set_name] = dict(sorted(counter.items()))
    tags = Counter(tag for question in result.questions for tag in question.tags)
    seed_files = sorted((root / "data/seed").rglob("*.yaml"))
    seed_hashes = {
        path.relative_to(root).as_posix(): sha256_file(path) for path in seed_files
    }
    doc_hashes = {
        (root / DOCS_PATH / f"{doc_id}.json").relative_to(root).as_posix(): sha256_file(
            root / DOCS_PATH / f"{doc_id}.json"
        )
        for doc_id in sorted(docs)
    }
    docs_hash = hashlib.sha256(
        json.dumps(
            doc_hashes,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "counts": counts,
        "tags": dict(sorted(tags.items())),
        "unmarked_candidate_questions": sum(
            bool(question.unmarked_candidates) for question in result.questions
        ),
        "rejected_candidates": dict(sorted(result.rejected.items())),
        "candidates": {
            set_name: dict(sorted(type_counts.items()))
            for set_name, type_counts in sorted(result.candidates.items())
        },
        **result.diversity,
        "inputs": {
            "seed_files_sha256": seed_hashes,
            "docs_sha256": docs_hash,
            "db_content_hash": db_content_hash,
        },
    }


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _display_path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)


if __name__ == "__main__":
    raise SystemExit(main())
