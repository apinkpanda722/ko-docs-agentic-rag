"""Streaming stays final-only and crosses threads through a queue."""

import threading
from unittest.mock import Mock

import pytest

from kodocs.ui.streaming import (
    FinalStreamingChat,
    PartialText,
    partial_answer,
    run_worker,
)


def test_only_final_call_streams():
    inner = Mock()
    sink = Mock()
    chat = FinalStreamingChat(inner, sink)
    chat.invoke("choose", tools=["tool"])
    assert inner.invoke.call_args.kwargs == {"tools": ["tool"]}
    chat.invoke("final", response_schema=dict)
    assert inner.invoke.call_args.kwargs == {
        "response_schema": dict,
        "stream": True,
        "on_text_delta": sink,
        "regenerate_on_truncation": True,
    }


def test_partial_answer_uses_json_escapes():
    assert partial_answer('{"answer":"hello\\nwo') == "hello\nwo"
    assert partial_answer('{"citations":[') == ""
    assert partial_answer("invalid") == ""


def test_worker_delivers_on_consumer_thread_and_propagates_exception():
    caller = threading.get_ident()
    seen = []

    def work(queue):
        assert threading.get_ident() != caller
        queue.put(("delta", "partial"))
        raise ValueError("private")

    def render(kind, value):
        seen.append((threading.get_ident(), kind, value))

    with pytest.raises(ValueError):
        run_worker(work, render)
    assert seen == [(caller, "delta", "partial")]


def test_callback_does_not_use_streamlit():
    import inspect

    from kodocs.ui import streaming

    assert "import streamlit" not in inspect.getsource(streaming)
    assert "st." not in inspect.getsource(streaming.run_worker)


def test_parallel_tool_events_and_sink_failure_do_not_change_results(
    tmp_path, monkeypatch
):

    from kodocs.agent.evidence import EvidenceLog
    from kodocs.agent.guardrails import Guard, GuardrailConfig
    from kodocs.agent.tools import AgentContext, execute_tool_call
    from kodocs.agent.trace import TraceRecorder
    from kodocs.tools import ToolResult, UserContext
    from kodocs.ui.streaming import event_lines

    events = []
    barrier = threading.Barrier(2)

    def tool(*args):
        barrier.wait(timeout=3)
        return ToolResult(
            {"documents": [], "body": "PRIVATE", "title": "SECRET"}, False, (), {}
        )

    monkeypatch.setattr("kodocs.agent.tools.run_tool", tool)
    trace = TraceRecorder(tmp_path).start(
        question_id="q",
        system="agentic",
        user=UserContext("a", 1),
        model_record={},
        hashes={},
    )
    ctx = AgentContext(
        UserContext("a", 1),
        EvidenceLog(),
        Guard(GuardrailConfig(), timeout_s=45),
        trace,
        event_sink=events.append,
    )
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                execute_tool_call, ctx, object(), "lookup_id", {"identifier": "q"}
            )
            for _ in range(2)
        ]
        assert all(not future.result().is_error for future in futures)
    assert [e["kind"] for e in events] == [
        "tool_start",
        "tool_start",
        "tool_end",
        "tool_end",
    ]
    assert len(event_lines(events)) == 2
    assert all("완료(" in line and "refs 0" in line for line in event_lines(events))
    assert "PRIVATE" not in str(events) and "SECRET" not in str(events)
    with pytest.raises(TypeError):
        events[0]["name"] = "changed"
    with pytest.raises(TypeError):
        events[0]["args"]["identifier"] = "changed"
    assert "guardrail" not in str(events)

    def fails(event):
        raise RuntimeError("private")

    failing = AgentContext(
        ctx.user, ctx.evidence, ctx.guard, ctx.trace, event_sink=fails
    )
    failing.emit("model_start", "agent")
    assert failing._event_state["errors"] == 1


def test_frozen_m6_hash_and_m8_rules_hash_use_existing_functions():
    from pathlib import Path

    from kodocs.eval.m6_config import verify_config
    from kodocs.eval.m8_rules import rules_sha256

    root = Path(__file__).resolve().parents[2]
    assert verify_config(root / "data/manifest/m6_config.json").ok
    assert (
        rules_sha256()
        == "f59ded7f91461eb8d37a43b7429d32bd6c497cf0a3be29ad7c278a159d3d44da"
    )


def test_frozen_m7_config_hash_still_verifies_against_the_local_m6_runs():
    from pathlib import Path

    from kodocs.eval.m7_config import verify_config as verify_m7

    root = Path(__file__).resolve().parents[2]
    if not (root / "data/interim/m6/dev/runs.jsonl").exists():
        pytest.skip("비커밋 M6 실행 원본(data/interim/m6)이 없는 clone에서는 건너뜀")
    assert verify_m7(root=root)["config_hash"]


def test_attempt_start_is_forwarded_only_on_the_final_call():
    inner = Mock()
    sink, on_attempt = Mock(), Mock()
    chat = FinalStreamingChat(inner, sink, on_attempt)
    chat.invoke("choose", tools=["tool"])
    assert inner.invoke.call_args.kwargs == {"tools": ["tool"]}
    chat.invoke("final", response_schema=dict)
    assert inner.invoke.call_args.kwargs == {
        "response_schema": dict,
        "stream": True,
        "on_text_delta": sink,
        "on_attempt_start": on_attempt,
        "regenerate_on_truncation": True,
    }


def test_new_attempt_clears_text_of_the_previous_attempt_on_the_consumer_thread():
    shown = []
    text = PartialText()

    def render(kind, value):
        if kind == "delta":
            text.add(value)
        elif kind == "attempt_start":
            text.reset()
        shown.append(text.answer())

    def work(messages):
        messages.put(("attempt_start", 1))
        messages.put(("delta", '{"answer":"FIRST'))
        messages.put(("attempt_start", 2))
        messages.put(("delta", '{"answer":"SEC'))
        messages.put(("delta", 'OND"}'))
        return "done"

    assert run_worker(work, render) == "done"
    assert shown == ["", "FIRST", "", "SEC", "SECOND"]
    assert "FIRST" not in text.answer()


def test_tool_turns_use_the_evaluation_chat_and_final_turn_uses_the_ui_chat():
    tool_chat, final_chat = Mock(), Mock()
    chat = FinalStreamingChat(tool_chat, Mock(), final_inner=final_chat)
    chat.invoke("choose", tools=["tool"])
    chat.invoke("final", response_schema=dict)
    assert tool_chat.invoke.call_count == 1
    assert "stream" not in tool_chat.invoke.call_args.kwargs
    assert final_chat.invoke.call_count == 1
    assert final_chat.invoke.call_args.kwargs["stream"] is True
