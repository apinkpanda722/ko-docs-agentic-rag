"""Build deterministic question specifications from the synthetic corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from itertools import combinations, pairwise
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
    span_sha256,
    validate_question,
    write_questions,
)
from kodocs.ingest.manifest import sha256_file
from kodocs.synth.db import build_rows
from kodocs.synth.seed import Fact, Person, Seed, load_seed, surface_forms

SPECS_PATH = Path("data/synthetic/queries/specs.jsonl")
SUMMARY_PATH = Path("data/manifest/golden_specs.json")
DOCS_PATH = Path("data/synthetic/docs")
DB_MANIFEST_PATH = Path("data/manifest/synth_db.json")

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
        "tickets_by_team",
        (
            "SELECT owner_team, COUNT(*) AS count FROM tickets "
            "GROUP BY owner_team ORDER BY owner_team"
        ),
    ),
    (
        "tickets_by_status",
        (
            "SELECT status, COUNT(*) AS count FROM tickets "
            "GROUP BY status ORDER BY status"
        ),
    ),
    (
        "tickets_by_month",
        (
            "SELECT substr(created_at, 1, 7) AS month, COUNT(*) AS count "
            "FROM tickets GROUP BY month ORDER BY month"
        ),
    ),
    (
        "approvals_by_team",
        (
            "SELECT owner_team, COUNT(*) AS count FROM approvals "
            "GROUP BY owner_team ORDER BY owner_team"
        ),
    ),
    (
        "approvals_by_status",
        (
            "SELECT status, COUNT(*) AS count FROM approvals "
            "GROUP BY status ORDER BY status"
        ),
    ),
    (
        "approval_amount_by_team",
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


def run(root: Path, *, print_fn=print) -> int:
    """Build and persist deterministic specifications and their summary."""
    root = root.resolve()
    try:
        seed = load_seed(root)
        docs = _load_docs(root / DOCS_PATH)
        db_manifest = _load_mapping(root / DB_MANIFEST_PATH)
        db_content_hash = db_manifest.get("content_hash")
        if not isinstance(db_content_hash, str) or not db_content_hash:
            raise SpecBuildError("synth_db manifest has no content_hash")
        rows = build_rows(seed)
        result = _build_specs_result(
            seed,
            docs,
            db_rows=rows,
            db_content_hash=db_content_hash,
            random_seed=int(seed.company["random_seed"]),
        )
        specs_path = root / SPECS_PATH
        summary_path = root / SUMMARY_PATH
        write_questions(specs_path, sorted(result.questions, key=lambda item: item.id))
        summary = _summary(root, result, docs, db_content_hash)
        _write_json(summary_path, summary)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print_fn(f"golden specs failed: {exc}")
        return 1
    print_fn(
        f"wrote {len(result.questions)} specifications to "
        f"{_display_path(specs_path, root)}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for ``python -m kodocs.eval.golden_specs``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    return run(args.root)


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
            (service,),
            (fact,),
            seed=seed,
            docs=docs,
            index=index,
            people=people,
            document_fact_ids=document_fact_ids,
        )
        if candidate is not None:
            semantic_by_fact[fact.fact_id] = candidate
    for (person_id, _), facts in sorted(person_semantic_groups.items()):
        if len(facts) != 1:
            rejected["semantic_ambiguous_subject_attribute"] += len(facts)
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

    pair_groups: dict[tuple[str, str], tuple[Fact, ...]] = {}
    for subject, facts in facts_by_subject.items():
        usable = tuple(fact for fact in facts if index.get(fact.fact_id))
        if len(usable) >= 2:
            pair_groups[("subject", subject)] = usable
    facts_by_service: dict[str, list[Fact]] = defaultdict(list)
    for fact in seed.facts.values():
        service = _service_for_subject(seed, fact.subject)
        if service is not None and index.get(fact.fact_id):
            facts_by_service[service].append(fact)
    for service, facts in facts_by_service.items():
        if len(facts) >= 2:
            pair_groups[("service", service)] = tuple(facts)

    seen_pairs: set[tuple[str, str]] = set()
    for (group_kind, group_value), facts in sorted(pair_groups.items()):
        for left, right in combinations(
            sorted(facts, key=lambda item: item.fact_id), 2
        ):
            pair = tuple(sorted((left.fact_id, right.fact_id)))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            if any(
                set(pair).issubset(doc_facts)
                for doc_facts in document_fact_ids.values()
            ):
                rejected["multi_doc_one_document_has_all_facts"] += 1
                continue
            anchor = (
                (_service_for_subject(seed, group_value) or group_value,)
                if group_kind == "subject"
                else (group_value,)
            )
            candidate = _fact_candidate(
                "multi_doc",
                anchor,
                (left, right),
                seed=seed,
                docs=docs,
                index=index,
                people=people,
                document_fact_ids=document_fact_ids,
            )
            if candidate is not None:
                pools["multi_doc"].append(candidate)

    temporal_groups: dict[tuple[str, str], list[Fact]] = defaultdict(list)
    replacement_groups: dict[tuple[str, str], list[Fact]] = defaultdict(list)
    for fact in seed.facts.values():
        service = _service_for_subject(seed, fact.subject)
        if service is None or fact.date is None or not index.get(fact.fact_id):
            continue
        if fact.kind == "text":
            temporal_groups[(service, _attribute(fact))].append(fact)
        elif fact.kind == "person":
            replacement_groups[(service, _attribute(fact))].append(fact)

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

    for (service, _), facts in sorted(replacement_groups.items()):
        ordered = sorted(facts, key=lambda item: (item.date, item.fact_id))
        for old, new in pairwise(ordered):
            if old.value == new.value or old.date is None or new.date is None:
                continue
            gap = (new.date - old.date).days
            if gap <= 1:
                rejected["temporal_replacement_has_no_between_date"] += 1
                continue
            as_of = old.date + timedelta(days=gap // 2)
            candidate = _fact_candidate(
                "temporal",
                (service,),
                (old,),
                seed=seed,
                docs=docs,
                index=index,
                people=people,
                document_fact_ids=document_fact_ids,
                tags=("temporal:as_of_event",),
                as_of=as_of.isoformat(),
                temporal_mode="replacement",
                temporal_candidate_count=len(ordered),
                discriminator=new.fact_id,
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
            db_rows.get("tickets", ()),
        )
        connection.executemany(
            "INSERT INTO approvals VALUES "
            "(:owner_team, :status, :submitted_at, :amount_krw)",
            db_rows.get("approvals", ()),
        )
        candidates: list[_Candidate] = []
        for name, query in _SQL_QUERIES:
            expected = [dict(row) for row in connection.execute(query).fetchall()]
            sql = SqlAnswer(
                query=query,
                query_sha256=hashlib.sha256(query.encode("utf-8")).hexdigest(),
                expected=expected,
                db_content_hash=db_content_hash,
            )
            compact = json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
            candidates.append(
                _Candidate(
                    type="sql",
                    anchor_values=(name,),
                    answer_values=(compact,),
                    answer_fact_ids=(),
                    qrels=(),
                    reference_answer=compact,
                    sql=sql,
                    discriminator=name,
                )
            )
        return candidates
    finally:
        connection.close()


def _take_candidates(
    set_name: str,
    question_type: str,
    candidates: Sequence[object],
    quota: int,
) -> list[object]:
    if len(candidates) < quota:
        raise SpecBuildError(
            f"candidate shortage: set={set_name} type={question_type} "
            f"candidates={len(candidates)} quota={quota}"
        )
    return list(candidates[:quota])


def _build_specs_result(
    seed: Seed,
    docs: Mapping[str, Mapping[str, object]],
    *,
    db_rows: Mapping[str, Sequence[Mapping[str, object]]],
    db_content_hash: str,
    random_seed: int,
) -> _BuildResult:
    index = fact_index(docs)
    pools, rejected = _candidate_pools(seed, docs, index)
    pools["sql"] = _sql_candidates(db_rows, db_content_hash)
    people = tuple(sorted(seed.people, key=lambda item: item.person_id))

    eligible: dict[str, list[_Candidate]] = {}
    for question_type, candidates in pools.items():
        accepted: list[_Candidate] = []
        for candidate in candidates:
            if _eligible_people(candidate, people, docs):
                accepted.append(candidate)
            else:
                rejected[f"{question_type}_no_eligible_asker"] += 1
        eligible[question_type] = accepted

    candidate_counts: dict[str, dict[str, int]] = {"gold": {}, "silver": {}}
    selected_gold: list[_Candidate] = []
    for question_type in QUESTION_TYPES:
        quota = GOLD_QUOTA.get((question_type, "synthetic"), 0)
        if not quota:
            continue
        available = eligible.get(question_type, [])
        candidate_counts["gold"][question_type] = len(available)
        selected_gold.extend(
            _take_candidates("gold", question_type, available, quota)  # type: ignore[arg-type]
        )

    gold_answer_facts = {
        fact_id for candidate in selected_gold for fact_id in candidate.answer_fact_ids
    }
    gold_keys = {candidate.key for candidate in selected_gold}
    selected_silver: list[_Candidate] = []
    for question_type, quota in SILVER_QUOTA.items():
        available = [
            candidate
            for candidate in eligible[question_type]
            if candidate.key not in gold_keys
            and gold_answer_facts.isdisjoint(candidate.answer_fact_ids)
        ]
        candidate_counts["silver"][question_type] = len(available)
        if question_type == "temporal":
            events = [c for c in available if c.temporal_mode == "event"]
            replacements = [c for c in available if c.temporal_mode == "replacement"]
            chosen = [
                *_take_candidates("silver", "temporal:event", events, quota - 1),
                *_take_candidates("silver", "temporal:replacement", replacements, 1),
            ]
        else:
            chosen = _take_candidates("silver", question_type, available, quota)
        selected_silver.extend(chosen)  # type: ignore[arg-type]

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
            questions.append(
                Question(
                    id=f"{prefix}-{set_number:03d}",
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
    return _BuildResult(questions, candidate_counts, rejected)


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
    return fact.fact_id.rsplit("-", 1)[-1]


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
