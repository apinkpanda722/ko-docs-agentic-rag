"""Tests for evidence capture and final-context allocation."""

from __future__ import annotations

from kodocs.agent.evidence import EvidenceLog, round_robin_context
from kodocs.tools import ToolResult


def _result(payload: dict, refs: tuple[dict, ...]) -> ToolResult:
    return ToolResult(payload=payload, is_error=False, refs=refs, timings=None)


def test_round_robin_uses_call_order_then_rank_within_each_call() -> None:
    log = EvidenceLog()
    log.record(
        "search_docs",
        {"query": "첫 검색"},
        _result(
            {
                "results": [
                    {
                        "doc_id": "d1",
                        "section_key": "s1",
                        "title": "문서 1",
                        "text": "A1",
                    },
                    {
                        "doc_id": "d2",
                        "section_key": "s2",
                        "title": "문서 2",
                        "text": "A2",
                    },
                    {
                        "doc_id": "d3",
                        "section_key": "s3",
                        "title": "문서 3",
                        "text": "A3",
                    },
                ]
            },
            (
                {"kind": "doc", "doc_id": "d1", "section_key": "s1"},
                {"kind": "doc", "doc_id": "d2", "section_key": "s2"},
                {"kind": "doc", "doc_id": "d3", "section_key": "s3"},
            ),
        ),
    )
    log.record(
        "lookup_id",
        {"kind": "ticket", "value": "PAY-1"},
        _result(
            {
                "documents": [
                    {"doc_id": "d4", "title": "문서 4", "text": "B1"},
                    {"doc_id": "d5", "title": "문서 5", "text": "B2"},
                ]
            },
            (
                {"kind": "doc", "doc_id": "d4", "section_key": None},
                {"kind": "doc", "doc_id": "d5", "section_key": None},
            ),
        ),
    )

    context = round_robin_context(log)

    assert [context.index(value) for value in ("A1", "B1", "A2", "B2", "A3")] == sorted(
        context.index(value) for value in ("A1", "B1", "A2", "B2", "A3")
    )
    assert "[d1 | s1 | 문서 1]" in context
    assert "[d4 | - | 문서 4]" in context


def test_budget_counts_only_text_and_truncates_the_last_item() -> None:
    log = EvidenceLog()
    log.record(
        "search_docs",
        {"query": "검색"},
        _result(
            {
                "results": [
                    {
                        "doc_id": "very-long-document-id",
                        "section_key": "very-long-section",
                        "title": "긴 제목",
                        "text": "가나다",
                    },
                    {
                        "doc_id": "d2",
                        "section_key": "s2",
                        "title": "문서 2",
                        "text": "라마바",
                    },
                ]
            },
            (),
        ),
    )

    context = round_robin_context(log, budget=5)

    assert "가나다" in context
    assert "라마" in context
    assert "라마바" not in context
    assert "[very-long-document-id | very-long-section | 긴 제목]" in context


def test_single_call_keeps_original_item_order() -> None:
    log = EvidenceLog()
    log.record(
        "run_sql",
        {"sql": "SELECT 1"},
        _result(
            {"columns": ["value"], "rows": [[1], [2]], "truncated": False},
            ({"kind": "sql"},),
        ),
    )
    log.record(
        "get_document",
        {"doc_id": "doc-1"},
        _result(
            {
                "doc_id": "doc-1",
                "title": "문서",
                "sections": ["s1"],
                "text": "본문",
                "truncated": False,
            },
            ({"kind": "doc", "doc_id": "doc-1", "section_key": "s1"},),
        ),
    )

    assert log.calls[0].call_id == "c1"
    assert log.calls[0].items[0].label == "[SQL c1]"
    assert '"columns":["value"]' in log.calls[0].items[0].text
    assert round_robin_context(EvidenceLog.from_calls((log.calls[1],))).endswith("본문")
