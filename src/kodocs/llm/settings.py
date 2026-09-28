"""Validated generation settings shared by LLM wrappers and manifests."""

from __future__ import annotations

import hashlib
import json
import math
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

Thinking = Literal["disabled", "adaptive"]


@dataclass(frozen=True)
class GenerationConfig:
    """Output-determining generation settings for one model role."""

    model: str
    max_tokens: int
    thinking: Thinking
    effort: str | None
    temperature: float | None
    max_attempts: int
    pilot_limit: int
    chars_per_token_estimate: float

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("model must not be empty")
        if isinstance(self.max_tokens, bool) or self.max_tokens <= 0:
            raise ValueError("max_tokens must be a positive integer")
        if self.thinking not in {"disabled", "adaptive"}:
            raise ValueError("thinking must be 'disabled' or 'adaptive'")
        if self.effort not in {None, "low", "medium", "high"}:
            raise ValueError("effort must be low, medium, high, or empty")
        if self.temperature is not None and (
            isinstance(self.temperature, bool) or not math.isfinite(self.temperature)
        ):
            raise ValueError("temperature must be finite or empty")
        if isinstance(self.max_attempts, bool) or self.max_attempts <= 0:
            raise ValueError("max_attempts must be a positive integer")
        if isinstance(self.pilot_limit, bool) or self.pilot_limit <= 0:
            raise ValueError("pilot_limit must be a positive integer")
        if (
            isinstance(self.chars_per_token_estimate, bool)
            or not math.isfinite(self.chars_per_token_estimate)
            or self.chars_per_token_estimate <= 0
        ):
            raise ValueError("chars_per_token_estimate must be positive and finite")

    def request_params(self) -> dict[str, object]:
        """Return only settings documented as supported by the configured model."""
        params: dict[str, object] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "thinking": {"type": self.thinking},
        }
        if self.effort is not None:
            params["output_config"] = {"effort": self.effort}
        if self.temperature is not None:
            params["temperature"] = self.temperature
        return params

    def record(self) -> dict[str, object]:
        """Return generation metadata safe to persist with outputs and manifests."""
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "thinking": self.thinking,
            "effort": self.effort,
            "temperature": self.temperature,
            "sampling": "unsupported_by_model",
        }

    def settings_hash(self) -> str:
        """Return a stable digest of every configured generation setting."""
        serialized = json.dumps(
            asdict(self),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def load_generation_config(
    path: Path | None = None,
    section: str = "synth",
) -> GenerationConfig:
    """Load one generation section from the repository TOML settings."""
    settings_path = path or Path(__file__).with_name("models.toml")
    with settings_path.open("rb") as source:
        document = tomllib.load(source)
    raw = document.get(section)
    if raw is None:
        raise ValueError(f"Generation settings section not found: {section}")
    if not isinstance(raw, Mapping):
        raise TypeError(f"Generation settings section must be a table: {section}")

    effort = _optional_string(raw.get("effort"), name="effort")
    temperature = _optional_float(raw.get("temperature"), name="temperature")
    return GenerationConfig(
        model=_required_string(raw.get("model"), name="model"),
        max_tokens=_required_int(raw.get("max_tokens"), name="max_tokens"),
        thinking=_thinking(raw.get("thinking")),
        effort=effort,
        temperature=temperature,
        max_attempts=_required_int(raw.get("max_attempts"), name="max_attempts"),
        pilot_limit=_required_int(raw.get("pilot_limit"), name="pilot_limit"),
        chars_per_token_estimate=_required_float(
            raw.get("chars_per_token_estimate"),
            name="chars_per_token_estimate",
        ),
    )


def _required_string(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_string(value: object, *, name: str) -> str | None:
    if value == "":
        return None
    return _required_string(value, name=name)


def _required_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value


def _required_float(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    return float(value)


def _optional_float(value: object, *, name: str) -> float | None:
    if value == "":
        return None
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be a number or empty") from exc
    return _required_float(value, name=name)


def _thinking(value: object) -> Thinking:
    if value not in {"disabled", "adaptive"}:
        raise ValueError("thinking must be 'disabled' or 'adaptive'")
    return value  # type: ignore[return-value]
