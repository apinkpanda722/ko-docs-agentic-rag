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

    reports = tmp_path / "docs/results"
    reports.mkdir(parents=True)
    (reports / "report.md").write_text(
        "# 결과표 본문\n\n| 지표 | 값 |\n|---|---|\n| 정확도 | 1 |"
    )
    (reports / "report-appendix.md").write_text("# 부록 본문")
    monkeypatch.setattr(views, "RESULTS_DIR", reports)
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
    assert [t.label for t in app.tabs] == ["질의", "결과표"]
    assert calls == []
    assert app.selectbox(key="system").value == "agentic"
    app.text_area(key="question").set_value("같은 질문")
    app.button(key="compare").click().run()
    assert not app.exception
    assert len(calls) == 2
    assert calls[0][0] == calls[1][0] == "같은 질문"
    assert calls[0][1] != calls[1][1]
    columns = app.tabs[0].columns
    assert "답변입니다." in markdown_text(columns[0])
    assert "doc-a" in markdown_text(columns[0])
    assert "규정" in markdown_text(columns[0])
    assert "확인 가능한 근거가 없습니다." in markdown_text(columns[1])
    assert "doc-a" not in markdown_text(columns[1])
    assert "인용" not in markdown_text(columns[1])
    assert "권한" not in markdown_text(columns[1])
    assert "model_refused" not in "\n".join(x.value for x in columns[1].markdown)
    assert "search_docs" in markdown_text(columns[1].expander[0])
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


def test_results_viewer_excludes_appendix_and_renders(app, monkeypatch):
    answer = Mock()
    monkeypatch.setattr(service, "_answer_fn", answer)
    app.run()
    assert app.selectbox(key="report").options == ["report.md"]
    assert "결과표 본문" in markdown_text(app.tabs[1])
    app.checkbox(key="include_appendix").check().run()
    assert app.selectbox(key="report").options == ["report-appendix.md", "report.md"]
    app.selectbox(key="report").select("report-appendix.md").run()
    assert "부록 본문" in markdown_text(app.tabs[1])
    answer.assert_not_called()


def test_viewer_handles_empty_directory(app, tmp_path, monkeypatch):
    from kodocs.ui import views

    monkeypatch.setattr(views, "RESULTS_DIR", tmp_path)
    app.run()
    assert not app.exception
    assert app.info
