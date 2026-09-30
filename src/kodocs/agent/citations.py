"""Deterministic validation of model-proposed evidence citations."""

from __future__ import annotations

from collections.abc import Iterable

from kodocs.agent.evidence import EvidenceLog
from kodocs.agent.types import Citation


def validate_citations(
    citations: Iterable[Citation],
    log: EvidenceLog,
) -> tuple[tuple[Citation, ...], tuple[Citation, ...]]:
    """Partition citations by whether captured evidence proves their origin."""
    doc_refs = {
        (ref.get("doc_id"), ref.get("section_key"))
        for call in log.calls
        if not call.is_error
        for ref in call.refs
        if ref.get("kind") == "doc" and isinstance(ref.get("doc_id"), str)
    }
    sql_calls = {
        call.call_id
        for call in log.calls
        if call.name == "run_sql" and not call.is_error
    }

    valid: list[Citation] = []
    dropped: list[Citation] = []
    for citation in citations:
        accepted = False
        if citation.kind == "doc":
            accepted = any(
                citation.doc_id == doc_id
                and (
                    citation.section_key is None or citation.section_key == section_key
                )
                for doc_id, section_key in doc_refs
            )
        elif citation.call_id in sql_calls:
            accepted = True
        (valid if accepted else dropped).append(citation)
    return tuple(valid), tuple(dropped)
