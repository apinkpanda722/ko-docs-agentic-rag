"""Shared, versioned retrieval tool contract for in-process and MCP callers."""

from kodocs.retrieval.access import UserContext
from kodocs.tools.contract import (
    ERROR_CODES,
    INVALID_ARGUMENT,
    LIMIT_EXCEEDED,
    NOT_FOUND,
    TOOL_VERSIONS,
    UNAUTHORIZED,
    ErrorCode,
    error_payload,
)
from kodocs.tools.deps import ToolDeps, build_tool_deps, load_tool_config
from kodocs.tools.run import ToolResult, run_tool

__all__ = [
    "ERROR_CODES",
    "INVALID_ARGUMENT",
    "LIMIT_EXCEEDED",
    "NOT_FOUND",
    "TOOL_VERSIONS",
    "UNAUTHORIZED",
    "ErrorCode",
    "ToolDeps",
    "ToolResult",
    "UserContext",
    "build_tool_deps",
    "error_payload",
    "load_tool_config",
    "run_tool",
]
