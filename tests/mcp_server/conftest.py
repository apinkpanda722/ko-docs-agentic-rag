"""Shared fake dependencies for in-process MCP tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from kodocs.retrieval.embedding import FakeEmbedder
from kodocs.retrieval.search import SearchParams
from kodocs.tools import ToolDeps
from kodocs.tools.deps import TOOL_INDEX

TEST_TOKEN = "test-demo-token-a"
SECOND_TOKEN = "test-demo-token-b"


class FakeClient:
    """Return deterministic hits and retain every OpenSearch request body."""

    def __init__(self, barrier: threading.Barrier | None = None) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self._barrier = barrier
        self._lock = threading.Lock()

    def request(self, method: str, path: str, json: object = None) -> dict:
        with self._lock:
            self.calls.append((method, path, json))
        if self._barrier is not None and "search_pipeline=" in path:
            self._barrier.wait(timeout=5)
        if not isinstance(json, dict):
            return {"hits": {"hits": []}}
        serialized = json_module_dumps(json)
        if '"ids_ticket"' in serialized:
            return {"hits": {"hits": [_lookup_hit()]}}
        if '"doc_id"' in serialized and "doc-1" in serialized:
            return {"hits": {"hits": [_document_hit()]}}
        if "search_pipeline=" in path:
            return {"hits": {"hits": [_search_hit()]}}
        return {"hits": {"hits": []}}


def json_module_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _search_hit() -> dict:
    return {
        "_score": 2.0,
        "_source": {
            "chunk_id": "chunk-1",
            "doc_id": "doc-1",
            "start": 0,
            "end": 4,
            "section_key": "section-1",
            "section_kind": "section",
            "text_nori": "검색 결과",
            "title": "문서 1",
            "is_current": True,
            "rule_id": None,
            "date": "2026-09-30",
        },
    }


def _lookup_hit() -> dict:
    return {
        "_score": 1.0,
        "_source": {
            "chunk_id": "chunk-1",
            "doc_id": "doc-1",
            "date": "2026-09-30",
            "title": "문서 1",
            "is_current": True,
            "rule_id": None,
        },
    }


def _document_hit() -> dict:
    return {
        "_score": 1.0,
        "_source": {
            "chunk_id": "chunk-1",
            "doc_id": "doc-1",
            "title": "문서 1",
            "section_key": "section-1",
            "section_kind": "section",
            "start": 0,
            "end": 4,
            "text_nori": "문서 본문",
        },
    }


def write_ledger(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE tickets (
                ticket_id TEXT PRIMARY KEY,
                owner_team TEXT NOT NULL,
                status TEXT NOT NULL,
                access_level TEXT NOT NULL,
                access_teams TEXT NOT NULL,
                access_min_grade INTEGER NOT NULL
            );
            CREATE TABLE approvals (
                approval_id TEXT PRIMARY KEY,
                owner_team TEXT NOT NULL,
                status TEXT NOT NULL,
                access_level TEXT NOT NULL,
                access_teams TEXT NOT NULL,
                access_min_grade INTEGER NOT NULL
            );
            INSERT INTO tickets VALUES
                ('PUB-1', 'platform', 'open', 'public', '', 0),
                ('SEC-1', 'security', 'open', 'restricted', 'security', 4);
            INSERT INTO approvals VALUES
                ('EXP-1', 'platform', 'approved', 'public', '', 0);
            """
        )


def make_deps(tmp_path: Path, *, client: FakeClient | None = None) -> ToolDeps:
    ledger = tmp_path / "ledger.sqlite"
    if not ledger.exists():
        write_ledger(ledger)
    return ToolDeps(
        client=client or FakeClient(),
        index=TOOL_INDEX,
        embedder=FakeEmbedder(dim=4),
        db_path=ledger,
        search_params=SearchParams(
            "hybrid_weighted",
            candidate_chunks=50,
            bm25_weight=0.7,
        ),
        prefer_current=True,
    )


def write_hash_manifest(
    path: Path,
    users: tuple[tuple[str, str, int], ...] = (
        (TEST_TOKEN, "platform", 3),
        (SECOND_TOKEN, "security", 5),
    ),
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "users": [
            {
                "label": f"demo-{team}-g{grade}",
                "team": team,
                "grade": grade,
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            }
            for token, team, grade in users
        ]
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def deps_factory(tmp_path: Path) -> Callable[..., ToolDeps]:
    counter = iter(range(100))

    def factory(*, client: FakeClient | None = None) -> ToolDeps:
        directory = tmp_path / f"deps-{next(counter)}"
        directory.mkdir()
        return make_deps(directory, client=client)

    return factory


@pytest.fixture
def token_hash_path(tmp_path: Path) -> Path:
    return write_hash_manifest(tmp_path / "mcp_token_hashes.json")


@pytest.fixture
def project_root() -> Iterator[Path]:
    yield Path(__file__).resolve().parents[2]
