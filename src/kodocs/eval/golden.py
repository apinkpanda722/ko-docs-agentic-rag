"""Shared schema, persistence, and validation for M4 evaluation questions."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

QUESTION_TYPES = (
    "identifier",
    "semantic",
    "table",
    "multi_doc",
    "temporal",
    "permission",
    "unanswerable",
    "sql",
)
TAGS = (
    "temporal:revision",
    "temporal:as_of_event",
    "lexical_mismatch",
    "canary",
    "injection",
)
EXPECTED_RESPONSES = (
    "answer",
    "no_answer",
    "indistinguishable_from_no_answer",
)
GOLD_QUOTA = {
    ("identifier", "real"): 2,
    ("identifier", "synthetic"): 5,
    ("semantic", "real"): 4,
    ("semantic", "synthetic"): 5,
    ("table", "real"): 6,
    ("table", "synthetic"): 0,
    ("multi_doc", "real"): 2,
    ("multi_doc", "synthetic"): 6,
    ("temporal", "real"): 5,
    ("temporal", "synthetic"): 0,
    ("permission", "real"): 0,
    ("permission", "synthetic"): 5,
    ("unanswerable", "real"): 1,
    ("unanswerable", "synthetic"): 3,
    ("sql", "real"): 0,
    ("sql", "synthetic"): 6,
}
SILVER_QUOTA = {
    "identifier": 25,
    "semantic": 40,
    "multi_doc": 35,
    "temporal": 20,
    "permission": 15,
    "unanswerable": 15,
}
GOLD_SPLIT_QUOTA = {
    "test": {
        "identifier": 4,
        "semantic": 5,
        "table": 4,
        "multi_doc": 5,
        "temporal": 3,
        "permission": 3,
        "unanswerable": 2,
        "sql": 4,
    },
    "dev": {
        "identifier": 3,
        "semantic": 4,
        "table": 2,
        "multi_doc": 3,
        "temporal": 2,
        "permission": 2,
        "unanswerable": 2,
        "sql": 2,
    },
}
ATTENTION_CHECK_PREFIX = "chk-"

_LAW_DOC_ID_RE = re.compile(r"^law-(\d+)$")


@dataclass(frozen=True)
class Asker:
    team: str
    grade: int


@dataclass(frozen=True)
class Qrel:
    doc_id: str
    rel: int
    fact_id: str | None = None
    start: int | None = None
    end: int | None = None
    span_sha256: str | None = None
    article_key: str | None = None
    annex_id: str | None = None


@dataclass(frozen=True)
class SqlAnswer:
    query: str
    query_sha256: str
    expected: object
    db_content_hash: str


@dataclass(frozen=True)
class Question:
    id: str
    set: str
    source: str
    type: str
    tags: tuple[str, ...]
    question: str
    asker: Asker
    as_of: str | None
    qrels: tuple[Qrel, ...]
    anchor_values: tuple[str, ...]
    answer_values: tuple[str, ...]
    expected_response: str
    sql: SqlAnswer | None
    reference_answer: str | None
    reference_sha256: str | None
    summary80: str | None
    split: str | None


def question_from_dict(d: Mapping[str, object]) -> Question:
    """Build an immutable question from its JSON-compatible representation."""
    asker_value = _mapping(d["asker"], "asker")
    qrels_value = _sequence(d["qrels"], "qrels")
    sql_value = d.get("sql")
    sql: SqlAnswer | None = None
    if sql_value is not None:
        sql_data = _mapping(sql_value, "sql")
        sql = SqlAnswer(
            query=sql_data["query"],  # type: ignore[arg-type]
            query_sha256=sql_data["query_sha256"],  # type: ignore[arg-type]
            expected=sql_data["expected"],
            db_content_hash=sql_data["db_content_hash"],  # type: ignore[arg-type]
        )
    qrels = tuple(
        Qrel(
            doc_id=qrel["doc_id"],  # type: ignore[arg-type]
            rel=qrel["rel"],  # type: ignore[arg-type]
            fact_id=qrel.get("fact_id"),  # type: ignore[arg-type]
            start=qrel.get("start"),  # type: ignore[arg-type]
            end=qrel.get("end"),  # type: ignore[arg-type]
            span_sha256=qrel.get("span_sha256"),  # type: ignore[arg-type]
            article_key=qrel.get("article_key"),  # type: ignore[arg-type]
            annex_id=qrel.get("annex_id"),  # type: ignore[arg-type]
        )
        for qrel in (_mapping(item, "qrels item") for item in qrels_value)
    )
    return Question(
        id=d["id"],  # type: ignore[arg-type]
        set=d["set"],  # type: ignore[arg-type]
        source=d["source"],  # type: ignore[arg-type]
        type=d["type"],  # type: ignore[arg-type]
        tags=tuple(_sequence(d["tags"], "tags")),  # type: ignore[arg-type]
        question=d["question"],  # type: ignore[arg-type]
        asker=Asker(
            team=asker_value["team"],  # type: ignore[arg-type]
            grade=asker_value["grade"],  # type: ignore[arg-type]
        ),
        as_of=d.get("as_of"),  # type: ignore[arg-type]
        qrels=qrels,
        anchor_values=tuple(  # type: ignore[arg-type]
            _sequence(d["anchor_values"], "anchor_values")
        ),
        answer_values=tuple(  # type: ignore[arg-type]
            _sequence(d["answer_values"], "answer_values")
        ),
        expected_response=d["expected_response"],  # type: ignore[arg-type]
        sql=sql,
        reference_answer=d.get("reference_answer"),  # type: ignore[arg-type]
        reference_sha256=d.get("reference_sha256"),  # type: ignore[arg-type]
        summary80=d.get("summary80"),  # type: ignore[arg-type]
        split=d.get("split"),  # type: ignore[arg-type]
    )


def question_to_dict(q: Question) -> dict[str, object]:
    """Return a recursively key-sorted, JSON-compatible question mapping."""
    payload: dict[str, object] = {
        "id": q.id,
        "set": q.set,
        "source": q.source,
        "type": q.type,
        "tags": list(q.tags),
        "question": q.question,
        "asker": {"team": q.asker.team, "grade": q.asker.grade},
        "as_of": q.as_of,
        "qrels": [
            {
                "doc_id": qrel.doc_id,
                "rel": qrel.rel,
                "fact_id": qrel.fact_id,
                "start": qrel.start,
                "end": qrel.end,
                "span_sha256": qrel.span_sha256,
                "article_key": qrel.article_key,
                "annex_id": qrel.annex_id,
            }
            for qrel in q.qrels
        ],
        "anchor_values": list(q.anchor_values),
        "answer_values": list(q.answer_values),
        "expected_response": q.expected_response,
        "sql": (
            None
            if q.sql is None
            else {
                "query": q.sql.query,
                "query_sha256": q.sql.query_sha256,
                "expected": q.sql.expected,
                "db_content_hash": q.sql.db_content_hash,
            }
        ),
        "reference_answer": q.reference_answer,
        "reference_sha256": q.reference_sha256,
        "summary80": q.summary80,
        "split": q.split,
    }
    return _sorted_json_value(payload)  # type: ignore[return-value]


def load_questions(path: Path) -> list[Question]:
    """Load questions from a UTF-8 JSONL file."""
    questions: list[Question] = []
    with path.open(encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            payload = json.loads(line)
            questions.append(question_from_dict(_mapping(payload, "question")))
    return questions


def write_questions(path: Path, questions: Sequence[Question]) -> None:
    """Atomically write deterministically serialized questions as JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    with temporary.open("w", encoding="utf-8") as destination:
        for question in questions:
            destination.write(
                json.dumps(
                    question_to_dict(question),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            destination.write("\n")
    temporary.replace(path)


def span_sha256(text: str) -> str:
    """Return the lowercase SHA-256 digest of a UTF-8 text span."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def can_read(asker: Asker, access: Mapping[str, object]) -> bool:
    """Apply the synthetic-document access policy for one asker."""
    level = access.get("level")
    if level == "public":
        return True
    teams = access.get("teams")
    if not isinstance(teams, Sequence) or isinstance(teams, (str, bytes)):
        return False
    if asker.team not in teams:
        return False
    if level == "team":
        return True
    if level != "restricted":
        return False
    min_grade = access.get("min_grade")
    return (
        isinstance(min_grade, int)
        and not isinstance(min_grade, bool)
        and asker.grade >= min_grade
    )


def accessible_relevant_docs(
    q: Question, docs: Mapping[str, Mapping[str, object]]
) -> list[str]:
    """Return unique relevant document IDs readable by the question asker."""
    accessible: list[str] = []
    seen: set[str] = set()
    for qrel in q.qrels:
        if qrel.rel not in {1, 2} or qrel.doc_id in seen:
            continue
        document = docs.get(qrel.doc_id)
        if document is None:
            continue
        access = document.get("access")
        if isinstance(access, Mapping) and can_read(q.asker, access):
            accessible.append(qrel.doc_id)
            seen.add(qrel.doc_id)
    return accessible


def validate_question(
    q: Question,
    *,
    docs: Mapping[str, Mapping[str, object]],
    regulation_index: Mapping[str, object],
    table_ids: set[str],
) -> list[str]:
    """Return unique validation error codes in deterministic check order."""
    errors: list[str] = []

    def add(code: str) -> None:
        if code not in errors:
            errors.append(code)

    if q.type not in QUESTION_TYPES:
        add("unknown_type")
    if any(tag not in TAGS for tag in q.tags):
        add("unknown_tag")
    if q.expected_response not in EXPECTED_RESPONSES:
        add("unknown_expected_response")
    if q.id.startswith(ATTENTION_CHECK_PREFIX):
        add("attention_check_id")

    qrel_errors: set[str] = set()
    for qrel in q.qrels:
        if qrel.rel not in {1, 2}:
            qrel_errors.add("bad_rel")
        if q.source == "synthetic":
            document = docs.get(qrel.doc_id)
            if document is None:
                qrel_errors.add("unknown_doc")
            elif not _span_matches(qrel, document):
                qrel_errors.add("span_mismatch")
        elif q.source == "real":
            if not _article_exists(qrel, regulation_index):
                qrel_errors.add("unknown_article")
            if qrel.annex_id is not None and qrel.annex_id not in table_ids:
                qrel_errors.add("unknown_annex")
    for code in (
        "span_mismatch",
        "unknown_doc",
        "unknown_article",
        "unknown_annex",
        "bad_rel",
    ):
        if code in qrel_errors:
            add(code)

    has_relevant_qrel = any(qrel.rel == 2 for qrel in q.qrels)
    if q.expected_response == "answer" and not has_relevant_qrel and q.sql is None:
        add("empty_qrels")
    if q.expected_response == "no_answer" and q.qrels:
        add("nonempty_qrels_for_no_answer")
    if (
        q.expected_response == "indistinguishable_from_no_answer"
        and accessible_relevant_docs(q, docs)
    ):
        add("permission_accessible")
    if (
        q.expected_response == "answer"
        and q.source == "synthetic"
        and q.sql is None
        and not _accessible_rel_two_docs(q, docs)
    ):
        add("answer_inaccessible")

    if q.type == "temporal":
        temporal_tag_count = sum(
            q.tags.count(tag) for tag in ("temporal:revision", "temporal:as_of_event")
        )
        if temporal_tag_count != 1 or (
            "temporal:as_of_event" in q.tags and not q.as_of
        ):
            add("temporal_tag")

    if q.source == "real" and (
        q.reference_answer is not None
        or not isinstance(q.summary80, str)
        or not q.summary80
        or len(q.summary80) > 80
    ):
        add("real_text_fields")
    if q.type == "sql" and q.sql is None:
        add("sql_missing")
    return errors


def quota_report(
    questions: Sequence[Question], quota: Mapping[object, int]
) -> dict[object, dict[str, int]]:
    """Compare actual question counts with type or ``(type, source)`` quotas."""
    report: dict[object, dict[str, int]] = {}
    for key, expected in quota.items():
        if isinstance(key, tuple) and len(key) == 2:
            question_type, source = key
            actual = sum(
                question.type == question_type and question.source == source
                for question in questions
            )
        else:
            actual = sum(question.type == key for question in questions)
        report[key] = {
            "expected": expected,
            "actual": actual,
            "diff": actual - expected,
        }
    return report


def _span_matches(qrel: Qrel, document: Mapping[str, object]) -> bool:
    body = document.get("body")
    start = qrel.start
    end = qrel.end
    if (
        not isinstance(body, str)
        or not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or start < 0
        or end < start
        or end > len(body)
        or not isinstance(qrel.fact_id, str)
        or not isinstance(qrel.span_sha256, str)
    ):
        return False
    spans = document.get("fact_spans")
    if not isinstance(spans, Sequence) or isinstance(spans, (str, bytes)):
        return False
    matching_fact_span = any(
        isinstance(item, Mapping)
        and item.get("fact_id") == qrel.fact_id
        and item.get("start") == start
        and item.get("end") == end
        for item in spans
    )
    return matching_fact_span and span_sha256(body[start:end]) == qrel.span_sha256


def _article_exists(qrel: Qrel, regulation_index: Mapping[str, object]) -> bool:
    match = _LAW_DOC_ID_RE.fullmatch(qrel.doc_id)
    if match is None or not isinstance(qrel.article_key, str):
        return False
    rules = regulation_index.get("rules")
    if not isinstance(rules, Mapping):
        return False
    serial = match.group(1)
    for rule in rules.values():
        if not isinstance(rule, Mapping) or str(rule.get("serial")) != serial:
            continue
        articles = rule.get("articles")
        return isinstance(articles, Mapping) and qrel.article_key in articles
    return False


def _accessible_rel_two_docs(
    q: Question, docs: Mapping[str, Mapping[str, object]]
) -> list[str]:
    accessible: list[str] = []
    seen: set[str] = set()
    for qrel in q.qrels:
        if qrel.rel != 2 or qrel.doc_id in seen:
            continue
        document = docs.get(qrel.doc_id)
        if document is None:
            continue
        access = document.get("access")
        if isinstance(access, Mapping) and can_read(q.asker, access):
            accessible.append(qrel.doc_id)
            seen.add(qrel.doc_id)
    return accessible


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    return value


def _sequence(value: object, name: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError(f"{name} must be a sequence")
    return value


def _sorted_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _sorted_json_value(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_sorted_json_value(item) for item in value]
    return value
