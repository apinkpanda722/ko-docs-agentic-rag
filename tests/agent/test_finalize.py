"""Tests for deterministic citation validation and refusal normalization."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kodocs.agent.evidence import EvidenceLog
from kodocs.agent.finalize import NO_ANSWER_TEXT, finalize_answer, finalizer_hash
from kodocs.agent.types import Citation, ModelAnswer
from kodocs.tools import ToolResult


def _record(
    log: EvidenceLog,
    name: str,
    payload: dict,
    refs: tuple[dict, ...],
    *,
    is_error: bool = False,
) -> str:
    return log.record(
        name,
        {},
        ToolResult(payload, is_error=is_error, refs=refs, timings=None),
    )


def test_citation_model_enforces_kind_specific_fields() -> None:
    assert Citation(kind="doc", doc_id="d1", section_key="s1").call_id is None
    assert Citation(kind="sql", call_id="c1").doc_id is None
    with pytest.raises(ValidationError):
        Citation(kind="doc", call_id="c1")
    with pytest.raises(ValidationError):
        Citation(kind="sql", doc_id="d1")


def test_unknown_doc_citation_is_dropped_but_valid_citation_remains() -> None:
    log = EvidenceLog()
    _record(
        log,
        "search_docs",
        {"results": []},
        ({"kind": "doc", "doc_id": "d1", "section_key": "s1"},),
    )
    model = ModelAnswer(
        answer="근거가 있는 답",
        citations=[
            Citation(kind="doc", doc_id="d1", section_key="s1"),
            Citation(kind="doc", doc_id="secret", section_key="x"),
        ],
        refused=False,
    )

    result = finalize_answer(model, log)

    assert result.refused is False
    assert result.answer == "근거가 있는 답"
    assert result.citations == (Citation(kind="doc", doc_id="d1", section_key="s1"),)
    assert result.trace["dropped_citations"] == [
        {"kind": "doc", "doc_id": "secret", "section_key": "x", "call_id": None}
    ]


def test_no_valid_citation_becomes_indistinguishable_refusal() -> None:
    result = finalize_answer(
        ModelAnswer(
            answer="권한이 없습니다.",
            citations=[Citation(kind="doc", doc_id="restricted")],
            refused=False,
        ),
        EvidenceLog(),
    )

    assert result.answer == NO_ANSWER_TEXT
    assert result.citations == ()
    assert result.refused is True
    assert result.refuse_reason == "no_valid_citation"


@pytest.mark.parametrize(
    ("model", "reason"),
    [
        (
            ModelAnswer(
                answer="모델별 거절 문장",
                citations=[Citation(kind="doc", doc_id="d1")],
                refused=True,
            ),
            None,
        ),
        (None, "parse_error"),
        (None, "guardrail_max_steps"),
        (None, "guardrail_tokens"),
        (None, "guardrail_timeout"),
    ],
)
def test_every_refusal_uses_one_constant_and_has_no_citations(
    model: ModelAnswer | None,
    reason: str | None,
) -> None:
    result = finalize_answer(model, EvidenceLog(), refuse_reason=reason)

    assert result.answer == NO_ANSWER_TEXT
    assert result.citations == ()
    assert result.refused is True
    assert result.refuse_reason == ("model_refused" if reason is None else reason)


def test_sql_citation_requires_a_successful_run_sql_call_id() -> None:
    log = EvidenceLog()
    successful = _record(
        log,
        "run_sql",
        {"columns": ["count"], "rows": [[3]]},
        ({"kind": "sql"},),
    )
    failed = _record(log, "run_sql", {"error": {}}, (), is_error=True)
    _record(log, "search_docs", {"results": []}, ())

    result = finalize_answer(
        ModelAnswer(
            answer="3건입니다.",
            citations=[
                Citation(kind="sql", call_id=successful),
                Citation(kind="sql", call_id=failed),
                Citation(kind="sql", call_id="c3"),
            ],
            refused=False,
        ),
        log,
    )

    assert result.refused is False
    assert result.citations == (Citation(kind="sql", call_id="c1"),)
    assert len(result.trace["dropped_citations"]) == 2


def _doc_log(section_key: str | None) -> EvidenceLog:
    log = EvidenceLog()
    _record(
        log,
        "search_docs",
        {"results": []},
        ({"kind": "doc", "doc_id": "syn-meeting-031", "section_key": section_key},),
    )
    return log


def _answer(section_key: str | None, doc_id: str = "syn-meeting-031") -> ModelAnswer:
    return ModelAnswer(
        answer="근거 있는 답",
        citations=[Citation(kind="doc", doc_id=doc_id, section_key=section_key)],
        refused=False,
    )


@pytest.mark.parametrize("model_key", ["-", "결정 사항", "", "  ", None])
def test_document_level_ref_accepts_any_model_section_key(
    model_key: str | None,
) -> None:
    result = finalize_answer(_answer(model_key), _doc_log(None))

    assert result.refused is False
    assert result.citations == (
        Citation(kind="doc", doc_id="syn-meeting-031", section_key=None),
    )
    assert result.trace["dropped_citations"] == []


@pytest.mark.parametrize("blank", ["", "-", "  ", " - "])
def test_blank_section_key_is_normalized_to_document_level(blank: str) -> None:
    result = finalize_answer(_answer(blank), _doc_log("제3조"))

    assert result.refused is False
    assert result.citations == (
        Citation(kind="doc", doc_id="syn-meeting-031", section_key=None),
    )


def test_sectioned_ref_keeps_the_existing_section_rules() -> None:
    log = _doc_log("제3조")

    kept = finalize_answer(_answer("제3조"), log)
    assert kept.citations == (
        Citation(kind="doc", doc_id="syn-meeting-031", section_key="제3조"),
    )
    wrong = finalize_answer(_answer("제9조"), log)
    assert wrong.refused is True
    assert wrong.refuse_reason == "no_valid_citation"
    assert wrong.trace["dropped_citations"] == [
        {
            "kind": "doc",
            "doc_id": "syn-meeting-031",
            "section_key": "제9조",
            "call_id": None,
        }
    ]


@pytest.mark.parametrize("section_key", ["-", "결정 사항", None, "제3조"])
@pytest.mark.parametrize("ref_key", [None, "제3조"])
def test_doc_id_missing_from_tool_results_is_always_dropped(
    section_key: str | None, ref_key: str | None
) -> None:
    result = finalize_answer(
        _answer(section_key, doc_id="other-doc"), _doc_log(ref_key)
    )

    assert result.refused is True
    assert result.refuse_reason == "no_valid_citation"
    assert len(result.trace["dropped_citations"]) == 1


def test_finalizer_hash_is_a_stable_sha256() -> None:
    value = finalizer_hash()

    assert value == finalizer_hash()
    assert len(value) == 64
    assert set(value) <= set("0123456789abcdef")
