"""lookup_id chunk/document mix-up impact: aggregation contracts (no services)."""

import json
from pathlib import Path

from kodocs.eval import m8_lookup_impact as m

ROOT = Path(__file__).resolve().parents[2]


def trace(*calls):
    return {
        "steps": [
            {"kind": "model", "name": "agent"},
            *calls,
            {"kind": "tool", "name": "search_docs", "args": {"query": "q"}, "refs": []},
        ]
    }


def call(value, docs):
    return {
        "kind": "tool",
        "name": "lookup_id",
        "args": {"kind": "incident", "value": value},
        "refs": [{"doc_id": d, "kind": "doc", "section_key": None} for d in docs],
    }


def test_lookup_calls_select_only_the_target_identifier():
    t = trace(call(m.TARGET_ID, ["a"]), call("INC-2026-011", ["b"]))
    assert len(m.lookup_calls(t, m.TARGET_ID)) == 1
    assert m.lookup_calls({}, m.TARGET_ID) == []


def test_omitted_checks_the_refs_of_one_call():
    assert m.omitted(call(m.TARGET_ID, ["a"]), m.MISSED_DOC) is True
    assert m.omitted(call(m.TARGET_ID, ["a", m.MISSED_DOC]), m.MISSED_DOC) is False


def test_score_of_means_the_three_repeats_and_survives_missing_entries():
    grades = {
        "k": {"class": "gemini_acc", "correctness": [3, 2, 1]},
        "z": {"class": "det_zero", "correctness": []},
    }
    assert m.score_of(grades, "k") == {"class": "gemini_acc", "correctness_mean": 2.0}
    assert m.score_of(grades, "z") == {"class": "det_zero", "correctness_mean": None}
    assert m.score_of(grades, "missing") == {"class": None, "correctness_mean": None}


def runs():
    base = {"question_id": "s-syn-078", "_run_set": "test_silver", "model": "luna"}
    return [
        {**base, "system": "agentic", "trace_id": "t1", "refused": False},
        {**base, "system": "simple", "trace_id": "t2", "refused": True},
        {**base, "question_id": "other", "system": "agentic", "trace_id": "t3"},
    ]


def test_run_rows_count_calls_and_omissions_only_when_the_asker_may_read():
    traces = {"t1": trace(call(m.TARGET_ID, ["a"])), "t2": trace()}
    grades = {
        "test_silver|s-syn-078|agentic|luna": {
            "class": "gemini_acc",
            "correctness": [0, 0, 0],
        }
    }
    readable = m.run_rows(runs(), traces, grades, lambda q: True)
    agentic = next(r for r in readable if r["system"] == "agentic")
    assert agentic["lookup_id_calls_for_target"] == 1
    assert agentic["calls_missing_report"] == 1
    assert agentic["correctness_mean"] == 0.0
    simple = next(r for r in readable if r["system"] == "simple")
    assert simple["lookup_id_calls_for_target"] is None  # no lookup_id tool
    assert len(readable) == 2  # the unrelated question is skipped
    unreadable = m.run_rows(runs(), traces, grades, lambda q: False)
    assert (
        next(r for r in unreadable if r["system"] == "agentic")["calls_missing_report"]
        == 0
    )


def test_run_rows_tolerate_a_missing_trace_and_carry_no_text():
    rows = m.run_rows(runs(), {}, {}, lambda q: True)
    assert all(r["trace_found"] is False for r in rows)
    assert not {"answer", "question"} & set(rows[0])


def test_corpus_measurement_counts_changes_and_budget_prefixes():
    top5 = {
        "INC-1": ["a", "b", "c", "d", "e"],
        "INC-2": ["a", "b"],
        "INC-3": ["a", "b", "c", "d", "e"],
    }

    def tool(kind, value, team, chunk_limit):
        if chunk_limit == 6:  # the old chunk window
            return ["a", "b"] if value == "INC-2" else top5[value][:4]
        return {
            "INC-1": top5["INC-1"],
            "INC-2": ["a", "b"],
            "INC-3": top5["INC-3"][:4],
        }[value]

    result = m.corpus_measurement(
        [("incident", v, "hr") for v in top5], tool, lambda k, v, t: top5[v]
    )
    part = result["by_kind"]["incident"]
    assert part == {
        "identifiers": 3,
        "changed_by_fix": 1,
        "equal_top5": 2,
        "budget_prefix": 1,
    }
    assert result["changed"] == [{"kind": "incident", "value": "INC-1", "added": ["e"]}]


def test_committed_manifest_matches_the_measured_scope_and_has_no_text():
    manifest = json.loads((ROOT / m.OUTPUT).read_text(encoding="utf-8"))
    corpus = manifest["corpus"]["by_kind"]
    assert (
        corpus["incident"]["identifiers"] == 25
        and corpus["ticket"]["identifiers"] == 22
    )
    assert (
        corpus["incident"]["changed_by_fix"] == 1
        and corpus["ticket"]["changed_by_fix"] == 0
    )
    assert manifest["corpus"]["changed"] == [
        {"kind": "incident", "value": m.TARGET_ID, "added": [m.MISSED_DOC]}
    ]
    assert {r["question_id"] for r in manifest["runs"]} == set(m.QUESTIONS)
    assert len(manifest["runs"]) == 8
    dumped = json.dumps(manifest, ensure_ascii=False)
    assert '"answer"' not in dumped and '"question"' not in dumped
