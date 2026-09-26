"""Configuration loading and secret-handling tests."""

import re
from pathlib import Path

import pytest

from kodocs.config import (
    DEFAULT_MONTHLY_BUDGET_USD,
    KEY_ENV_VARS,
    MissingAPIKeyError,
    load_settings,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUDGET_ENV_VAR = "MONTHLY_BUDGET_USD"
CONFIG_ENV_VARS = {*KEY_ENV_VARS, BUDGET_ENV_VAR}


def test_load_settings_succeeds_without_api_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loading settings alone must not require developer API credentials."""
    for name in CONFIG_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("kodocs.config.load_dotenv", lambda: False)

    settings = load_settings()

    assert settings.api_keys == {}
    assert settings.monthly_budget_usd == DEFAULT_MONTHLY_BUDGET_USD


def test_load_settings_reads_present_api_keys() -> None:
    settings = load_settings(
        {
            "ANTHROPIC_API_KEY": "anthropic-secret",
            "OPENAI_API_KEY": "openai-secret",
            "GEMINI_API_KEY": "gemini-secret",
            "UPSTAGE_API_KEY": "upstage-secret",
            "LAW_OPEN_API_KEY": "law-secret",
        }
    )

    assert settings.api_keys == {
        "ANTHROPIC_API_KEY": "anthropic-secret",
        "OPENAI_API_KEY": "openai-secret",
        "GEMINI_API_KEY": "gemini-secret",
        "UPSTAGE_API_KEY": "upstage-secret",
        "LAW_OPEN_API_KEY": "law-secret",
    }


def test_explicit_env_is_isolated_from_dotenv_and_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "process-secret")

    def fail_if_called() -> None:
        pytest.fail("load_dotenv must not be called when env is provided")

    monkeypatch.setattr("kodocs.config.load_dotenv", fail_if_called)

    assert load_settings({}).api_keys == {}


def test_empty_api_key_is_treated_as_missing() -> None:
    settings = load_settings({"OPENAI_API_KEY": ""})

    assert settings.api_keys == {}


def test_require_key_returns_present_value() -> None:
    settings = load_settings({"OPENAI_API_KEY": "openai-secret"})

    assert settings.require_key("OPENAI_API_KEY") == "openai-secret"


def test_require_key_explains_missing_environment_variable() -> None:
    settings = load_settings({})

    with pytest.raises(MissingAPIKeyError) as exc_info:
        settings.require_key("OPENAI_API_KEY")

    message = str(exc_info.value)
    assert "OPENAI_API_KEY" in message
    assert ".env.example" in message


def test_require_key_rejects_unknown_name() -> None:
    settings = load_settings({})

    with pytest.raises(ValueError, match="OPENAI_APY_KEY"):
        settings.require_key("OPENAI_APY_KEY")


@pytest.mark.parametrize("value", [None, ""])
def test_budget_defaults_when_unset_or_empty(value: str | None) -> None:
    env = {} if value is None else {BUDGET_ENV_VAR: value}

    assert load_settings(env).monthly_budget_usd == DEFAULT_MONTHLY_BUDGET_USD


def test_budget_reads_numeric_value() -> None:
    assert load_settings({BUDGET_ENV_VAR: "42.5"}).monthly_budget_usd == 42.5


@pytest.mark.parametrize("value", ["thirty-five", "-0.01", "nan", "inf", "-inf"])
def test_budget_rejects_invalid_or_unsafe_value(value: str) -> None:
    with pytest.raises(ValueError, match=BUDGET_ENV_VAR):
        load_settings({BUDGET_ENV_VAR: value})


def test_settings_repr_and_str_do_not_expose_api_key_values() -> None:
    secret = "sk-should-not-appear"
    settings = load_settings({"OPENAI_API_KEY": secret})

    assert secret not in repr(settings)
    assert secret not in str(settings)


def test_env_example_and_config_variable_names_match() -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    declared_names = set(
        re.findall(r"^([A-Z][A-Z0-9_]*)=", env_example, flags=re.MULTILINE)
    )

    assert declared_names == CONFIG_ENV_VARS
