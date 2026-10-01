"""Tests for deterministic citation validation and refusal normalization."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kodocs.agent.evidence import EvidenceLog
from kodocs.agent.finalize import NO_ANSWER_TEXT, finalize_answer
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
