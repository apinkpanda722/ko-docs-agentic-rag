"""Versioned schemas, descriptions, and errors for the shared tool contract."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kodocs.retrieval.sql import ALLOWED_TABLES

type ToolName = Literal[
    "search_docs",
    "lookup_id",
    "run_sql",
    "get_document",
]
type ErrorCode = Literal[
    "unauthorized",
    "not_found",
    "invalid_argument",
    "limit_exceeded",
]

UNAUTHORIZED: ErrorCode = "unauthorized"
NOT_FOUND: ErrorCode = "not_found"
INVALID_ARGUMENT: ErrorCode = "invalid_argument"
LIMIT_EXCEEDED: ErrorCode = "limit_exceeded"
ERROR_CODES: tuple[ErrorCode, ...] = (
    UNAUTHORIZED,
    NOT_FOUND,
    INVALID_ARGUMENT,
    LIMIT_EXCEEDED,
)

TOOL_VERSIONS: dict[ToolName, int] = {
    "search_docs": 1,
    "lookup_id": 1,
    "run_sql": 1,
    "get_document": 1,
}

QUERY_MAX_LENGTH = 500
SQL_MAX_LENGTH = 2_000
IDENTIFIER_MAX_LENGTH = 100
DOCUMENT_ID_MAX_LENGTH = 100
SECTION_KEY_MAX_LENGTH = 100


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _require_non_whitespace(value: str) -> str:
    if not value.strip():
        raise ValueError("value must contain a non-whitespace character")
    return value


class SearchDocsInput(_ContractModel):
    query: str = Field(
        min_length=1,
        max_length=QUERY_MAX_LENGTH,
        description="검색할 한국어 질문 또는 검색어",
    )

    _query_not_blank = field_validator("query")(_require_non_whitespace)


class SearchDocument(_ContractModel):
    doc_id: str
    title: str | None
    section_key: str | None
    section_kind: str | None
    is_current: bool | None
    text: str


class SearchDocsOutput(_ContractModel):
    results: list[SearchDocument]
    truncated: bool


class LookupIdInput(_ContractModel):
    kind: Literal["ticket", "approval", "incident", "service", "regulation"]
    value: str = Field(
        min_length=1,
        max_length=IDENTIFIER_MAX_LENGTH,
        description="정확히 조회할 식별자 값",
    )

    _value_not_blank = field_validator("value")(_require_non_whitespace)


class LookupDocument(_ContractModel):
    doc_id: str
    title: str | None
    is_current: bool | None
    date: str | None
    text: str


class LookupIdOutput(_ContractModel):
    documents: list[LookupDocument]
    truncated: bool


class RunSqlInput(_ContractModel):
    sql: str = Field(
        min_length=1,
        max_length=SQL_MAX_LENGTH,
        description="허용된 테이블만 읽는 단일 SELECT 문",
    )

    _sql_not_blank = field_validator("sql")(_require_non_whitespace)


class RunSqlOutput(_ContractModel):
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool


class GetDocumentInput(_ContractModel):
    doc_id: str = Field(
        min_length=1,
        max_length=DOCUMENT_ID_MAX_LENGTH,
        description="가져올 문서 식별자",
    )
    section_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=SECTION_KEY_MAX_LENGTH,
        description="특정 구간만 가져올 때의 구간 식별자",
    )

    _doc_id_not_blank = field_validator("doc_id")(_require_non_whitespace)
    _section_key_not_blank = field_validator("section_key")(
        lambda value: None if value is None else _require_non_whitespace(value)
    )


class GetDocumentOutput(_ContractModel):
    doc_id: str
    title: str
    sections: list[str | None]
    text: str
    truncated: bool


_SQL_COLUMNS = {
    "tickets": (
        "ticket_id",
        "project_key",
        "owner_team",
        "title",
        "ticket_type",
        "status",
        "priority",
        "service",
        "incident_id",
        "reporter_id",
        "assignee_id",
        "created_at",
        "resolved_at",
        "story_points",
        "access_level",
        "access_teams",
        "access_min_grade",
        "is_anchor",
        "fact_ids",
    ),
    "approvals": (
        "approval_id",
        "owner_team",
        "category",
        "title",
        "amount_krw",
        "status",
        "requester_id",
        "approver_id",
        "submitted_at",
        "decided_at",
        "regulation_ref",
        "access_level",
        "access_teams",
        "access_min_grade",
        "is_anchor",
        "fact_ids",
    ),
}


def _sql_description() -> str:
    tables = "; ".join(
        f"{table}({', '.join(_SQL_COLUMNS[table])})" for table in ALLOWED_TABLES
    )
    return (
        "권한으로 미리 필터링된 SQLite 원장을 단일 SELECT로 조회합니다. "
        f"허용 테이블과 컬럼: {tables}. "
        "SELECT 또는 SELECT로 끝나는 WITH만 허용되고 결과 행 수에는 서버 LIMIT이 적용됩니다."
    )


TOOL_DESCRIPTIONS: dict[ToolName, str] = {
    "search_docs": (
        "권한으로 미리 필터링된 문서 청크를 M5 확정 hybrid 검색으로 찾습니다. "
        "최대 8개 청크와 합계 6,000자의 근거를 반환합니다."
    ),
    "lookup_id": (
        "티켓·결재·장애·서비스·규정 식별자를 권한 필터와 함께 정확히 조회합니다. "
        "최대 5개 문서의 요약 본문을 반환합니다."
    ),
    "run_sql": _sql_description(),
    "get_document": (
        "문서 ID와 선택적 구간 ID로 권한이 확인된 문서 본문을 가져옵니다. "
        "문서가 없거나 볼 수 없으면 같은 not_found 오류를 반환합니다."
    ),
}

TOOL_INPUT_MODELS: dict[ToolName, type[BaseModel]] = {
    "search_docs": SearchDocsInput,
    "lookup_id": LookupIdInput,
    "run_sql": RunSqlInput,
    "get_document": GetDocumentInput,
}
TOOL_OUTPUT_MODELS: dict[ToolName, type[BaseModel]] = {
    "search_docs": SearchDocsOutput,
    "lookup_id": LookupIdOutput,
    "run_sql": RunSqlOutput,
    "get_document": GetDocumentOutput,
}


def error_payload(code: ErrorCode, message: str) -> dict[str, dict[str, str]]:
    """Build the one stable tool-error envelope."""
    if code not in ERROR_CODES:
        raise ValueError("unsupported tool error code")
    if not isinstance(message, str):
        raise TypeError("error message must be a string")
    return {"error": {"code": code, "message": message}}
