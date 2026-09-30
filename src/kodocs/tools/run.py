"""One in-process dispatcher shared by agent and MCP tool entry points."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from pydantic import BaseModel, ValidationError

from kodocs.retrieval.access import UserContext
from kodocs.retrieval.document import authorized_get_document
from kodocs.retrieval.lookup import authorized_lookup_id
from kodocs.retrieval.search import authorized_search
from kodocs.retrieval.sql import DEFAULT_LIMIT, SqlRejected, authorized_run_sql
from kodocs.tools.contract import (
    INVALID_ARGUMENT,
    LIMIT_EXCEEDED,
    NOT_FOUND,
    TOOL_INPUT_MODELS,
    TOOL_OUTPUT_MODELS,
    ErrorCode,
    GetDocumentInput,
    LookupIdInput,
    RunSqlInput,
    SearchDocsInput,
    ToolName,
    error_payload,
)
from kodocs.tools.deps import (
    DOCUMENT_CHUNK_LIMIT,
    LOOKUP_DOCUMENT_LIMIT,
    ToolDeps,
)
from kodocs.tools.shape import (
    document_text,
    shape_document_result,
    shape_lookup_documents,
    shape_search_result,
    shape_sql_result,
)

_INVALID_ARGUMENT_MESSAGE = "도구 인자가 올바르지 않습니다."
_LIMIT_EXCEEDED_MESSAGE = "입력 길이 상한을 초과했습니다."
_NOT_FOUND_MESSAGE = "문서를 찾을 수 없습니다."


@dataclass(frozen=True)
class ToolResult:
    """Stable tool payload plus non-payload evidence and timing metadata."""

    payload: dict
    is_error: bool
    refs: tuple[dict, ...]
    timings: dict[str, float | None] | None


def run_tool(
    name: str,
    args: Mapping[str, object],
    ctx: UserContext,
    deps: ToolDeps,
) -> ToolResult:
    """Validate model arguments and call one authorization-aware retrieval path."""
    if name not in TOOL_INPUT_MODELS:
        return _error(INVALID_ARGUMENT, _INVALID_ARGUMENT_MESSAGE)
    tool_name = cast(ToolName, name)
    try:
        parsed = TOOL_INPUT_MODELS[tool_name].model_validate(args)
    except ValidationError as exc:
        if any(error["type"] == "string_too_long" for error in exc.errors()):
            return _error(LIMIT_EXCEEDED, _LIMIT_EXCEEDED_MESSAGE)
        return _error(INVALID_ARGUMENT, _INVALID_ARGUMENT_MESSAGE)

    if tool_name == "search_docs":
        return _run_search(cast(SearchDocsInput, parsed), ctx, deps)
    if tool_name == "lookup_id":
        return _run_lookup(cast(LookupIdInput, parsed), ctx, deps)
    if tool_name == "run_sql":
        return _run_sql(cast(RunSqlInput, parsed), ctx, deps)
    return _run_get_document(cast(GetDocumentInput, parsed), ctx, deps)


def _run_search(
    args: SearchDocsInput,
    ctx: UserContext,
    deps: ToolDeps,
) -> ToolResult:
    result = authorized_search(
        deps.client,
        deps.index,
        ctx,
        args.query,
        deps.search_params,
        deps.embedder,
        prefer_current=deps.prefer_current,
    )
    payload, refs = shape_search_result(result)
    return ToolResult(
        payload=_validated_output("search_docs", payload),
        is_error=False,
        refs=refs,
        timings={"embed_ms": result.embed_ms, "search_ms": result.search_ms},
    )


def _run_lookup(
    args: LookupIdInput,
    ctx: UserContext,
    deps: ToolDeps,
) -> ToolResult:
    hits = authorized_lookup_id(
        deps.client,
        deps.index,
        ctx,
        args.kind,
        args.value,
        size=LOOKUP_DOCUMENT_LIMIT + 1,
        prefer_current=deps.prefer_current,
    )
    documents: list[dict[str, object]] = []
    for hit in hits[:LOOKUP_DOCUMENT_LIMIT]:
        document = authorized_get_document(
            deps.client,
            deps.index,
            ctx,
            hit.doc_id,
            size=DOCUMENT_CHUNK_LIMIT,
        )
        documents.append(
            {
                "doc_id": hit.doc_id,
                "title": hit.title,
                "is_current": hit.is_current,
                "date": getattr(hit, "date", None),
                "text": "" if document is None else document_text(document),
            }
        )
    payload, refs = shape_lookup_documents(
        documents,
        source_truncated=len(hits) > LOOKUP_DOCUMENT_LIMIT,
    )
    return ToolResult(
        payload=_validated_output("lookup_id", payload),
        is_error=False,
        refs=refs,
        timings=None,
    )


def _run_sql(
    args: RunSqlInput,
    ctx: UserContext,
    deps: ToolDeps,
) -> ToolResult:
    try:
        result = authorized_run_sql(
            deps.db_path,
            ctx,
            args.sql,
            limit=DEFAULT_LIMIT,
        )
    except SqlRejected as exc:
        return _error(INVALID_ARGUMENT, str(exc))
    payload = shape_sql_result(result)
    return ToolResult(
        payload=_validated_output("run_sql", payload),
        is_error=False,
        refs=({"kind": "sql"},),
        timings=None,
    )


def _run_get_document(
    args: GetDocumentInput,
    ctx: UserContext,
    deps: ToolDeps,
) -> ToolResult:
    result = authorized_get_document(
        deps.client,
        deps.index,
        ctx,
        args.doc_id,
        section_key=args.section_key,
        size=DOCUMENT_CHUNK_LIMIT,
    )
    if result is None:
        return _error(NOT_FOUND, _NOT_FOUND_MESSAGE)
    payload, refs = shape_document_result(result)
    return ToolResult(
        payload=_validated_output("get_document", payload),
        is_error=False,
        refs=refs,
        timings=None,
    )


def _validated_output(name: ToolName, payload: dict[str, object]) -> dict:
    output: BaseModel = TOOL_OUTPUT_MODELS[name].model_validate(payload)
    return output.model_dump(mode="json")


def _error(code: ErrorCode, message: str) -> ToolResult:
    return ToolResult(
        payload=error_payload(code, message),
        is_error=True,
        refs=(),
        timings=None,
    )
