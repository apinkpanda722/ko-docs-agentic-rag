"""Read-only permission checks against the already-built M5 measured index."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import anyio
import httpx
import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from kodocs.mcp_server.server import create_app
from kodocs.retrieval.access import UserContext, can_read_doc
from kodocs.retrieval.embedding import FakeEmbedder
from kodocs.retrieval.opensearch import OpenSearchClient
from kodocs.tools import ToolDeps, run_tool
from kodocs.tools.deps import TOOL_INDEX, load_tool_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "http://127.0.0.1:9200")
MCP_URL = "http://127.0.0.1:8765/mcp"
RESTRICTED_CASES = (
    (
        "syn-meeting-045",
        "금빛고래-9272",
        UserContext("security", 3),
        {"kind": "regulation", "value": "정보보안 기본지침 제72조"},
    ),
    (
        "syn-meeting-046",
        "금빛상어-2094",
        UserContext("finance", 6),
        {"kind": "regulation", "value": "근무성적평정규칙 제10조"},
    ),
    (
        "syn-meeting-048",
        "짙은토끼-5222",
        UserContext("legal", 3),
        {"kind": "ticket", "value": "LGL-1001"},
    ),
)


def _token(ctx: UserContext) -> str:
    return f"integration-{ctx.team}-g{ctx.grade}"


def _write_hashes(path: Path) -> None:
    users = []
    for _doc_id, _canary, ctx, _lookup in RESTRICTED_CASES:
        users.append(
            {
                "label": f"demo-{ctx.team}-g{ctx.grade}",
                "team": ctx.team,
                "grade": ctx.grade,
                "token_sha256": hashlib.sha256(_token(ctx).encode()).hexdigest(),
            }
        )
    path.write_text(json.dumps({"users": users}), encoding="utf-8")


def _serialized(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _assert_no_leak(payload: object) -> None:
    serialized = _serialized(payload)
    for doc_id, canary, _ctx, _lookup in RESTRICTED_CASES:
        assert doc_id not in serialized
        assert canary not in serialized


def _assert_cases_are_restricted() -> None:
    assert len({doc_id for doc_id, *_rest in RESTRICTED_CASES}) == 3
    assert len({ctx for _doc_id, _canary, ctx, _lookup in RESTRICTED_CASES}) == 3
    for doc_id, canary, ctx, _lookup in RESTRICTED_CASES:
        path = PROJECT_ROOT / "data/synthetic/docs" / f"{doc_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        access = payload["access"]
        assert access["level"] == "restricted"
        assert payload["canary"]["value"] == canary
        assert not can_read_doc(
            ctx,
            level=access["level"],
            teams=access["teams"],
            min_grade=access["min_grade"],
        )


async def _mcp_payloads(app, token: str, calls: list[tuple[str, dict]]) -> list[object]:
    results: list[object] = []
    transport = httpx2.ASGITransport(app=app)
    async with (
        httpx2.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8765",
            headers={"Authorization": f"Bearer {token}"},
        ) as http_client,
        streamable_http_client(
            MCP_URL,
            http_client=http_client,
            terminate_on_close=False,
        ) as streams,
        ClientSession(*streams) as session,
    ):
        await session.initialize()
        await session.list_tools()
        for name, args in calls:
            result = await session.call_tool(name, args)
            results.append(result.structured_content)
    return results


@pytest.mark.integration
def test_m5_index_permissions_match_across_run_tool_and_mcp(tmp_path: Path) -> None:
    """Never create or delete indexes here; this test reads the frozen M5 index."""
    _assert_cases_are_restricted()
    http_client = httpx.Client(timeout=30.0)
    client = OpenSearchClient(OPENSEARCH_URL, client=http_client)
    index, params = load_tool_config(PROJECT_ROOT / "data/manifest/m5_config.json")
    deps = ToolDeps(
        client=client,
        index=index,
        embedder=FakeEmbedder(dim=1024),
        db_path=PROJECT_ROOT / "data/synthetic/ledger.sqlite",
        search_params=params,
        prefer_current=True,
    )
    hash_path = tmp_path / "mcp_token_hashes.json"
    _write_hashes(hash_path)
    app = create_app(deps, token_hash_path=hash_path)

    try:
        counted = client.request("GET", f"/{TOOL_INDEX}/_count")
        assert counted["count"] > 0
        assert deps.db_path.is_file(), "run `make build-db` before integration tests"

        async def exercise_mcp() -> list[list[object]]:
            async with app.router.lifespan_context(app):
                all_results = []
                for doc_id, canary, ctx, lookup_args in RESTRICTED_CASES:
                    calls = [
                        ("search_docs", {"query": canary}),
                        ("lookup_id", lookup_args),
                        (
                            "run_sql",
                            {
                                "sql": "SELECT ticket_id, title FROM tickets ORDER BY ticket_id"
                            },
                        ),
                        ("get_document", {"doc_id": doc_id}),
                        ("get_document", {"doc_id": "missing-document-parity"}),
                    ]
                    all_results.append(await _mcp_payloads(app, _token(ctx), calls))
                return all_results

        mcp_results = anyio.run(exercise_mcp)
        for case, path_results in zip(RESTRICTED_CASES, mcp_results, strict=True):
            doc_id, canary, ctx, lookup_args = case
            calls = [
                ("search_docs", {"query": canary}),
                ("lookup_id", lookup_args),
                (
                    "run_sql",
                    {"sql": "SELECT ticket_id, title FROM tickets ORDER BY ticket_id"},
                ),
                ("get_document", {"doc_id": doc_id}),
                ("get_document", {"doc_id": "missing-document-parity"}),
            ]
            direct_results = [
                run_tool(name, args, ctx, deps).payload for name, args in calls
            ]
            for payload in (*direct_results, *path_results):
                _assert_no_leak(payload)
            direct_denied = direct_results[-2]
            direct_missing = direct_results[-1]
            assert _serialized(direct_denied) == _serialized(direct_missing)
            assert _serialized(path_results[-2]) == _serialized(path_results[-1])
            assert _serialized(path_results[-2]) == _serialized(direct_missing)
    except httpx.TransportError as exc:
        pytest.fail(
            f"OpenSearch is unavailable at {OPENSEARCH_URL}. "
            f"Run `make up` before retrying: {exc}"
        )
    finally:
        http_client.close()
