"""M7-F1/F2/F3 use recorded evidence only, with exclusive precedence."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from kodocs.eval import m7_failures as failures
from kodocs.eval.golden import QUESTION_TYPES, Asker, Qrel, question_from_dict
from kodocs.eval.m7_grading import AnsweredRun


@pytest.fixture
def question():
    return question_from_dict(
        {
            "id": "q",
            "set": "gold",
            "source": "synthetic",
            "type": "semantic",
            "tags": [],
            "question": "private question",
            "asker": {"team": "a", "grade": 1},
            "as_of": None,
            "qrels": [
                {"doc_id": "a", "rel": 2},
                {"doc_id": "b", "rel": 2},
                {"doc_id": "hidden", "rel": 2},
            ],
            "anchor_values": [],
            "answer_values": [],
            "expected_response": "answer",
            "sql": None,
            "reference_answer": "private reference",
            "reference_sha256": None,
            "summary80": None,
            "split": "test",
        }
    )


@pytest.fixture
def meta():
    return {
        "a": {"access": {"level": "public"}},
        "b": {"access": {"level": "public"}},
        "hidden": {"access": {"level": "restricted", "teams": ["a"], "min_grade": 4}},
    }


def run(q, **kwargs):
    r = AnsweredRun(
        "test_gold|q|agentic|gpt-6-luna",
        "test_gold",
        q,
        "private answer",
        (),
        False,
        None,
        "completed",
        "agentic",
        "gpt-6-luna",
        (),
        "hash",
        Path("unused"),
        Path("."),
        "trace",
    )
    if kwargs.get("system") == "simple":
        kwargs.setdefault("answer_id", "test_gold|q|simple|gpt-6-luna")
    return replace(r, **kwargs)


def trace(ids=("a", "b"), name="search_docs", error=None, texts=None):
    refs = [{"kind": "doc", "doc_id": i, "section_key": None} for i in ids]
    step = {
        "kind": "tool",
        "index": 1,
        "name": name,
        "args": {},
        "error": error,
        "refs": refs,
    }
    if texts is not None:
        step["result"] = {
            "results": [
                {"doc_id": i, "title": "title", "section_key": None, "text": t}
                for i, t in zip(ids, texts, strict=True)
            ]
        }
    return {
        "steps": [
            step,
            {"kind": "model", "index": 2, "name": "final_answer", "error": None},
        ]
    }


def classify(r, q, t, meta):
    return failures.classify_failure(r, q, t, docs_meta=meta)


def test_m7_failures_required_tools():
    assert set(failures.REQUIRED_TOOLS) == set(QUESTION_TYPES)
    for typ in QUESTION_TYPES:
        assert failures.REQUIRED_TOOLS[typ] == (
            frozenset({"lookup_id"})
            if typ == "identifier"
            else frozenset({"run_sql"})
            if typ == "sql"
            else frozenset({"search_docs", "get_document"})
        )


@pytest.mark.parametrize(
    "reason", ["guardrail_timeout", "guardrail_tokens", "parse_error"]
)
def test_m7_failures_guardrail_first(question, meta, reason):
    assert (
        classify(
            run(question, refused=True, refuse_reason=reason),
            question,
            trace(ids=()),
            meta,
        ).category
        == "guardrail_infra"
    )


@pytest.mark.parametrize("status", ["infra_error", "billing_uncertain"])
def test_m7_failures_infra_first(question, meta, status):
    assert (
        classify(
            run(question, refused=True, status=status), question, None, meta
        ).category
        == "guardrail_infra"
    )


def test_m7_failures_permission_precedes_search(question, meta):
    assert (
        classify(run(question, refused=True), question, trace(ids=()), meta).category
        == "permission_overrefusal"
    )
    invisible = replace(question, qrels=(Qrel("hidden", 2),))
    assert (
        classify(run(invisible, refused=True), invisible, trace(), meta).category
        == "indeterminate"
    )
    assert failures.visible_relevance(question, meta) == {"a": 2, "b": 2}
    assert "hidden" in failures.visible_relevance(
        replace(question, asker=Asker("a", 4)), meta
    )


@pytest.mark.parametrize(
    "typ,name",
    [("identifier", "search_docs"), ("sql", "search_docs"), ("semantic", "lookup_id")],
)
def test_m7_failures_tool_selection(question, meta, typ, name):
    q = replace(question, type=typ)
    assert classify(run(q), q, trace(name=name), meta).category == "tool_selection"


@pytest.mark.parametrize(
    "error", ["invalid_argument", "limit_exceeded", "OperationalError"]
)
def test_m7_failures_arguments(question, meta, error):
    name = "run_sql" if error == "OperationalError" else "search_docs"
    q = replace(question, type="sql" if name == "run_sql" else "semantic")
    assert (
        classify(run(q), q, trace(name=name, error=error), meta).category
        == "tool_argument"
    )


def test_m7_failures_simple_sql_structural(question, meta):
    q = replace(question, type="sql")
    assert (
        classify(run(q, system="simple"), q, None, meta).category
        == "structural_tool_absence"
    )


@pytest.mark.parametrize("ids,partial", [((), False), (("a",), True)])
def test_m7_failures_agentic_d1(question, meta, ids, partial):
    result = classify(run(question), question, trace(ids=ids), meta)
    assert result.category == "d1" and result.partial is partial


@pytest.mark.parametrize(
    "texts,category", [(("x" * 6000, "y"), "d2"), (("x", "y"), "generation")]
)
def test_m7_failures_reconstruct_context(question, meta, texts, category):
    assert (
        classify(run(question), question, trace(texts=texts), meta).category == category
    )


def test_m7_failures_round_robin_across_calls(question, meta):
    t = trace(ids=("a", "b"), texts=("x" * 6000, "y"))
    extra = trace(ids=("b",), texts=("z",))["steps"][0]
    extra["index"] = 2
    t["steps"] = [
        t["steps"][0],
        extra,
        {"kind": "model", "name": "final_answer", "index": 3},
    ]
    assert classify(run(question), question, t, meta).category == "d2"
    # First call consumes budget; shorter first item lets the second call enter.
    t["steps"][0]["result"]["results"][0]["text"] = "x" * 5999
    assert classify(run(question), question, t, meta).category == "generation"


def test_m7_failures_context_missing_and_citation_conflict(question, meta):
    result = classify(run(question), question, trace(), meta)
    assert result.category == "indeterminate" and result.missing == ("final_context",)
    r = run(question, citations=({"kind": "doc", "doc_id": "b"},))
    result = classify(r, question, trace(texts=("x" * 6000, "y")), meta)
    assert result.category == "indeterminate" and result.missing == (
        "citation_context_conflict",
    )


def test_m7_failures_simple_never_searches(question, meta, monkeypatch):
    from kodocs.tools import run as tools_run

    def forbidden(*args, **kwargs):
        pytest.fail("search/replay must never execute")

    monkeypatch.setattr(tools_run, "run_tool", forbidden)
    r = run(question, system="simple")
    result = classify(r, question, trace(texts=("x", "y")), meta)
    assert result.category == "indeterminate" and result.missing == ("candidates_50",)


@pytest.mark.parametrize(
    "ids,texts,category",
    [
        (("a",), ("x", "y"), "d1"),
        (("a", "b"), ("x" * 6000, "y"), "d2"),
        (("a", "b"), ("x", "y"), "generation"),
    ],
)
def test_m7_failures_simple_recorded_candidates(question, meta, ids, texts, category):
    t = trace(texts=texts)
    t["candidates_50"] = [{"doc_id": i} for i in ids]
    assert (
        classify(run(question, system="simple"), question, t, meta).category == category
    )


def test_m7_failures_missing_trace_is_not_tool_selection(question, meta):
    result = classify(run(question), question, None, meta)
    assert result.category == "indeterminate" and result.missing == ("tool_calls",)


def test_m7_failures_f1_aggregate_exclusive_and_text_free(question, meta):
    r = run(question, refused=True)
    q2 = replace(question, id="no", type="unanswerable", expected_response="no_answer")
    no = run(q2, answer_id="test_gold|no|agentic|gpt-6-luna")
    good = run(question, system="simple")
    scores = {
        r.answer_id: {"class": "det_zero", "correctness": [0, 0, 0]},
        no.answer_id: {"class": "gemini_faith_only", "correctness": []},
        good.answer_id: {"class": "gemini_acc", "correctness": [1, 2, 3]},
    }
    report, details = failures.analyze_failures(
        [r, no, good], scores, {"trace": trace(ids=())}, meta
    )
    group = report["by_system_set"]["agentic|test_gold"]
    assert group["wrong_count"] == group["classification_total"] == 1
    assert group["classification_sum_matches"] is True
    assert group["classes"]["permission_overrefusal"]["ids"] == ["q"]
    assert group["classes"]["d1"]["count"] == 0
    assert group["refusal_violations"]["ids"] == ["no"]
    assert len(details) == 1
    assert "private" not in json.dumps(report)
    assert report["by_system_set"]["simple|test_gold"]["wrong_count"] == 0
    assert failures.is_wrong(good, {"class": "gemini_acc", "correctness": [1, None, 2]})
    with pytest.raises(ValueError):
        failures.is_wrong(good, {"class": "gemini_acc", "correctness": [None, None, 1]})


def test_m7_failures_data_notes_and_cli(question, meta, tmp_path, monkeypatch):
    q = replace(question, qrels=(Qrel("law-2200000149621", 2, annex_id="17229_0-00"),))
    r = run(q, refused=True)
    meta = {"law-2200000149621": {"access": {"level": "public"}}}
    scores = {r.answer_id: {"class": "det_zero", "correctness": [0, 0, 0]}}
    report, _ = failures.analyze_failures([r], scores, {}, meta)
    assert report["data_preparation_notes"][0]["loss_type"] == "annex_context_lost"
    monkeypatch.setattr(
        failures, "verify_analysis_inputs", lambda root: {"scores": scores}
    )
    monkeypatch.setattr(failures, "load_m6_answers", lambda root: [r])
    monkeypatch.setattr(failures, "load_docs_meta", lambda root: meta)
    assert failures.main(["--root", str(tmp_path)]) == 0
    assert (tmp_path / failures.MANIFEST_PATH).exists()
    assert (tmp_path / failures.DETAILS_PATH).exists()
    monkeypatch.setattr(failures, "verify_analysis_inputs", lambda root: {"scores": {}})
    assert failures.main(["--root", str(tmp_path)]) == 2


def test_m7_failures_selection_precedes_argument(question, meta):
    q = replace(question, type="identifier")
    assert (
        classify(
            run(q), q, trace(name="search_docs", error="invalid_argument"), meta
        ).category
        == "tool_selection"
    )


def test_m7_failures_rel1_can_refuse_but_not_retrieve_truth(question, meta):
    q = replace(question, qrels=(Qrel("a", 1), Qrel("hidden", 2)))
    assert (
        classify(run(q, refused=True), q, trace(), meta).category
        == "permission_overrefusal"
    )
    assert classify(run(q), q, trace(), meta).missing == ("rel2_truth",)
    with pytest.raises(ValueError, match="missing access metadata"):
        failures.visible_relevance(q, {})


@pytest.mark.parametrize(
    "change,missing",
    [
        ("refs", "tool_refs"),
        ("payload", "result_refs_conflict"),
        ("size", "result_payload_conflict"),
        ("final", "final_context"),
    ],
)
def test_m7_failures_incomplete_or_contradictory_record(
    question, meta, change, missing
):
    t = trace(texts=("x", "y"))
    if change == "refs":
        del t["steps"][0]["refs"]
    elif change == "payload":
        t["steps"][0]["result"]["results"][1]["doc_id"] = "other"
    elif change == "size":
        t["steps"][0]["result_chars"] = 1
    else:
        t["steps"].pop()
    assert classify(run(question), question, t, meta).missing == (missing,)


def test_m7_failures_citation_section_conflict(question, meta):
    r = run(
        question, citations=({"kind": "doc", "doc_id": "a", "section_key": "missing"},)
    )
    assert classify(r, question, trace(texts=("x", "y")), meta).missing == (
        "citation_context_conflict",
    )


def test_m7_failures_aggregate_partial_structural_and_indeterminate(question, meta):
    q2 = replace(question, id="sql", type="sql")
    q3 = replace(question, id="missing")
    rs = [
        run(question),
        run(q2, system="simple", answer_id="test_gold|sql|simple|gpt-6-luna"),
        run(q3, answer_id="test_gold|missing|agentic|gpt-6-luna", trace_id="absent"),
    ]
    scores = {
        r.answer_id: {"class": "gemini_acc", "correctness": [1, 1, 1]} for r in rs
    }
    report, details = failures.analyze_failures(
        rs, scores, {"trace": trace(ids=("a",))}, meta
    )
    g = report["by_system_set"]["agentic|test_gold"]
    assert g["wrong_count"] == g["classification_total"] == 2
    assert g["d1_partial"] == {"count": 1, "ids": ["q"]}
    assert g["indeterminate_by_reason"]["tool_calls"]["ids"] == ["missing"]
    g = report["by_system_set"]["simple|test_gold"]
    assert g["classes"]["structural_tool_absence"]["count"] == 1
    assert g["classes"]["tool_selection"]["count"] == 0
    assert sum(b["wrong_count"] for b in g["by_type"].values()) == g["wrong_count"]
    assert len(details) == 3
    with pytest.raises(ValueError, match="duplicate"):
        failures.analyze_failures([rs[0], rs[0]], scores, {}, meta)


def test_m7_failures_trace_files_and_duplicate(tmp_path):
    folder = tmp_path / "logs/traces"
    folder.mkdir(parents=True)
    p = folder / "day.jsonl"
    p.write_text(json.dumps({"trace_id": "one", "steps": []}) + "\n")
    records, hashes = failures.load_traces(tmp_path)
    assert records["one"]["steps"] == []
    assert set(hashes) == {"logs/traces/day.jsonl"}
    p.write_text(p.read_text() * 2)
    with pytest.raises(ValueError, match="duplicate trace_id"):
        failures.load_traces(tmp_path)


def test_m7_failures_tool_arguments_are_agentic_only(question, meta):
    r = run(question, system="simple")
    result = classify(r, question, trace(error="invalid_argument"), meta)
    assert result.category == "indeterminate"
    assert result.missing == ("candidates_50",)
