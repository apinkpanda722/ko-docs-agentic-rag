"""Tests for OpenAI answer-model settings."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from kodocs.llm.answer_settings import AnswerModelConfig, load_answer_config


@pytest.mark.parametrize(
    ("name", "model"),
    [("luna", "gpt-6-luna"), ("sol", "gpt-6.1-sol")],
)
def test_load_answer_config_keeps_unsupported_sampling_null(
    name: str,
    model: str,
) -> None:
    config = load_answer_config(name)

    assert config == AnswerModelConfig(
        model=model,
        api="responses",
        effort="low",
        temperature=None,
        max_output_tokens=4000,
        request_timeout_s=30,
        max_attempts=3,
        max_input_tokens=272_000,
    )
    assert config.record() == {
        "api": "responses",
        "effort": "low",
        "max_attempts": 3,
        "max_input_tokens": 272_000,
        "max_output_tokens": 4000,
        "model": model,
        "request_timeout_s": 30,
        "temperature": None,
    }
    assert len(config.settings_hash()) == 64


def test_answer_config_validates_nested_section_and_is_frozen(tmp_path: Path) -> None:
    settings_path = tmp_path / "models.toml"
    settings_path.write_text(
        """
[answer.bad]
model = "gpt-test"
api = "chat_completions"
effort = "low"
temperature = ""
max_output_tokens = 4000
request_timeout_s = 30
max_attempts = 3
max_input_tokens = 272000
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="api"):
        load_answer_config("bad", settings_path)

    config = load_answer_config("luna")
    with pytest.raises(FrozenInstanceError):
        config.model = "changed"  # type: ignore[misc]
