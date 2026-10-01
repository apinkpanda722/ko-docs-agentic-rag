"""Deterministic validation of model-proposed evidence citations."""

from __future__ import annotations

from collections.abc import Iterable

from kodocs.agent.evidence import EvidenceLog
from kodocs.agent.types import Citation


def _normalize_section_key(value: str | None) -> str | None:
    """Treat blank placeholders ("", "-", whitespace) as "no section"."""
    if value is None:
        return None
    stripped = value.strip()
    return None if stripped in {"", "-"} else stripped


def validate_citations(
    citations: Iterable[Citation],
    log: EvidenceLog,
) -> tuple[tuple[Citation, ...], tuple[Citation, ...]]:
    """Partition citations by whether captured evidence proves their origin.

    A document-level ref (no ``section_key``, e.g. synthetic documents the model sees
    labeled ``[doc | - | title]``) accepts any citation of that ``doc_id`` as a
    document-level citation. A sectioned ref accepts a document-level citation or the
    same section. Doc IDs absent from the tool results are always dropped.
    """
    doc_refs: dict[str, set[str | None]] = {}
    for call in log.calls:
        if call.is_error:
            continue
        for ref in call.refs:
            doc_id = ref.get("doc_id")
            if ref.get("kind") == "doc" and isinstance(doc_id, str):
                raw_key = ref.get("section_key")
                key = _normalize_section_key(
                    raw_key if isinstance(raw_key, str) else None
                )
                doc_refs.setdefault(doc_id, set()).add(key)
    sql_calls = {
        call.call_id
        for call in log.calls
        if call.name == "run_sql" and not call.is_error
    }

    valid: list[Citation] = []
    dropped: list[Citation] = []
    for citation in citations:
        accepted: Citation | None = None
        if citation.kind == "doc":
            ref_keys = doc_refs.get(citation.doc_id or "")
            cited_key = _normalize_section_key(citation.section_key)
            if ref_keys is not None:
                if None in ref_keys or cited_key is None:
                    accepted = citation.model_copy(update={"section_key": None})
                elif cited_key in ref_keys:
                    accepted = citation.model_copy(update={"section_key": cited_key})
        elif citation.call_id in sql_calls:
            accepted = citation
        if accepted is None:
            dropped.append(citation)
        else:
            valid.append(accepted)
    return tuple(valid), tuple(dropped)
