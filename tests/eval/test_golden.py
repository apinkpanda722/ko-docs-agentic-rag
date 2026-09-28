"""Shared golden-question schema and validator tests."""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path

import pytest

from kodocs.eval.golden import (
    ATTENTION_CHECK_PREFIX,
    EXPECTED_RESPONSES,
    GOLD_QUOTA,
    GOLD_SPLIT_QUOTA,
    QUESTION_TYPES,
    SILVER_QUOTA,
    TAGS,
    Asker,
    Qrel,
    Question,
    SqlAnswer,
    accessible_relevant_docs,
    can_read,
    load_questions,
    question_from_dict,
    question_to_dict,
    quota_report,
    span_sha256,
    validate_question,
    write_questions,
)

ROOT = Path(__file__).resolve().parents[2]
BODY = "가짜 사실이 담긴 짧은 문서"
SPAN = "가짜 사실"


@pytest.fixture
def docs(tmp_path: Path) -> dict[str, dict[str, object]]:
    path = tmp_path / "docs.json"
    path.write_text(
        json.dumps(
            {
                "syn-doc-001": {
                    "access": {"level": "public", "teams": [], "min_grade": 1},
                    "body": BODY,
                    "fact_spans": [
                        {"fact_id": "f-test-001", "start": 0, "end": len(SPAN)}
                    ],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def regulation_index(tmp_path: Path) -> dict[str, object]:
    path = tmp_path / "regulation_refs.json"
    path.write_text(
        json.dumps(
            {
                "rules": {
                    "가상 복무규정": {
                        "rule_id": "rule-test",
                        "serial": "123",
                        "articles": {"제1조": "가짜 목적"},
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def table_ids(tmp_path: Path) -> set[str]:
    path = tmp_path / "table_golden.json"
    path.write_text(json.dumps({"tables": [{"id": "123_0-00"}]}), encoding="utf-8")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {item["id"] for item in payload["tables"]}


def _synthetic_question(**changes: object) -> Question:
    question = Question(
        id="gold-semantic-001",
        set="gold",
        source="synthetic",
        type="semantic",
        tags=(),
        question="가짜 문서의 핵심 사실은 무엇인가?",
        asker=Asker(team="platform", grade=3),
        as_of=None,
        qrels=(
            Qrel(
                doc_id="syn-doc-001",
                rel=2,
                fact_id="f-test-001",
                start=0,
                end=len(SPAN),
                span_sha256=span_sha256(SPAN),
            ),
        ),
        anchor_values=("가짜",),
        answer_values=("사실",),
        expected_response="answer",
        sql=None,
        reference_answer="가짜 사실이다.",
        reference_sha256=None,
        summary80=None,
        split=None,
    )
    return replace(question, **changes)


def _real_question(**changes: object) -> Question:
    question = Question(
        id="gold-table-001",
        set="gold",
        source="real",
        type="table",
        tags=(),
        question="가짜 표의 항목은 무엇인가?",
        asker=Asker(team="platform", grade=3),
        as_of=None,
        qrels=(
            Qrel(
                doc_id="law-123",
                rel=2,
                article_key="제1조",
                annex_id="123_0-00",
            ),
        ),
        anchor_values=("가짜 표",),
        answer_values=("가짜 항목",),
        expected_response="answer",
        sql=None,
        reference_answer=None,
        reference_sha256="a" * 64,
        summary80="가짜 표에 있는 항목의 짧은 요지",
        split="test",
    )
    return replace(question, **changes)


def _errors(
    question: Question,
    *,
    docs: dict[str, dict[str, object]],
    regulation_index: dict[str, object],
    table_ids: set[str],
) -> list[str]:
    return validate_question(
        question,
        docs=docs,
        regulation_index=regulation_index,
        table_ids=table_ids,
    )


def test_quota_constants_match_adr_025() -> None:
    assert QUESTION_TYPES == (
        "identifier",
        "semantic",
        "table",
        "multi_doc",
        "temporal",
        "permission",
        "unanswerable",
        "sql",
    )
    assert TAGS == (
        "temporal:revision",
        "temporal:as_of_event",
        "lexical_mismatch",
        "canary",
        "injection",
    )
    assert EXPECTED_RESPONSES == (
        "answer",
        "no_answer",
        "indistinguishable_from_no_answer",
    )
    assert GOLD_QUOTA == {
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
    assert SILVER_QUOTA == {
        "identifier": 25,
        "semantic": 40,
        "multi_doc": 35,
        "temporal": 20,
        "permission": 15,
        "unanswerable": 15,
    }
    assert GOLD_SPLIT_QUOTA == {
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


def test_question_jsonl_round_trip_is_deterministic(tmp_path: Path) -> None:
    sql = SqlAnswer(
        query="SELECT COUNT(*) FROM tickets",
        query_sha256="b" * 64,
        expected={"count": 3, "groups": ["a", "b"]},
        db_content_hash="c" * 64,
    )
    question = _synthetic_question(sql=sql)
    payload = question_to_dict(question)

    assert list(payload) == sorted(payload)
    assert list(payload["asker"]) == sorted(payload["asker"])
    assert question_from_dict(payload) == question

    path = tmp_path / "nested" / "questions.jsonl"
    write_questions(path, [question, _real_question()])
    first = path.read_bytes()
    write_questions(path, [question, _real_question()])

    assert path.read_bytes() == first
    assert load_questions(path) == [question, _real_question()]
    assert not path.with_suffix(".jsonl.tmp").exists()
    assert all(
        list(json.loads(line)) == sorted(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
    )


def test_span_sha256_hashes_utf8_text() -> None:
    assert span_sha256("가짜 사실") == (
        "7f3f3286c897dc753861bf005d170f4b57c863a1ad95e984572af38373f68f50"
    )


@pytest.mark.parametrize(
    ("access", "expected"),
    [
        ({"level": "public", "teams": [], "min_grade": 1}, True),
        ({"level": "team", "teams": ["platform"], "min_grade": 1}, True),
        ({"level": "team", "teams": ["payments"], "min_grade": 1}, False),
        (
            {"level": "restricted", "teams": ["platform"], "min_grade": 4},
            False,
        ),
    ],
)
def test_can_read_applies_access_policy(
    access: dict[str, object], expected: bool
) -> None:
    assert can_read(Asker(team="platform", grade=3), access) is expected


def test_accessible_relevant_docs_returns_unique_readable_docs(
    docs: dict[str, dict[str, object]],
) -> None:
    question = _synthetic_question(
        qrels=(
            *_synthetic_question().qrels,
            replace(_synthetic_question().qrels[0], rel=1),
        )
    )

    assert accessible_relevant_docs(question, docs) == ["syn-doc-001"]


@pytest.mark.parametrize(
    ("question", "code"),
    [
        (_synthetic_question(type="other"), "unknown_type"),
        (_synthetic_question(tags=("other",)), "unknown_tag"),
        (
            _synthetic_question(expected_response="other"),
            "unknown_expected_response",
        ),
        (_synthetic_question(id="chk-deliberately-wrong"), "attention_check_id"),
        (
            _synthetic_question(
                qrels=(replace(_synthetic_question().qrels[0], rel=3),)
            ),
            "bad_rel",
        ),
        (_synthetic_question(qrels=()), "empty_qrels"),
        (
            _synthetic_question(
                expected_response="no_answer",
                reference_answer=None,
            ),
            "nonempty_qrels_for_no_answer",
        ),
        (
            _synthetic_question(expected_response="indistinguishable_from_no_answer"),
            "permission_accessible",
        ),
        (_synthetic_question(type="temporal"), "temporal_tag"),
        (
            _synthetic_question(type="temporal", tags=("temporal:as_of_event",)),
            "temporal_tag",
        ),
        (_real_question(reference_answer="원문을 인용한 답"), "real_text_fields"),
        (_real_question(summary80="가" * 81), "real_text_fields"),
        (_synthetic_question(type="sql"), "sql_missing"),
    ],
)
def test_validate_question_reports_scalar_contract_errors(
    question: Question,
    code: str,
    docs: dict[str, dict[str, object]],
    regulation_index: dict[str, object],
    table_ids: set[str],
) -> None:
    assert code in _errors(
        question,
        docs=docs,
        regulation_index=regulation_index,
        table_ids=table_ids,
    )


@pytest.mark.parametrize("mismatch", ["hash", "fact_span"])
def test_validate_question_reports_span_mismatch(
    mismatch: str,
    docs: dict[str, dict[str, object]],
    regulation_index: dict[str, object],
    table_ids: set[str],
) -> None:
    qrel = _synthetic_question().qrels[0]
    if mismatch == "hash":
        qrel = replace(qrel, span_sha256="0" * 64)
    else:
        qrel = replace(qrel, fact_id="f-test-missing")

    assert "span_mismatch" in _errors(
        _synthetic_question(qrels=(qrel,)),
        docs=docs,
        regulation_index=regulation_index,
        table_ids=table_ids,
    )


def test_validate_question_reports_unknown_doc(
    docs: dict[str, dict[str, object]],
    regulation_index: dict[str, object],
    table_ids: set[str],
) -> None:
    qrel = replace(_synthetic_question().qrels[0], doc_id="syn-doc-missing")

    assert "unknown_doc" in _errors(
        _synthetic_question(qrels=(qrel,)),
        docs=docs,
        regulation_index=regulation_index,
        table_ids=table_ids,
    )


@pytest.mark.parametrize(
    "qrel",
    [
        replace(_real_question().qrels[0], doc_id="not-a-law-id"),
        replace(_real_question().qrels[0], doc_id="law-999"),
        replace(_real_question().qrels[0], article_key="제99조"),
    ],
)
def test_validate_question_reports_unknown_article(
    qrel: Qrel,
    docs: dict[str, dict[str, object]],
    regulation_index: dict[str, object],
    table_ids: set[str],
) -> None:
    assert "unknown_article" in _errors(
        _real_question(qrels=(qrel,)),
        docs=docs,
        regulation_index=regulation_index,
        table_ids=table_ids,
    )


def test_validate_question_reports_unknown_annex(
    docs: dict[str, dict[str, object]],
    regulation_index: dict[str, object],
    table_ids: set[str],
) -> None:
    qrel = replace(_real_question().qrels[0], annex_id="missing-table")

    assert "unknown_annex" in _errors(
        _real_question(qrels=(qrel,)),
        docs=docs,
        regulation_index=regulation_index,
        table_ids=table_ids,
    )


def test_validate_question_reports_answer_inaccessible(
    docs: dict[str, dict[str, object]],
    regulation_index: dict[str, object],
    table_ids: set[str],
) -> None:
    docs["syn-doc-001"]["access"] = {
        "level": "team",
        "teams": ["payments"],
        "min_grade": 1,
    }

    assert "answer_inaccessible" in _errors(
        _synthetic_question(),
        docs=docs,
        regulation_index=regulation_index,
        table_ids=table_ids,
    )


def test_valid_questions_and_sql_question_have_no_errors(
    docs: dict[str, dict[str, object]],
    regulation_index: dict[str, object],
    table_ids: set[str],
) -> None:
    sql = SqlAnswer(
        query="SELECT COUNT(*) FROM tickets",
        query_sha256="b" * 64,
        expected=3,
        db_content_hash="c" * 64,
    )
    sql_question = _synthetic_question(type="sql", qrels=(), sql=sql)

    assert not _errors(
        _synthetic_question(),
        docs=docs,
        regulation_index=regulation_index,
        table_ids=table_ids,
    )
    assert not _errors(
        _real_question(),
        docs=docs,
        regulation_index=regulation_index,
        table_ids=table_ids,
    )
    assert not _errors(
        sql_question,
        docs=docs,
        regulation_index=regulation_index,
        table_ids=table_ids,
    )


def test_quota_report_supports_source_and_type_quotas() -> None:
    questions = [_synthetic_question(), _synthetic_question(type="identifier")]

    assert quota_report(
        questions,
        {("semantic", "synthetic"): 2, ("identifier", "real"): 1},
    ) == {
        ("semantic", "synthetic"): {"expected": 2, "actual": 1, "diff": -1},
        ("identifier", "real"): {"expected": 1, "actual": 0, "diff": -1},
    }
    assert quota_report(questions, {"semantic": 1, "identifier": 2}) == {
        "semantic": {"expected": 1, "actual": 1, "diff": 0},
        "identifier": {"expected": 2, "actual": 1, "diff": -1},
    }


SYNTHETIC_QUESTION_FILES = ("specs.jsonl", "validated.jsonl", "real_projection.jsonl")


def test_committed_real_jsonl_never_contains_reference_text() -> None:
    # Only question files: data/synthetic/queries also holds append-only logs
    # (draft_log.jsonl, draft_failures.jsonl) whose rows are not questions.
    paths = sorted((ROOT / "data/golden/queries").glob("*.jsonl")) + [
        ROOT / "data/synthetic/queries" / name for name in SYNTHETIC_QUESTION_FILES
    ]
    for path in paths:
        if not path.exists():
            continue
        for question in load_questions(path):
            if question.source == "real":
                assert question.reference_answer is None, path
                assert question.summary80 is not None, path
                assert len(question.summary80) <= 80, path


def test_committed_query_files_never_contain_attention_check_ids() -> None:
    for directory in (ROOT / "data/golden/queries", ROOT / "data/synthetic/queries"):
        for path in sorted(directory.rglob("*")) if directory.is_dir() else ():
            if not path.is_file():
                continue
            if path.suffix == ".jsonl":
                values = [
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            elif path.suffix == ".json":
                values = [json.loads(path.read_text(encoding="utf-8"))]
            elif path.suffix == ".csv":
                with path.open(encoding="utf-8", newline="") as source:
                    values = list(csv.DictReader(source))
            else:
                continue
            assert not _has_attention_check_id(values), path


def _has_attention_check_id(value: object) -> bool:
    if isinstance(value, str):
        return value.startswith(ATTENTION_CHECK_PREFIX)
    if isinstance(value, dict):
        return any(
            _has_attention_check_id(key) or _has_attention_check_id(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_has_attention_check_id(item) for item in value)
    return False


@pytest.mark.parametrize(
    ("question_type", "expected_response"),
    [
        ("permission", "answer"),
        ("permission", "no_answer"),
        ("unanswerable", "answer"),
        ("unanswerable", "indistinguishable_from_no_answer"),
        ("semantic", "no_answer"),
        ("identifier", "indistinguishable_from_no_answer"),
    ],
)
def test_validate_question_reports_type_response_mismatch(
    question_type: str,
    expected_response: str,
    docs: dict[str, dict[str, object]],
    regulation_index: dict[str, object],
    table_ids: set[str],
) -> None:
    question = _synthetic_question(
        type=question_type, expected_response=expected_response
    )

    assert "type_response_mismatch" in _errors(
        question,
        docs=docs,
        regulation_index=regulation_index,
        table_ids=table_ids,
    )


def _permission_question(**changes: object) -> Question:
    return _synthetic_question(
        type="permission",
        expected_response="indistinguishable_from_no_answer",
        asker=Asker(team="platform", grade=3),
        **changes,
    )


def _restrict(docs: dict[str, dict[str, object]]) -> None:
    docs["syn-doc-001"]["access"] = {
        "level": "restricted",
        "teams": ["security"],
        "min_grade": 4,
    }


def test_permission_question_with_hidden_restricted_answer_is_valid(
    docs: dict[str, dict[str, object]],
    regulation_index: dict[str, object],
    table_ids: set[str],
) -> None:
    _restrict(docs)

    assert not _errors(
        _permission_question(),
        docs=docs,
        regulation_index=regulation_index,
        table_ids=table_ids,
    )


@pytest.mark.parametrize("rel", [None, 1])
def test_validate_question_reports_permission_empty_qrels(
    rel: int | None,
    docs: dict[str, dict[str, object]],
    regulation_index: dict[str, object],
    table_ids: set[str],
) -> None:
    # M7 leak criterion (a) needs the restricted doc IDs a permission item hides.
    _restrict(docs)
    qrels = () if rel is None else (replace(_synthetic_question().qrels[0], rel=rel),)

    assert "permission_empty_qrels" in _errors(
        _permission_question(qrels=qrels),
        docs=docs,
        regulation_index=regulation_index,
        table_ids=table_ids,
    )


def test_spec_fields_survive_jsonl_round_trip(tmp_path: Path) -> None:
    # Step 2 writes these on specs; later steps (packets, promote) read them back.
    question = _synthetic_question(
        type="unanswerable",
        expected_response="no_answer",
        qrels=(),
        reference_answer=None,
        unmarked_candidates=("syn-doc-002", "syn-doc-003"),
        no_answer_checks={"seed": True, "docs": True, "real": None},
    )
    path = tmp_path / "specs.jsonl"

    write_questions(path, [question])
    [loaded] = load_questions(path)
    row = json.loads(path.read_text(encoding="utf-8"))

    assert loaded == question
    assert row["unmarked_candidates"] == ["syn-doc-002", "syn-doc-003"]
    assert row["no_answer_checks"] == {"docs": True, "real": None, "seed": True}


def test_spec_fields_default_when_absent_from_older_rows() -> None:
    row = question_to_dict(_synthetic_question())
    del row["unmarked_candidates"], row["no_answer_checks"]

    loaded = question_from_dict(row)

    assert loaded.unmarked_candidates == ()
    assert loaded.no_answer_checks is None
