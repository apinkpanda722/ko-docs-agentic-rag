import json
from pathlib import Path

import pytest

from kodocs.eval import m8_parse_error as parse
from kodocs.eval.m7_rawhash import RawHashMismatch, fingerprint


def test_counts_classification_and_no_content():
    rows = [
        {
            "question_id": "q",
            "system": "simple",
            "refuse_reason": "parse_error",
            "answer": "PRIVATE",
        }
    ]
    result = parse.analyze(
        rows,
        expected=4,
        model="fixture",
        m6_rows=[{"question_id": "q", "system": "simple", "refuse_reason": None}],
    )
    assert result["count"] == 1 and result["reported_count"] == 4
    assert result["count_matches"] is False
    assert result["class_counts"] == {"indeterminate": 1}
    assert result["items"][0]["m6_parse_error"] is False
    assert result["items"][0]["reason"]
    assert "PRIVATE" not in json.dumps(result)
    assert not {"answer", "raw_output"} & result["items"][0].keys()


def test_trace_correlation_uses_timings_not_question_alone():
    row = {
        "question_id": "q",
        "system": "simple",
        "refuse_reason": "parse_error",
        "embed_s": 0.1,
        "search_s": 0.2,
    }
    trace = {
        "question_id": "q",
        "system": "simple",
        "refuse_reason": "parse_error",
        "timings": {"embed_ms": 100, "search_ms": 200},
        "steps": [{"kind": "model", "error": "ChatParseError"}],
    }
    result = parse.analyze(
        [row],
        expected=1,
        model="m",
        traces=[trace, {**trace, "timings": {"embed_ms": 200, "search_ms": 200}}],
    )
    assert result["items"][0]["matched_traces"] == 1
    assert result["items"][0]["classification"] == "indeterminate"


def test_raw_mismatch_refuses_before_analysis(tmp_path):
    raw = tmp_path / parse.RAW
    raw.parent.mkdir(parents=True)
    raw.write_text("{}\n")
    manifest = tmp_path / parse.LATENCY
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"raw": [{"key": "latency", **fingerprint(raw)}]}))
    raw.write_text("{}\n{}\n")
    with pytest.raises(RawHashMismatch):
        parse.build(tmp_path)
    assert not (tmp_path / parse.OUTPUT).exists()


def test_make_targets():
    root = Path(__file__).resolve().parents[2]
    for target, module in [
        ("adr-index", "m8_adr_index"),
        ("m8-parse-error", "m8_parse_error"),
        ("m8-wrap-report", "m8_wrap_report"),
    ]:
        text = (root / "Makefile").read_text()
        assert target in text.splitlines()[0].split()
        assert f"{target}:\n\tuv run python -m kodocs.eval.{module}" in text
        assert f"make {target}" in (root / "AGENTS.md").read_text()


def test_missing_traces_still_classifies_with_frozen_model(tmp_path):
    raw = tmp_path / parse.RAW
    raw.parent.mkdir(parents=True)
    raw.write_text(
        json.dumps(
            {"question_id": "q", "system": "simple", "refuse_reason": "parse_error"}
        )
        + "\n"
    )
    manifest = tmp_path / parse.LATENCY
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "raw": [{"key": "latency", **fingerprint(raw)}],
                "model_settings_hash": "h",
            }
        )
    )
    (tmp_path / "data/manifest/m6_config.json").write_text(
        json.dumps({"answer": {"model": "fixture", "settings_hash": "h"}})
    )
    report = tmp_path / parse.M7_REPORT
    report.parent.mkdir(parents=True)
    report.write_text("지연 한계 2(스트리밍 parse_error): parse_error 1건")
    result = parse.build(tmp_path)
    assert result["items"][0]["classification"] == "indeterminate"
    assert result["items"][0]["model"] == "fixture"
    assert result["items"][0]["m6_parse_error"] is None


def ledger_request(request_id, output, *, run="m7-latency", latency=1000):
    return [
        {
            "request_id": request_id,
            "run_id": run,
            "service": "openai.fixture." + kind,
            "usage": {"output_tokens": output, "reasoning_tokens": 0},
            "latency_ms": latency,
            "amount_usd": 0.01,
        }
        for kind in ("input", "cached_input", "cache_write", "output")
    ]


def test_ledger_counts_usage_once_and_links_by_request_id():
    row = {
        "question_id": "q",
        "system": "simple",
        "refuse_reason": "parse_error",
        "embed_s": 0.1,
        "search_s": 0.2,
    }
    trace = {
        **{k: row[k] for k in ("question_id", "system", "refuse_reason")},
        "timings": {"embed_ms": 100, "search_ms": 200},
        "steps": [
            {"name": "final_answer", "error": "ChatParseError", "request_id": "r"}
        ],
    }
    result = parse.analyze(
        [row],
        expected=1,
        model="m",
        traces=[trace],
        ledger=ledger_request("r", 4000)
        + ledger_request("other", 30)
        + ledger_request("ref", 80, run="m6-dev"),
        max_output_tokens=4000,
    )
    item = result["items"][0]
    assert item["classification"] == "max_output_tokens_reached"
    assert item["link_method"] == "request_id"
    assert item["output_tokens"] == 4000 and item["reasoning_tokens"] == 0
    assert result["comparisons"]["m7-latency"]["count"] == 2
    assert result["comparisons"]["m7-latency"]["remaining_max_output_tokens"] == 30
    assert result["comparisons"]["m6-dev"]["max_output_tokens"] == 80
    assert result["cause_note"]


def test_order_link_requires_unique_complete_execution_order_and_latency():
    row = {
        "question_id": "q",
        "system": "simple",
        "refuse_reason": "parse_error",
        "embed_s": 0.1,
        "search_s": 0.2,
        "total_s": 1.4,
    }
    trace = {
        **{k: row[k] for k in ("question_id", "system", "refuse_reason")},
        "timings": {"embed_ms": 100, "search_ms": 200, "total_ms": 1400},
        "steps": [
            {
                "kind": "model",
                "name": "final_answer",
                "latency_ms": 1000,
                "error": "ChatParseError",
            }
        ],
    }
    kwargs = dict(  # noqa: C408
        expected=1, model="m", ledger=ledger_request("r", 3999), max_output_tokens=4000
    )
    linked = parse.analyze([row], traces=[trace], **kwargs)["items"][0]
    assert linked["link_method"] == "order_and_latency"
    assert linked["classification"] == "indeterminate"
    unlinked = parse.analyze([row], traces=[trace, trace], **kwargs)["items"][0]
    assert unlinked["link_method"] == "unlinked"
    assert unlinked["classification"] == "indeterminate"
