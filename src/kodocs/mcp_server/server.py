"""Authenticated streamable-HTTP MCP adapter over the shared tool dispatcher."""

from __future__ import annotations

import json
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import anyio
from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from kodocs.mcp_server.tokens import TOKEN_HASH_PATH, TokenHashStore
from kodocs.tools import ToolDeps, UserContext, run_tool
from kodocs.tools.contract import (
    TOOL_DESCRIPTIONS,
    TOOL_INPUT_MODELS,
    TOOL_OUTPUT_MODELS,
    TOOL_VERSIONS,
)

_AUTHORIZATION = re.compile(r"(?i:Bearer) (?P<token>[^\s]+)")
_UNAUTHORIZED_BODY = (
    b'{"error":{"code":"unauthorized","message":"'
    + "인증에 실패했습니다.".encode()
    + b'"}}'
)


class _BearerAuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        token_store: TokenHashStore,
        current_user: ContextVar[UserContext | None],
    ) -> None:
        self.app = app
        self.token_store = token_store
        self.current_user = current_user

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        authorization = _single_authorization_header(scope)
        match = (
            None if authorization is None else _AUTHORIZATION.fullmatch(authorization)
        )
        user = (
            None
            if match is None
            else self.token_store.authenticate(match.group("token"))
        )
        if user is None:
            response = Response(
                _UNAUTHORIZED_BODY,
                status_code=401,
                media_type="application/json",
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        context_token = self.current_user.set(user)
        try:
            await self.app(scope, receive, send)
        finally:
            self.current_user.reset(context_token)


def create_app(
    deps: ToolDeps,
    *,
    token_hash_path: Path = TOKEN_HASH_PATH,
) -> Starlette:
    """Build one stateless MCP ASGI app with request-local bearer identity."""
    token_store = TokenHashStore.load(token_hash_path)
    current_user: ContextVar[UserContext | None] = ContextVar(
        "mcp_request_user",
        default=None,
    )

    async def list_tools(
        _ctx: ServerRequestContext[Any],
        _params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        _require_user(current_user)
        return types.ListToolsResult(tools=_contract_tools())

    async def call_tool(
        _ctx: ServerRequestContext[Any],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        user = _require_user(current_user)
        result = await anyio.to_thread.run_sync(
            run_tool,
            params.name,
            params.arguments or {},
            user,
            deps,
        )
        return types.CallToolResult(
            content=[
                types.TextContent(
                    text=json.dumps(
                        result.payload,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
            ],
            structuredContent=result.payload,
            isError=result.is_error,
        )

    server: Server[dict[str, Any]] = Server(
        "ko-docs-agentic-rag",
        version="1",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        host="127.0.0.1",
    )
    app.add_middleware(
        _BearerAuthMiddleware,
        token_store=token_store,
        current_user=current_user,
    )
    return app


def _contract_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name=name,
            description=TOOL_DESCRIPTIONS[name],
            inputSchema=TOOL_INPUT_MODELS[name].model_json_schema(mode="validation"),
            outputSchema=TOOL_OUTPUT_MODELS[name].model_json_schema(
                mode="serialization"
            ),
            _meta={"version": version},
        )
        for name, version in TOOL_VERSIONS.items()
    ]


def _require_user(
    current_user: ContextVar[UserContext | None],
) -> UserContext:
    user = current_user.get()
    if user is None:
        raise RuntimeError("authenticated MCP request has no user context")
    return user


def _single_authorization_header(scope: Scope) -> str | None:
    values = [
        value.decode("latin-1")
        for name, value in scope.get("headers", ())
        if name.lower() == b"authorization"
    ]
    if len(values) != 1:
        return None
    return values[0]
