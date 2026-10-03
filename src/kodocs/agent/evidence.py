"""Tool-evidence capture and fair final-context construction."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from threading import Lock

from kodocs.tools import ToolResult

# 1: run_sql evidence = {columns, rows} (M6/M7 evaluation runs).
# 2: run_sql evidence = {sql, columns, rows} (serving change after the demo
#    rehearsal; the M6/M7 numbers were measured with version 1). Not covered by
#    finalizer_hash, so it is recorded in every trace's ``hashes``.
EVIDENCE_FORMAT_VERSION = 2


@dataclass(frozen=True)
class EvidenceItem:
    """One labeled context item produced by a tool call."""

    label: str
    text: str


@dataclass(frozen=True)
class EvidenceCall:
    """Evidence and citation metadata for one numbered tool invocation."""

    call_id: str
    name: str
    args: dict[str, object]
    refs: tuple[dict, ...]
    items: tuple[EvidenceItem, ...]
    is_error: bool


class EvidenceLog:
    """Append-only per-question tool evidence log."""

    def __init__(self) -> None:
        self._calls: list[EvidenceCall] = []
        self._lock = Lock()

    @classmethod
    def from_calls(cls, calls: Sequence[EvidenceCall]) -> EvidenceLog:
        """Build a read-only-equivalent view over already captured calls."""
        log = cls()
        log._calls.extend(calls)
        return log

    @property
    def calls(self) -> tuple[EvidenceCall, ...]:
        with self._lock:
            return tuple(self._calls)

    def record(
        self,
        name: str,
        args: Mapping[str, object],
        result: ToolResult,
    ) -> str:
        """Record one call and return its stable local ID (``c1``, ``c2``...)."""
        with self._lock:
            call_id = f"c{len(self._calls) + 1}"
            items = (
                ()
                if result.is_error
                else _evidence_items(name, result.payload, call_id, args)
            )
            self._calls.append(
                EvidenceCall(
                    call_id=call_id,
                    name=name,
                    args=dict(args),
                    refs=tuple(dict(ref) for ref in result.refs),
                    items=items,
                    is_error=result.is_error,
                )
            )
        return call_id


def round_robin_context(log: EvidenceLog, budget: int = 6_000) -> str:
    """Fill a text-only budget fairly across calls, preserving result ranks."""
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
        raise ValueError("context budget must be a non-negative integer")
    if budget == 0:
        return ""

    ordered: list[EvidenceItem] = []
    maximum_items = max((len(call.items) for call in log.calls), default=0)
    for rank in range(maximum_items):
        for call in log.calls:
            if rank < len(call.items):
                ordered.append(call.items[rank])

    parts: list[str] = []
    used = 0
    for item in ordered:
        remaining = budget - used
        if remaining <= 0:
            break
        text = item.text[:remaining]
        parts.append(f"{item.label}\n{text}")
        used += len(text)
        if len(text) < len(item.text) or used == budget:
            break
    return "\n\n".join(parts)


def _evidence_items(
    name: str,
    payload: Mapping[str, object],
    call_id: str,
    args: Mapping[str, object] | None = None,
) -> tuple[EvidenceItem, ...]:
    if name == "search_docs":
        return tuple(
            _document_item(item, section=item.get("section_key"))
            for item in _mapping_items(payload.get("results"), "search results")
        )
    if name == "lookup_id":
        return tuple(
            _document_item(item, section=None)
            for item in _mapping_items(payload.get("documents"), "lookup documents")
        )
    if name == "get_document":
        sections = payload.get("sections")
        section = None
        if isinstance(sections, list) and len(sections) == 1:
            section = sections[0]
        return (_document_item(payload, section=section),)
    if name == "run_sql":
        # The final answer call only sees evidence text. Without the executed
        # statement it cannot tell that a number was filtered by owner_team, and
        # the "refuse when not verifiable" rule made runs flip between answer and
        # refusal. The SQL was written by the model, so it hides nothing.
        statement = (args or {}).get("sql")
        sql_text = json.dumps(
            {
                "sql": statement if isinstance(statement, str) else None,
                "columns": payload.get("columns", []),
                "rows": payload.get("rows", []),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return (EvidenceItem(label=f"[SQL {call_id}]", text=sql_text),)
    return ()


def _document_item(item: Mapping[str, object], *, section: object) -> EvidenceItem:
    doc_id = _display(item.get("doc_id"))
    title = _display(item.get("title"))
    section_key = _display(section)
    text = item.get("text")
    if not isinstance(text, str):
        raise TypeError("evidence text must be a string")
    return EvidenceItem(
        label=f"[{doc_id} | {section_key} | {title}]",
        text=text,
    )


def _mapping_items(value: object, label: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list):
        raise TypeError(f"{label} must be a list")
    if not all(isinstance(item, Mapping) for item in value):
        raise TypeError(f"{label} must contain objects")
    return tuple(value)


def _display(value: object) -> str:
    return value if isinstance(value, str) and value else "-"
