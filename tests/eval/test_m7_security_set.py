"""Offline security scenarios and preregistered exposure termination rules."""

import json
from collections import Counter
from pathlib import Path

import pytest

from kodocs.eval import m7_security_set as security
from kodocs.retrieval.access import UserContext, can_read_doc
from kodocs.retrieval.search import ChunkHit, SearchResult

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def inputs():
    from dataclasses import replace

    loaded = security.load_inputs(ROOT)
    if loaded.ledger_rows:
        return loaded
    # The local SQLite ledger is untracked; keep unit coverage without building it.
    rows = tuple(
        {
            "row_id": f"fixture-{index:04d}",
            "table": "approvals",
            "owner_team": "finance",
            "access_level": "restricted",
            "access_teams": "finance",
            "access_min_grade": 4,
        }
        for index in range(20)
    )
    return replace(loaded, ledger_rows=rows)


def hit(doc_id, text, index=0):
    return ChunkHit(f"chunk-{index}", doc_id, 1.0, 0, len(text), "s1", "section", text)


def result(chunks):
    return SearchResult(tuple(chunks), (), None, 0.0)


def test_m7_security_set_counts_and_hash(inputs):
    rows = security.generate_scenarios(inputs)
    counts = Counter(row["kind"] for row in rows)
    assert counts == {
        "S1": 30,
        "S2": 60,
        "S3": 30,
        "S4": 30,
        "S5": 20,
        "S6": 48,
        "control": 30,
    }
    assert security.count_unique(rows) == 218
    assert security.scenarios_sha256(rows) == security.scenarios_sha256(
        security.generate_scenarios(inputs)
    )
    security.validate_scenarios(rows, inputs)
    users = inputs.users
    docs = {doc["doc_id"]: doc for doc in inputs.plan}
    for row in rows:
        assert row["asker"] in users
        for doc_id in row["target_doc_ids"]:
            readable = security.independent_can_read(docs[doc_id], row["asker"])
            assert readable == (row["control"] or row["kind"] == "S6")
        if row["kind"] == "S6":
            assert row["asker"]["grade"] in {1, 3}
            assert not any(
                security.independent_can_read(d, row["asker"])
                for d in inputs.restricted
            )


def test_m7_security_set_independent_policy_exhaustive(inputs):
    assert len(inputs.users) == 32
    assert len(inputs.plan) == 200
    assert len(inputs.restricted) == 30
    for doc in inputs.plan:
        access = doc["access"]
        for asker in inputs.users:
            assert security.independent_can_read(doc, asker) == can_read_doc(
                UserContext(asker["team"], asker["grade"]),
                level=access["level"],
                teams=access["teams"],
                min_grade=access["min_grade"],
            )


@pytest.mark.parametrize("attempt", [0, 1, 3, None])
def test_m7_security_set_exposure_replacements(inputs, attempt):
    rows = security.generate_scenarios(inputs)
    target = next(row for row in rows if row["kind"] == "S6")
    injection = inputs.injections[target["target_doc_ids"][0]]
    calls = []

    def searcher(question, asker):
        calls.append((question, asker))
        return (
            result([hit(injection["doc_id"], injection["text"])])
            if (attempt is not None and len(calls) == attempt + 1)
            else result([])
        )

    report = security.check_s6_exposure([target], searcher, inputs=inputs)
    row = report.scenarios[0]
    assert row["attempt"] == (3 if attempt is None else attempt)
    assert row["excluded"] == (attempt is None)
    assert row["in_final_context"] == (attempt is not None)
    assert row["exclusion_reason"] == (
        "never_in_final_context" if attempt is None else None
    )
    assert len(report.history) == (4 if attempt is None else attempt + 1)
    assert len({q for q, _ in calls}) == len(calls)
    assert all(asker == target["asker"] for _, asker in calls)
    if attempt is not None:
        assert report.history[-1]["matches"] == [{"rank": 1, "chunk_id": "chunk-0"}]


@pytest.mark.parametrize("mode", ["ninth", "budget", "wrong_doc", "partial"])
def test_m7_security_set_final_context_limits(inputs, mode):
    target = next(
        row for row in security.generate_scenarios(inputs) if row["kind"] == "S6"
    )
    inj = inputs.injections[target["target_doc_ids"][0]]
    poison = hit(inj["doc_id"], inj["text"], 9)
    if mode == "ninth":
        chunks = [hit("other", "short", i) for i in range(8)] + [poison]
    elif mode == "budget":
        chunks = [hit("other", "x" * 5999), poison]
    elif mode == "wrong_doc":
        chunks = [hit("other", inj["text"])]
    else:
        chunks = [hit(inj["doc_id"], "x" * 5990 + inj["text"])]
    report = security.check_s6_exposure(
        [target], lambda q, a: result(chunks), inputs=inputs
    )
    assert report.scenarios[0]["excluded"]


def test_m7_security_set_normalized_exposure(inputs):
    row = next(
        row for row in security.generate_scenarios(inputs) if row["kind"] == "S6"
    )
    inj = inputs.injections[row["target_doc_ids"][0]]
    report = security.check_s6_exposure(
        [row],
        lambda q, a: result([hit(inj["doc_id"], inj["text"].replace(" ", "\n"))]),
        inputs=inputs,
    )
    assert report.scenarios[0]["in_final_context"]


def test_m7_security_set_exclusions_supplement_manifest(inputs, tmp_path):
    rows = security.generate_scenarios(inputs)
    report = security.check_s6_exposure(rows, lambda q, a: result([]), inputs=inputs)
    manifest = security.write_set(report.scenarios, inputs, tmp_path, report=report)
    assert manifest["non_control_total"] == 200
    assert manifest["supplemented"]
    assert manifest["supplement_count"] == 30
    assert len(manifest["s6_exposure_history"]) == 48 * 4
    assert manifest["s6_exposure_checked"]
    assert manifest["s6_remaining_all_in_final_context"]
    assert (
        manifest["count_unit"]
        == "unique (asker, question) pairs, excluding controls and excluded scenarios"
    )
    assert len(manifest["s6_exclusions"]) == 48
    assert all(
        item["reason"] == "never_in_final_context" for item in manifest["s6_exclusions"]
    )
    security.validate_scenarios(report.scenarios, inputs)


def test_m7_security_set_no_ledger_supplement(inputs):
    from dataclasses import replace

    empty = replace(inputs, ledger_rows=())
    rows = security.generate_scenarios(empty)
    assert not any(row["kind"] == "S5" for row in rows)
    assert security.count_unique(rows) >= 200
    assert sum(row["template_id"].startswith("supplement") for row in rows) == 20


def test_m7_security_set_no_index_write_and_secrets(inputs, tmp_path):
    rows = security.generate_scenarios(inputs)
    manifest = security.write_set(rows, inputs, tmp_path)
    assert not manifest["s6_exposure_checked"]
    assert not manifest["s6_remaining_all_in_final_context"]
    path = tmp_path / "data/synthetic/security/scenarios.jsonl"
    text = path.read_text()
    assert manifest["scenarios_sha256"] == security.scenarios_sha256(rows)
    assert "token" not in text
    assert all(
        security.normalize(c["value"]) not in security.normalize(text)
        for c in inputs.canaries
    )
    assert (
        json.loads((tmp_path / "data/manifest/m7_security_set.json").read_text())
        == manifest
    )


@pytest.mark.parametrize(
    "forbidden", ["canary", "body", "topic", "duplicate", "authorized"]
)
def test_m7_security_set_validation_rejects(inputs, forbidden):
    rows = security.generate_scenarios(inputs)
    row = rows[0]
    if forbidden == "canary":
        row["question"] += inputs.canaries[0]["value"].replace("-", " - ")
    elif forbidden == "body":
        row["question"] += inputs.bodies[inputs.restricted[0]["doc_id"]][100:130]
    elif forbidden == "topic":
        row["question"] += security.title_topic(inputs.restricted[0])
    elif forbidden == "duplicate":
        rows.append(dict(row, scenario_id="duplicate"))
    else:
        row["asker"] = next(
            user
            for user in inputs.users
            if user["team"] == inputs.restricted[0]["team"] and user["grade"] == 4
        )
    with pytest.raises(ValueError):
        security.validate_scenarios(rows, inputs)


def test_m7_security_set_dry_run_no_files(inputs, tmp_path, capsys):
    assert (
        security.main(
            [
                "--root",
                str(ROOT),
                "--output-root",
                str(tmp_path),
                "--no-index",
                "--dry-run",
            ]
        )
        == 0
    )
    assert not list(tmp_path.iterdir())
    assert '"non_control_total": 218' in capsys.readouterr().out


def test_m7_security_set_all_registered_users_policy(inputs):
    users = json.loads((ROOT / "data/manifest/mcp_token_hashes.json").read_text())[
        "users"
    ]
    for user in users:
        for doc in inputs.plan:
            access = doc["access"]
            assert security.independent_can_read(doc, user) == can_read_doc(
                UserContext(user["team"], user["grade"]),
                level=access["level"],
                teams=access["teams"],
                min_grade=access["min_grade"],
            )


@pytest.mark.parametrize(
    "method,path",
    [
        ("DELETE", "/kodocs-m5-composite-structural"),
        ("PUT", "/kodocs-m5-composite-structural"),
        ("POST", "/_bulk"),
        ("POST", "/other/_search?search_pipeline=x"),
    ],
)
def test_m7_security_set_read_only_guard(method, path):
    class Client:
        def request(self, *args, **kwargs):
            pytest.fail("write reached client")

    with pytest.raises(ValueError, match="only frozen-index reads"):
        security._ReadOnlyCapture(Client()).request(method, path)


def test_m7_security_set_search_adapter_frozen_prefilter(monkeypatch, inputs):
    from kodocs.tools import ToolDeps
    from kodocs.tools.deps import TOOL_INDEX, load_tool_config

    calls = []

    class Client:
        def request(self, method, path, json=None):
            calls.append((method, path, json))
            return {
                "hits": {
                    "hits": [
                        {
                            "_score": 1.0,
                            "_source": {
                                "chunk_id": "observed-id",
                                "doc_id": "public-doc",
                                "text_nori": "본문",
                                "start": 0,
                                "end": 2,
                                "title": "제목",
                                "section_key": "s1",
                                "section_kind": "section",
                                "is_current": True,
                            },
                        }
                    ]
                }
            }

    class Embedder:
        def embed(self, texts, *, kind):
            assert texts == ["질문"] and kind == "query"
            return [[0.1, 0.2]]

    index, params = load_tool_config(ROOT / "data/manifest/m5_config.json")
    deps = ToolDeps(
        Client(), index, Embedder(), ROOT / "data/synthetic/ledger.sqlite", params
    )
    monkeypatch.setattr(security, "build_tool_deps", lambda **kwargs: deps)
    search = security._index_searcher(ROOT)
    found = search("질문", inputs.users[0])
    assert found.chunks[0].chunk_id == "observed-id"
    method, path, body = calls[0]
    assert method == "POST" and path.startswith(f"/{TOOL_INDEX}/_search?")
    assert body["size"] == 50
    assert params.bm25_weight == 0.7 and params.analyzer == "nori"
    assert deps.prefer_current
    from kodocs.retrieval.access import access_filter

    assert body["query"]["hybrid"]["queries"][0]["bool"]["filter"] == access_filter(
        UserContext(inputs.users[0]["team"], inputs.users[0]["grade"])
    )


def test_m7_security_set_failed_search_does_not_exclude(inputs):
    row = next(r for r in security.generate_scenarios(inputs) if r["kind"] == "S6")

    def failed(question, asker):
        raise RuntimeError("index unavailable")

    with pytest.raises(RuntimeError, match="index unavailable"):
        security.check_s6_exposure([row], failed, inputs=inputs)
    assert row["in_final_context"] is None and not row["excluded"]


def test_m7_security_set_no_ledger_manifest(inputs, tmp_path):
    from dataclasses import replace

    empty = replace(inputs, ledger_rows=())
    rows = security.generate_scenarios(empty)
    manifest = security.write_set(rows, empty, tmp_path)
    assert manifest["s5_omission_reason"] == "no_restricted_ledger_rows"
    assert manifest["supplement_count"] == 20
    assert manifest["non_control_total"] == 218
