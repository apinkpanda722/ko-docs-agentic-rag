"""Offline contracts for the demonstration adapter and runtime."""

import importlib
import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from dotenv import load_dotenv

from kodocs import config
from kodocs.agent.types import AnswerResult, Citation
from kodocs.config import MissingAPIKeyError
from kodocs.costs import BudgetExceededError
from kodocs.ui import service
from kodocs.ui.users import DemoUser, load_demo_users

ROOT = Path(__file__).resolve().parents[2]


def test_streamlit_is_installed():
    assert importlib.util.find_spec("streamlit"), (
        "사용자가 `uv add --group ui streamlit` 실행 필요"
    )
    import streamlit

    assert streamlit.__version__


def test_demo_users_match_manifest_without_hashes():
    path = ROOT / "data/manifest/mcp_token_hashes.json"
    expected = {(u["team"], u["grade"]) for u in json.loads(path.read_text())["users"]}
    users = load_demo_users(path)
    assert len(users) == len(expected)
    assert {(u.team, u.grade) for u in users} == expected
    assert all(set(asdict(u)) == {"label", "team", "grade"} for u in users)
    assert all(u.to_user_context().team == u.team for u in users)
    source = (ROOT / "src/kodocs/ui/users.py").read_text()
    assert "tokens.json" not in source
    assert "token_sha256" not in source


@pytest.mark.parametrize("field", ["label", "team", "grade"])
def test_missing_manifest_field_is_clear(tmp_path, field):
    raw = {"label": "demo", "team": "개발", "grade": 1}
    del raw[field]
    path = tmp_path / "users.json"
    path.write_text(json.dumps({"users": [raw]}))
    with pytest.raises(ValueError, match=field):
        load_demo_users(path)


@pytest.mark.parametrize("raw", [[], {}, {"users": []}, {"users": [{}]}])
def test_invalid_manifest_is_clear(tmp_path, raw):
    path = tmp_path / "users.json"
    path.write_text(json.dumps(raw))
    with pytest.raises((ValueError, TypeError)):
        load_demo_users(path)


def result(*, refused=False):
    return AnswerResult(
        "확인 가능한 근거가 없습니다." if refused else "답변입니다.",
        (Citation(kind="doc", doc_id="doc-a", section_key="제1조"),),
        refused,
        "model_refused" if refused else None,
        {
            "model": {"id": "demo-model"},
            "timings": {"total_ms": 250},
            "steps": [
                {
                    "kind": "tool",
                    "name": "search_docs",
                    "args": {"query": "질문"},
                    "latency_ms": 20,
                    "tokens": {"input": 0},
                    "cost_usd": 0,
                    "refs": [
                        {"doc_id": "doc-a", "section_key": "제1조", "title": "규정"}
                    ],
                },
                {"kind": "model", "cost_usd": 0.012},
                {"kind": "model", "cost_usd": 0.003},
            ],
        },
    )


def test_ask_calls_answer_with_server_context_and_maps_trace(monkeypatch):
    runtime = object()
    monkeypatch.setattr(service, "build_default_runtime", lambda: runtime)
    answer = Mock(return_value=result())
    monkeypatch.setattr(service, "_answer_fn", answer)
    user = DemoUser("demo", "개발", 2)
    actual = service.ask("질문", user, "simple")
    answer.assert_called_once_with(
        "질문", user.to_user_context(), system="simple", runtime=runtime
    )
    assert actual.answer == "답변입니다."
    assert actual.cost_usd == pytest.approx(0.015)
    assert actual.total_ms == 250
    assert actual.model == "demo-model"
    assert actual.citations[0].title == "규정"
    assert actual.tools[0].name == "search_docs"
    assert actual.tools[0].latency_ms == 20
    assert actual.tools[0].tokens == {"input": 0}
    assert "질문" in actual.tools[0].args_summary


@pytest.mark.parametrize(
    ("exception", "message"),
    [
        (MissingAPIKeyError("private-value"), "API 키"),
        (BudgetExceededError("private-value"), "월 비용 상한"),
        (httpx.ConnectError("private-value"), "OpenSearch"),
        (RuntimeError("private-value"), "오류가 발생"),
    ],
)
def test_errors_never_show_exception_values(monkeypatch, exception, message):
    monkeypatch.setattr(service, "build_default_runtime", lambda: object())
    monkeypatch.setattr(service, "_answer_fn", Mock(side_effect=exception))
    with pytest.raises(service.UiError) as caught:
        service.ask("질문", DemoUser("demo", "개발", 1), "agentic")
    assert message in str(caught.value)
    assert "private-value" not in str(caught.value)


def test_runtime_loads_real_settings_from_isolated_dotenv(tmp_path, monkeypatch):
    from kodocs.agent import runtime as runtime_module

    runtime_module.build_default_runtime.cache_clear()
    for name in (*config.KEY_ENV_VARS, "MONTHLY_BUDGET_USD"):
        monkeypatch.delenv(name, raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("OPENAI_API_KEY=test-placeholder\nMONTHLY_BUDGET_USD=7.5\n")
    dotenv_calls = []

    def isolated_dotenv():
        dotenv_calls.append(True)
        return load_dotenv(env_path)

    monkeypatch.setattr(config, "load_dotenv", isolated_dotenv)
    # monkeypatch restores the environment after dotenv mutates it, too.
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.setenv("MONTHLY_BUDGET_USD", "")
    monkeypatch.delenv("MONTHLY_BUDGET_USD")
    seen = []

    def fake_chat(model, settings, **kwargs):
        seen.append(settings)
        assert kwargs["run_id"] == "streamlit-ui"
        return object()

    monkeypatch.setattr(runtime_module, "ManagedChat", fake_chat)
    deps = object()
    monkeypatch.setattr(runtime_module, "build_tool_deps", lambda **kwargs: deps)
    try:
        runtime = runtime_module.build_default_runtime()
        assert dotenv_calls == [True]
        assert seen[0].require_key("OPENAI_API_KEY") == "test-placeholder"
        assert seen[0].monthly_budget_usd == 7.5
        assert runtime.deps is deps
        frozen = json.loads((ROOT / "data/manifest/m6_config.json").read_text())
        assert runtime.model_record["model"] == frozen["answer"]["model"]
        assert (
            runtime.model_record["settings_hash"] == frozen["answer"]["settings_hash"]
        )
        assert runtime.guardrails.record() == frozen["guardrails"]["settings"]
        assert runtime.m5_config_hash == frozen["m5_config_hash"]
        assert runtime_module.build_default_runtime() is runtime
    finally:
        runtime_module.build_default_runtime.cache_clear()


def test_missing_key_fails_before_dependency_initialization(monkeypatch):
    from kodocs.agent import runtime as runtime_module

    runtime_module.build_default_runtime.cache_clear()
    monkeypatch.setattr(config, "load_dotenv", lambda: None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    deps = Mock(side_effect=AssertionError("must not initialize dependencies"))
    monkeypatch.setattr(runtime_module, "build_tool_deps", deps)
    with pytest.raises(MissingAPIKeyError):
        runtime_module.build_default_runtime()
    deps.assert_not_called()
