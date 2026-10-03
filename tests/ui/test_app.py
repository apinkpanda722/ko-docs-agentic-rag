"""Headless UI tests; every answer and dependency is offline."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest
from test_users_service import result

from kodocs.config import MissingAPIKeyError
from kodocs.ui import service
from kodocs.ui.users import DemoUser

APP = Path(__file__).resolve().parents[2] / "src/kodocs/ui/app.py"


@pytest.fixture
def app(tmp_path, monkeypatch):
    from kodocs.ui import views

    monkeypatch.setattr(
        views,
        "load_demo_users",
        lambda: (DemoUser("A", "개발", 2), DemoUser("B", "지원", 1)),
    )
    monkeypatch.setattr(service, "build_default_runtime", lambda: object())
    return AppTest.from_file(APP, default_timeout=10)


def markdown_text(tree):
    return "\n".join(element.value for element in tree.markdown)


def test_comparison_and_session_cost(app, monkeypatch):
    calls = []

    def answer(question, user, **kwargs):
        calls.append((question, user, kwargs))
        return result(refused=user.team == "지원")

    monkeypatch.setattr(service, "_answer_fn", answer)
    app.run(timeout=10)
    assert not app.exception
    assert len(app.tabs) == 0  # query screen only (results tab removed 2026-10-03)
    assert not app.checkbox and not [s for s in app.selectbox if s.key == "report"]
    assert calls == []
    assert app.selectbox(key="system").value == "agentic"
    app.text_area(key="question").set_value("같은 질문")
    app.button(key="compare").click().run()
    assert not app.exception
    assert len(calls) == 2
    assert calls[0][0] == calls[1][0] == "같은 질문"
    assert calls[0][1] != calls[1][1]
    columns = app.columns
    assert "답변입니다." in markdown_text(columns[0])
    assert "doc-a" in markdown_text(columns[0])
    assert "규정" in markdown_text(columns[0])
    assert "확인 가능한 근거가 없습니다." in markdown_text(columns[1])
    assert "doc-a" not in markdown_text(columns[1])
    assert "인용" not in markdown_text(columns[1])
    assert "권한" not in markdown_text(columns[1])
    assert "model_refused" not in "\n".join(x.value for x in columns[1].markdown)
    assert any("search_docs" in markdown_text(e) for e in columns[1].expander)
    assert any("0.015000" in x.value and "USD" in x.value for x in app.caption)
    assert app.session_state["session_cost_usd"] == pytest.approx(0.03)
    assert app.metric[0].value == "0.030000 USD"
    app.run()
    assert len(calls) == 2
    app.button(key="single").click().run()
    assert len(calls) == 3
    assert app.session_state["session_cost_usd"] == pytest.approx(0.045)
    assert app.metric[0].value == "0.045000 USD"


def test_missing_key_safe_korean_message(app, monkeypatch):
    monkeypatch.setattr(
        service, "_answer_fn", Mock(side_effect=MissingAPIKeyError("private-value"))
    )
    app.run()
    app.text_area(key="question").set_value("질문")
    app.button(key="single").click().run()
    assert not app.exception
    assert "API 키" in app.error[0].value
    assert "private-value" not in str(app)
    assert app.session_state["session_cost_usd"] == 0


def test_blank_question_makes_no_call(app, monkeypatch):
    answer = Mock()
    monkeypatch.setattr(service, "_answer_fn", answer)
    app.run().button(key="single").click().run()
    answer.assert_not_called()
    assert app.warning


def test_only_the_query_screen_is_left(app, monkeypatch):
    from kodocs.ui import views

    answer = Mock()
    monkeypatch.setattr(service, "_answer_fn", answer)
    app.run()
    assert not app.exception
    assert len(app.tabs) == 0
    assert app.text_area(key="question")
    assert not hasattr(views, "results_view") and not hasattr(views, "RESULTS_DIR")
    answer.assert_not_called()


def test_live_events_final_replacement_and_refusal_clear(app, monkeypatch):
    import threading

    from kodocs.agent.types import AgentRuntime

    worker_ids = []

    def fake_answer(question, user, *, runtime, **kwargs):
        worker_ids.append(threading.get_ident())
        runtime.event_sink(
            {
                "kind": "tool_start",
                "name": "get_document",
                "index": 1,
                "args": {},
                "latency_ms": 0,
                "refs_count": 0,
                "error": None,
            }
        )
        runtime.event_sink(
            {
                "kind": "tool_end",
                "name": "get_document",
                "index": 1,
                "args": {},
                "latency_ms": 12,
                "refs_count": 0,
                "error": "not_found",
            }
        )
        runtime.chat.on_text_delta('{"answer":"PROVISIONAL')
        return result(refused=user.team == "지원")

    runtime = AgentRuntime(object(), object(), {}, "fake")
    monkeypatch.setattr(service, "build_default_runtime", lambda: runtime)
    monkeypatch.setattr(service, "_answer_fn", fake_answer)
    app.run()
    app.text_area(key="question").set_value("question")
    app.button(key="compare").click().run()
    assert not app.exception
    assert len(worker_ids) == 2
    assert "PROVISIONAL" not in markdown_text(app)
    assert "답변입니다." in markdown_text(app)
    assert "확인 가능한 근거가 없습니다." in markdown_text(app)
    assert "get_document · 오류(not_found)" in markdown_text(app)


def test_stream_error_retry_same_request_no_fallback(app, monkeypatch):
    from kodocs.llm.openai_chat import ChatParseError

    calls = []

    def failing(question, user, **kwargs):
        calls.append((question, user, kwargs["system"]))
        raise ChatParseError("PRIVATE")

    monkeypatch.setattr(service, "_answer_fn", failing)
    app.run()
    app.text_area(key="question").set_value("original")
    app.button(key="single").click().run()
    assert service.STREAM_ERROR in app.error[0].value
    app.run()
    app.text_area(key="question").set_value("changed")
    app.selectbox(key="system").select("simple")
    app.button(key="retry").click().run()
    assert len(calls) == 2 and calls[0] == calls[1]
    assert "PRIVATE" not in str(app)


def test_service_forwards_attempt_start_to_the_queue(monkeypatch):
    import queue

    from kodocs.agent.types import AgentRuntime

    runtime = AgentRuntime(object(), object(), {}, "fake")
    monkeypatch.setattr(service, "build_default_runtime", lambda: runtime)

    def fake_answer(question, user, *, runtime, **kwargs):
        runtime.chat.on_text_delta('{"answer":"FIRST')
        runtime.chat.on_attempt_start(2)
        runtime.chat.on_text_delta('{"answer":"SECOND')
        return result()

    monkeypatch.setattr(service, "_answer_fn", fake_answer)
    messages = queue.Queue()
    service.ask("q", DemoUser("demo", "플랫폼", 4), "agentic", messages=messages)
    items = []
    while not messages.empty():
        items.append(messages.get())
    assert ("attempt_start", 2) in items
    deltas = [value for kind, value in items if kind == "delta"]
    assert deltas == ['{"answer":"FIRST', '{"answer":"SECOND']
    assert items.index(("attempt_start", 2)) == 1
