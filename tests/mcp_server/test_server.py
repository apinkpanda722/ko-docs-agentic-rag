"""Official MCP client checks for contract, auth, parity, and isolation."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import anyio
import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from kodocs.mcp_server.server import create_app
from kodocs.retrieval.access import UserContext, access_filter
from kodocs.tools import TOOL_VERSIONS, ToolDeps, run_tool
from kodocs.tools.snapshot import build_contract_snapshot

from .conftest import (
    SECOND_TOKEN,
    TEST_TOKEN,
    FakeClient,
    write_hash_manifest,
)

BASE_URL = "http://127.0.0.1:8765"
UNAUTHORIZED_BODY = (
    b'{"error":{"code":"unauthorized","message":"'
    + "인증에 실패했습니다.".encode()
    + b'"}}'
)


async def _with_session(app, token: str, operation, manage_lifespan: bool = True):
    if manage_lifespan:
        async with app.router.lifespan_context(app):
            return await _with_session(app, token, operation, False)
    transport = httpx2.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {token}"}
    async with (
        httpx2.AsyncClient(
            transport=transport,
            base_url=BASE_URL,
            headers=headers,
        ) as http_client,
        streamable_http_client(
            f"{BASE_URL}/mcp",
            http_client=http_client,
            terminate_on_close=False,
        ) as streams,
        ClientSession(*streams) as session,
    ):
        await session.initialize()
        return await operation(session)


def _mcp_tools(app, token: str = TEST_TOKEN):
    return anyio.run(
        _with_session,
        app,
        token,
        lambda session: session.list_tools(),
    )


def _mcp_call(app, token: str, name: str, args: dict[str, object]):
    async def operation(session: ClientSession):
        await session.list_tools()
        return await session.call_tool(name, args)

    return anyio.run(_with_session, app, token, operation)


def test_exposed_contract_uses_snapshotted_schema_description_and_version(
    deps_factory: Callable[..., ToolDeps],
    token_hash_path: Path,
) -> None:
    listed = _mcp_tools(create_app(deps_factory(), token_hash_path=token_hash_path))

    assert {tool.name for tool in listed.tools} == set(TOOL_VERSIONS)
    for tool in listed.tools:
        snapshot = build_contract_snapshot(tool.name)
        assert tool.description == snapshot["description"]
        assert tool.input_schema == snapshot["input_schema"]
        assert tool.output_schema == snapshot["output_schema"]
        assert tool.meta == {"version": TOOL_VERSIONS[tool.name]}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer invalid"},
        {"Authorization": "Basic test-demo-token-a"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer test-demo-token-a extra"},
    ],
)
def test_all_authentication_failures_have_identical_401(
    deps_factory: Callable[..., ToolDeps],
    token_hash_path: Path,
    headers: dict[str, str],
) -> None:
    app = create_app(deps_factory(), token_hash_path=token_hash_path)

    async def request():
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url=BASE_URL,
        ) as client:
            return await client.post("/mcp", headers=headers, content=b"{}")

    response = anyio.run(request)

    assert response.status_code == 401
    assert response.content == UNAUTHORIZED_BODY
    assert response.headers["www-authenticate"] == "Bearer"


def test_valid_token_can_list_tools(
    deps_factory: Callable[..., ToolDeps],
    token_hash_path: Path,
) -> None:
    result = _mcp_tools(create_app(deps_factory(), token_hash_path=token_hash_path))

    assert len(result.tools) == 4


SUCCESS_CASES = [
    ("search_docs", {"query": "검색"}),
    ("lookup_id", {"kind": "ticket", "value": "PAY-0001"}),
    ("run_sql", {"sql": "SELECT COUNT(*) AS count FROM tickets"}),
    ("get_document", {"doc_id": "doc-1"}),
]
ERROR_CASES = [
    ("search_docs", {"query": ""}, "invalid_argument"),
    ("lookup_id", {"kind": "unknown", "value": "PAY-0001"}, "invalid_argument"),
    ("run_sql", {"sql": "DELETE FROM tickets"}, "invalid_argument"),
    ("get_document", {"doc_id": "missing"}, "not_found"),
]


@pytest.mark.parametrize(("name", "args"), SUCCESS_CASES)
def test_mcp_success_payload_has_byte_parity_with_run_tool(
    deps_factory: Callable[..., ToolDeps],
    token_hash_path: Path,
    name: str,
    args: dict[str, object],
) -> None:
    ctx = UserContext("platform", 3)
    direct = run_tool(name, args, ctx, deps_factory())
    mcp_result = _mcp_call(
        create_app(deps_factory(), token_hash_path=token_hash_path),
        TEST_TOKEN,
        name,
        args,
    )

    assert mcp_result.is_error is direct.is_error is False
    assert json.dumps(mcp_result.structured_content, sort_keys=True) == json.dumps(
        direct.payload,
        sort_keys=True,
    )


@pytest.mark.parametrize(("name", "args", "code"), ERROR_CASES)
def test_mcp_error_payload_has_byte_parity_and_is_error(
    deps_factory: Callable[..., ToolDeps],
    token_hash_path: Path,
    name: str,
    args: dict[str, object],
    code: str,
) -> None:
    ctx = UserContext("platform", 3)
    direct = run_tool(name, args, ctx, deps_factory())
    mcp_result = _mcp_call(
        create_app(deps_factory(), token_hash_path=token_hash_path),
        TEST_TOKEN,
        name,
        args,
    )

    assert mcp_result.is_error is direct.is_error is True
    assert mcp_result.structured_content["error"]["code"] == code
    assert json.dumps(mcp_result.structured_content, sort_keys=True) == json.dumps(
        direct.payload,
        sort_keys=True,
    )


def test_mcp_unauthorized_and_missing_documents_are_byte_identical(
    deps_factory: Callable[..., ToolDeps],
    token_hash_path: Path,
) -> None:
    app = create_app(deps_factory(), token_hash_path=token_hash_path)

    async def operation(session: ClientSession):
        await session.list_tools()
        denied = await session.call_tool("get_document", {"doc_id": "restricted-doc"})
        missing = await session.call_tool("get_document", {"doc_id": "missing-doc"})
        return denied, missing

    denied, missing = anyio.run(_with_session, app, TEST_TOKEN, operation)

    assert denied.is_error is missing.is_error is True
    assert json.dumps(denied.structured_content, sort_keys=True) == json.dumps(
        missing.structured_content,
        sort_keys=True,
    )


@pytest.mark.parametrize(("name", "args"), SUCCESS_CASES)
def test_user_context_fields_are_rejected_on_mcp_path(
    deps_factory: Callable[..., ToolDeps],
    token_hash_path: Path,
    name: str,
    args: dict[str, object],
) -> None:
    result = _mcp_call(
        create_app(deps_factory(), token_hash_path=token_hash_path),
        TEST_TOKEN,
        name,
        {**args, "team": "security", "grade": 99},
    )

    assert result.is_error is True
    assert result.structured_content == {
        "error": {
            "code": "invalid_argument",
            "message": "도구 인자가 올바르지 않습니다.",
        }
    }


def _contains(value: object, expected: object) -> bool:
    if value == expected:
        return True
    if isinstance(value, Mapping):
        return any(_contains(item, expected) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return any(_contains(item, expected) for item in value)
    return False


def test_mcp_search_and_lookup_bodies_all_contain_server_user_access_filter(
    deps_factory: Callable[..., ToolDeps],
    token_hash_path: Path,
) -> None:
    client = FakeClient()
    deps = deps_factory(client=client)
    app = create_app(deps, token_hash_path=token_hash_path)
    calls = [
        ("search_docs", {"query": "검색"}),
        ("lookup_id", {"kind": "ticket", "value": "PAY-0001"}),
        ("get_document", {"doc_id": "doc-1"}),
    ]

    async def operation(session: ClientSession):
        await session.list_tools()
        for name, args in calls:
            await session.call_tool(name, args)

    anyio.run(_with_session, app, TEST_TOKEN, operation)
    expected = access_filter(UserContext("platform", 3))

    assert client.calls
    assert all(_contains(body, expected) for _method, _path, body in client.calls)


def test_two_concurrent_users_keep_request_context_isolated(
    tmp_path: Path,
    deps_factory: Callable[..., ToolDeps],
) -> None:
    client = FakeClient(barrier=threading.Barrier(2))
    hash_path = write_hash_manifest(tmp_path / "hashes.json")
    app = create_app(deps_factory(client=client), token_hash_path=hash_path)
    outputs: dict[str, object] = {}

    async def call_for(label: str, token: str):
        async def operation(session: ClientSession):
            await session.list_tools()
            outputs[label] = await session.call_tool("search_docs", {"query": label})

        await _with_session(app, token, operation, False)

    async def run_both():
        async with (
            app.router.lifespan_context(app),
            anyio.create_task_group() as group,
        ):
            group.start_soon(call_for, "platform-query", TEST_TOKEN)
            group.start_soon(call_for, "security-query", SECOND_TOKEN)

    anyio.run(run_both)

    search_bodies = [
        body for _method, path, body in client.calls if "search_pipeline=" in path
    ]
    assert len(search_bodies) == 2
    assert any(
        _contains(body, access_filter(UserContext("platform", 3)))
        and "platform-query" in json.dumps(body, ensure_ascii=False)
        for body in search_bodies
    )
    assert any(
        _contains(body, access_filter(UserContext("security", 5)))
        and "security-query" in json.dumps(body, ensure_ascii=False)
        for body in search_bodies
    )
    assert set(outputs) == {"platform-query", "security-query"}
