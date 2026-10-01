"""Validated settings for OpenAI answer models."""

from __future__ import annotations

import hashlib
import json
import math
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

AnswerAPI = Literal["responses"]
ReasoningEffort = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class AnswerModelConfig:
    """Request and limit settings shared by one OpenAI answer model."""

    model: str
    api: AnswerAPI
    effort: ReasoningEffort
    temperature: float | None
    max_output_tokens: int
    request_timeout_s: int
    max_attempts: int
    max_input_tokens: int

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("model must not be empty")
        if self.api != "responses":
            raise ValueError("api must be 'responses'")
        if self.effort not in {"low", "medium", "high"}:
            raise ValueError("effort must be low, medium, or high")
        if self.temperature is not None and (
            isinstance(self.temperature, bool) or not math.isfinite(self.temperature)
        ):
            raise ValueError("temperature must be finite or empty")
        for name in (
            "max_output_tokens",
            "request_timeout_s",
            "max_attempts",
            "max_input_tokens",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    def record(self) -> dict[str, object]:
        """Return the complete non-secret configuration persisted with calls."""
        return asdict(self)

    def settings_hash(self) -> str:
        """Return a stable digest of every configured answer setting."""
        serialized = json.dumps(
            asdict(self),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def load_answer_config(
    name: str,
    path: Path | None = None,
) -> AnswerModelConfig:
    """Load one ``answer.<name>`` table from the repository model settings."""
    if not name:
        raise ValueError("answer model name must not be empty")
    settings_path = path or Path(__file__).with_name("models.toml")
    with settings_path.open("rb") as source:
        document = tomllib.load(source)

    answer = document.get("answer")
    if answer is None:
        raise ValueError("Answer settings table not found: answer")
    if not isinstance(answer, Mapping):
        raise TypeError("Answer settings must be a table: answer")
    raw = answer.get(name)
    if raw is None:
        raise ValueError(f"Answer settings section not found: answer.{name}")
    if not isinstance(raw, Mapping):
        raise TypeError(f"Answer settings section must be a table: answer.{name}")

    temperature = _optional_float(raw.get("temperature"), name="temperature")
    return AnswerModelConfig(
        model=_required_string(raw.get("model"), name="model"),
        api=_required_string(raw.get("api"), name="api"),  # type: ignore[arg-type]
        effort=_required_string(  # type: ignore[arg-type]
            raw.get("effort"), name="effort"
        ),
        temperature=temperature,
        max_output_tokens=_required_int(
            raw.get("max_output_tokens"), name="max_output_tokens"
        ),
        request_timeout_s=_required_int(
            raw.get("request_timeout_s"), name="request_timeout_s"
        ),
        max_attempts=_required_int(raw.get("max_attempts"), name="max_attempts"),
        max_input_tokens=_required_int(
            raw.get("max_input_tokens"), name="max_input_tokens"
        ),
    )


def _required_string(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _required_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value


def _optional_float(value: object, *, name: str) -> float | None:
    if value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number or empty")
    return float(value)
