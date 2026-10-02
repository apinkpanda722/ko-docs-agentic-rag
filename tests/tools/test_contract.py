"""Contract, shaping, and dispatch tests for the shared tool layer."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from kodocs.retrieval.access import UserContext
from kodocs.retrieval.embedding import FakeEmbedder
from kodocs.retrieval.search import SearchParams, SearchResult
from kodocs.retrieval.sql import SqlRejected, SqlResult
from kodocs.tools import (
    ERROR_CODES,
    TOOL_VERSIONS,
    ToolDeps,
    error_payload,
    run_tool,
)
from kodocs.tools import run as run_module
from kodocs.tools.contract import (
    TOOL_DESCRIPTIONS,
    TOOL_INPUT_MODELS,
    TOOL_OUTPUT_MODELS,
)
from kodocs.tools.deps import TOOL_INDEX, load_tool_config
from kodocs.tools.snapshot import (
    ContractSnapshotError,
    build_contract_snapshot,
    canonical_snapshot_bytes,
    validate_snapshot_index,
    validate_snapshots,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_DIR = PROJECT_ROOT / "tests" / "contract" / "snapshots"
M5_CONFIG_PATH = PROJECT_ROOT / "data" / "manifest" / "m5_config.json"


class _Client:
    def __init__(self, responses: list[dict] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[str, str, object]] = []

    def request(self, method: str, path: str, json: object = None) -> dict:
        self.calls.append((method, path, json))
        if not self.responses:
            return {"hits": {"hits": []}}
        return self.responses.pop(0)


def _deps(
    client: object,
    tmp_path: Path,
    *,
    prefer_current: bool = True,
) -> ToolDeps:
    return ToolDeps(
        client=client,
        index=TOOL_INDEX,
        embedder=FakeEmbedder(dim=4),
        db_path=tmp_path / "ledger.sqlite",
        search_params=SearchParams(
            "hybrid_weighted",
            candidate_chunks=50,
            bm25_weight=0.7,
        ),
        prefer_current=prefer_current,
    )


def _raw_chunk(number: int, text: str) -> dict:
    return {
        "_score": float(100 - number),
        "_source": {
            "chunk_id": f"chunk-{number:02d}",
            "doc_id": f"doc-{number:02d}",
            "start": number * 10,
            "end": number * 10 + len(text),
            "section_key": f"section-{number:02d}",
            "section_kind": "section",
            "text_nori": text,
            "title": f"문서 {number}",
            "is_current": True,
            "rule_id": f"rule-{number:02d}",
            "date": "2026-09-30",
        },
    }


def _serialized_length(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def test_registered_contract_matches_latest_snapshots() -> None:
    """The checked-in highest version must exactly match the live contract."""
    validate_snapshots(SNAPSHOT_DIR)
    for name, version in TOOL_VERSIONS.items():
        path = SNAPSHOT_DIR / f"{name}.v{version}.json"
        expected = hashlib.sha256(canonical_snapshot_bytes(name)).hexdigest()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
        assert json.loads(path.read_text(encoding="utf-8")) == build_contract_snapshot(
            name
        )


def test_snapshot_index_protects_every_historical_snapshot() -> None:
    """Past version files remain immutable under the recorded SHA-256 index."""
    validate_snapshot_index(SNAPSHOT_DIR)


def test_snapshot_validator_detects_schema_change_without_version_bump(
    tmp_path: Path,
) -> None:
    """Prove the compatibility check fails when content changes at one version."""
    destination = tmp_path / "snapshots"
    shutil.copytree(SNAPSHOT_DIR, destination)
    current = {name: build_contract_snapshot(name) for name in sorted(TOOL_VERSIONS)}
    current["search_docs"] = {
        **current["search_docs"],
        "description": "호환되지 않는 변경",
    }

    with pytest.raises(ContractSnapshotError, match="version"):
        validate_snapshots(destination, current_snapshots=current)


def test_snapshot_validator_detects_version_bump_without_new_snapshot(
    tmp_path: Path,
) -> None:
    """Prove a bumped version cannot pass before its new file is committed."""
    destination = tmp_path / "snapshots"
    shutil.copytree(SNAPSHOT_DIR, destination)
    versions = dict(TOOL_VERSIONS)
    versions["search_docs"] += 1

    with pytest.raises(ContractSnapshotError, match="snapshot"):
        validate_snapshots(destination, versions=versions)


def test_tool_contract_names_versions_and_models_are_complete() -> None:
    expected = {"search_docs", "lookup_id", "run_sql", "get_document"}

    assert set(TOOL_VERSIONS) == expected
    assert set(TOOL_DESCRIPTIONS) == expected
    assert set(TOOL_INPUT_MODELS) == expected
    assert set(TOOL_OUTPUT_MODELS) == expected
    assert set(TOOL_VERSIONS.values()) == {1}


def test_input_models_never_expose_user_context_fields() -> None:
    forbidden = {"team", "grade", "user", "ctx", "token"}

    for model in TOOL_INPUT_MODELS.values():
        assert forbidden.isdisjoint(model.model_fields)


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("search_docs", {"query": "질문"}),
        ("lookup_id", {"kind": "ticket", "value": "PAY-0001"}),
        ("run_sql", {"sql": "SELECT COUNT(*) FROM tickets"}),
        ("get_document", {"doc_id": "doc-1"}),
    ],
)
def test_undeclared_context_argument_is_rejected(
    name: str,
    args: dict[str, object],
    tmp_path: Path,
) -> None:
    result = run_tool(
        name,
        {**args, "team": "platform"},
        UserContext("platform", 3),
        _deps(_Client(), tmp_path),
    )

    assert result.is_error is True
    assert result.payload == error_payload(
        "invalid_argument", "도구 인자가 올바르지 않습니다."
    )


def test_error_contract_has_exact_shape_and_codes() -> None:
    assert ERROR_CODES == (
        "unauthorized",
        "not_found",
        "invalid_argument",
        "limit_exceeded",
    )
    for code in ERROR_CODES:
        assert error_payload(code, "message") == {
            "error": {"code": code, "message": "message"}
        }


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("search_docs", {"query": "가" * 501}),
        ("lookup_id", {"kind": "ticket", "value": "가" * 101}),
        ("run_sql", {"sql": "가" * 2001}),
        ("get_document", {"doc_id": "가" * 101}),
        (
            "get_document",
            {"doc_id": "doc-1", "section_key": "가" * 101},
        ),
    ],
)
def test_input_length_overflow_maps_to_limit_exceeded(
    name: str,
    args: dict[str, object],
    tmp_path: Path,
) -> None:
    result = run_tool(
        name,
        args,
        UserContext("platform", 3),
        _deps(_Client(), tmp_path),
    )

    assert result.payload == error_payload(
        "limit_exceeded", "입력 길이 상한을 초과했습니다."
    )


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("search_docs", {"query": ""}),
        ("lookup_id", {"kind": "unknown", "value": "x"}),
        ("lookup_id", {"kind": "ticket", "value": 1}),
        ("run_sql", {"sql": ""}),
        ("get_document", {"doc_id": ""}),
        ("get_document", {"doc_id": "doc-1", "section_key": ""}),
    ],
)
def test_invalid_type_enum_and_empty_values_map_to_invalid_argument(
    name: str,
    args: dict[str, object],
    tmp_path: Path,
) -> None:
    result = run_tool(
        name,
        args,
        UserContext("platform", 3),
        _deps(_Client(), tmp_path),
    )

    assert result.payload == error_payload(
        "invalid_argument", "도구 인자가 올바르지 않습니다."
    )


def test_missing_and_unauthorized_document_payloads_are_byte_identical(
    tmp_path: Path,
) -> None:
    ctx = UserContext("platform", 1)
    deps = _deps(_Client(), tmp_path)

    missing = run_tool("get_document", {"doc_id": "missing"}, ctx, deps)
    unauthorized = run_tool("get_document", {"doc_id": "restricted"}, ctx, deps)

    assert missing.is_error is True
    assert json.dumps(missing.payload, sort_keys=True) == json.dumps(
        unauthorized.payload, sort_keys=True
    )
    assert missing.payload == error_payload("not_found", "문서를 찾을 수 없습니다.")


def test_search_truncates_first_oversize_chunk_at_6000_characters(
    tmp_path: Path,
) -> None:
    client = _Client([{"hits": {"hits": [_raw_chunk(1, "가" * 6001)]}}])

    result = run_tool(
        "search_docs",
        {"query": "검색"},
        UserContext("platform", 3),
        _deps(client, tmp_path),
    )

    assert result.is_error is False
    assert len(result.payload["results"]) == 1
    assert len(result.payload["results"][0]["text"]) == 6000
    assert result.payload["truncated"] is True


def test_search_drops_eighth_chunk_whole_when_text_budget_would_overflow(
    tmp_path: Path,
) -> None:
    hits = [
        *(_raw_chunk(number, "가" * 800) for number in range(1, 8)),
        _raw_chunk(8, "나" * 401),
        _raw_chunk(9, "다"),
    ]
    client = _Client([{"hits": {"hits": hits}}])

    result = run_tool(
        "search_docs",
        {"query": "검색"},
        UserContext("platform", 3),
        _deps(client, tmp_path),
    )

    texts = [item["text"] for item in result.payload["results"]]
    assert len(texts) == 7
    assert sum(map(len, texts)) == 5600
    assert result.payload["truncated"] is True


def test_search_returns_at_most_eight_chunks_and_stable_refs(tmp_path: Path) -> None:
    hits = [_raw_chunk(number, str(number)) for number in range(1, 10)]
    client = _Client([{"hits": {"hits": hits}}])

    result = run_tool(
        "search_docs",
        {"query": "검색"},
        UserContext("platform", 3),
        _deps(client, tmp_path),
    )

    assert len(result.payload["results"]) == 8
    assert result.payload["truncated"] is True
    assert len(result.refs) == 8
    assert result.refs[0] == {
        "kind": "doc",
        "doc_id": "doc-01",
        "section_key": "section-01",
    }
    assert result.timings is not None
    assert set(result.timings) == {"embed_ms", "search_ms"}
    assert "timings" not in result.payload


def test_prefer_current_setting_reaches_authorized_search(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[bool] = []

    def fake_search(*_args: object, prefer_current: bool = False) -> SearchResult:
        captured.append(prefer_current)
        return SearchResult(chunks=(), docs=(), embed_ms=None, search_ms=1.0)

    monkeypatch.setattr(run_module, "authorized_search", fake_search)

    result = run_tool(
        "search_docs",
        {"query": "검색"},
        UserContext("platform", 3),
        _deps(_Client(), tmp_path, prefer_current=True),
    )

    assert result.is_error is False
    assert captured == [True]


def test_lookup_limits_documents_text_and_serialized_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    hits = tuple(
        SimpleNamespace(
            doc_id=f"doc-{number}",
            title=f"문서 {number}",
            is_current=True,
            date="2026-09-30",
        )
        for number in range(6)
    )
    captured: dict[str, object] = {}

    def fake_lookup(*_args: object, **kwargs: object):
        captured.update(kwargs)
        return hits

    def fake_document(*_args: object, **_kwargs: object):
        return SimpleNamespace(
            chunks=(SimpleNamespace(start=0, text="가" * 1300, section_key="s1"),)
        )

    monkeypatch.setattr(run_module, "authorized_lookup_id", fake_lookup)
    monkeypatch.setattr(run_module, "authorized_get_document", fake_document)

    result = run_tool(
        "lookup_id",
        {"kind": "ticket", "value": "PAY-0001"},
        UserContext("platform", 3),
        _deps(_Client(), tmp_path),
    )

    assert len(result.payload["documents"]) <= 5
    assert all(len(item["text"]) <= 1200 for item in result.payload["documents"])
    assert _serialized_length(result.payload) <= 6000
    assert result.payload["truncated"] is True
    assert captured["prefer_current"] is True
    assert captured["size"] > 5


def test_lookup_empty_result_is_success(tmp_path: Path) -> None:
    result = run_tool(
        "lookup_id",
        {"kind": "ticket", "value": "PAY-9999"},
        UserContext("platform", 3),
        _deps(_Client(), tmp_path),
    )

    assert result.is_error is False
    assert result.payload == {"documents": [], "truncated": False}


def test_sql_rejection_exposes_only_reason_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def reject(*_args: object, **_kwargs: object) -> SqlResult:
        raise SqlRejected("table_not_allowed")

    monkeypatch.setattr(run_module, "authorized_run_sql", reject)

    result = run_tool(
        "run_sql",
        {"sql": "SELECT * FROM people"},
        UserContext("platform", 3),
        _deps(_Client(), tmp_path),
    )

    assert result.payload == error_payload("invalid_argument", "table_not_allowed")


def test_sql_drops_trailing_rows_to_serialized_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sql_result = SqlResult(
        columns=("value",),
        rows=tuple((str(number) + "가" * 1000,) for number in range(10)),
        truncated=False,
    )
    monkeypatch.setattr(
        run_module,
        "authorized_run_sql",
        lambda *_args, **_kwargs: sql_result,
    )

    result = run_tool(
        "run_sql",
        {"sql": "SELECT value FROM tickets"},
        UserContext("platform", 3),
        _deps(_Client(), tmp_path),
    )

    assert _serialized_length(result.payload) <= 6000
    assert len(result.payload["rows"]) < 10
    assert result.payload["truncated"] is True
    assert result.refs == ({"kind": "sql"},)


def test_get_document_orders_chunks_and_truncates_joined_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    document = SimpleNamespace(
        doc_id="doc-1",
        title="문서",
        chunks=(
            SimpleNamespace(start=4000, text="나" * 3000, section_key="s2"),
            SimpleNamespace(start=0, text="가" * 4000, section_key="s1"),
        ),
    )
    monkeypatch.setattr(
        run_module,
        "authorized_get_document",
        lambda *_args, **_kwargs: document,
    )

    result = run_tool(
        "get_document",
        {"doc_id": "doc-1"},
        UserContext("platform", 3),
        _deps(_Client(), tmp_path),
    )

    assert len(result.payload["text"]) == 6000
    assert result.payload["text"].startswith("가" * 4000)
    assert result.payload["text"].endswith("나" * 2000)
    assert result.payload["sections"] == ["s1", "s2"]
    assert result.payload["truncated"] is True


def test_unexpected_retrieval_exception_propagates(tmp_path: Path) -> None:
    class BrokenClient:
        def request(self, *_args: object, **_kwargs: object) -> dict:
            raise RuntimeError("OpenSearch unavailable")

    with pytest.raises(RuntimeError, match="OpenSearch unavailable"):
        run_tool(
            "search_docs",
            {"query": "검색"},
            UserContext("platform", 3),
            _deps(BrokenClient(), tmp_path),
        )


def test_load_tool_config_uses_frozen_m5_structural_index() -> None:
    index, params = load_tool_config(M5_CONFIG_PATH)
    manifest = json.loads(M5_CONFIG_PATH.read_text(encoding="utf-8"))

    assert index == "kodocs-m5-composite-structural"
    assert index in manifest["indexes"]
    assert params == SearchParams(
        method="hybrid_weighted",
        analyzer="nori",
        candidate_chunks=50,
        bm25_weight=0.7,
        rank_constant=60,
    )


def test_run_sql_description_names_allowed_tables_and_columns() -> None:
    description = TOOL_DESCRIPTIONS["run_sql"]

    assert "tickets" in description
    assert "approvals" in description
    assert "ticket_id" in description
    assert "approval_id" in description
    assert "amount_krw" in description


def test_agent_events_and_trace_for_restricted_and_missing_are_identical(
    tmp_path, monkeypatch
):
    from kodocs.agent.evidence import EvidenceLog
    from kodocs.agent.guardrails import Guard, GuardrailConfig
    from kodocs.agent.tools import AgentContext, execute_tool_call
    from kodocs.agent.trace import TraceRecorder

    monkeypatch.setattr("kodocs.agent.tools.time.perf_counter", lambda: 0)
    # The fake search applies the pre-filter: an actual private source is kept by
    # the fake backend, but never returned to the unauthorized context.
    private = _raw_chunk(1, "PRIVATE")
    private["_source"].update(
        doc_id="same-id",
        access_level="restricted",
        access_teams=["legal"],
        access_min_grade=4,
    )

    def matches(query, source):
        if "term" in query:
            return all(
                value in source.get(key, [])
                if isinstance(source.get(key), list)
                else source.get(key) == value
                for key, value in query["term"].items()
            )
        if "range" in query:
            return all(
                source[key] <= value["lte"] for key, value in query["range"].items()
            )
        body = query["bool"]
        return all(matches(q, source) for q in body.get("filter", [])) and (
            sum(matches(q, source) for q in body.get("should", []))
            >= body.get("minimum_should_match", 0)
        )

    class Backend(_Client):
        def __init__(self, documents=()):
            super().__init__()
            self.documents = documents

        def request(self, method, path, json=None):
            self.calls.append((method, path, json))
            assert json["query"]["bool"]["filter"][0]
            return {
                "hits": {
                    "hits": [
                        doc
                        for doc in self.documents
                        if matches(json["query"], doc["_source"])
                    ]
                }
            }

    def attempt(client):
        events = []
        trace = TraceRecorder(tmp_path, clock=lambda: 0).start(
            question_id="q",
            system="simple",
            user=UserContext("platform", 1),
            model_record={},
            hashes={},
        )
        ctx = AgentContext(
            UserContext("platform", 1),
            EvidenceLog(),
            Guard(GuardrailConfig(), timeout_s=45),
            trace,
            event_sink=events.append,
        )
        execute_tool_call(
            ctx, _deps(client, tmp_path), "get_document", {"doc_id": "same-id"}
        )
        return events, trace._steps

    restricted = Backend([private])
    absent = Backend()
    assert matches({"term": {"doc_id": "same-id"}}, private["_source"])
    from kodocs.retrieval.access import access_filter

    assert matches(access_filter(UserContext("legal", 4)), private["_source"])
    assert not matches(access_filter(UserContext("platform", 1)), private["_source"])
    assert attempt(restricted) == attempt(absent)
