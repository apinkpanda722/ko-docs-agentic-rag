"""Validated, hashable Gemini grading configuration."""

import hashlib
import json
import math
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path


class ConfigError(ValueError):
    """Invalid grading configuration; refuse before sending a request."""


@dataclass(frozen=True)
class GradeModelConfig:
    """Generation settings and explicit paid-tier pacing, independent of quotas."""

    model: str
    tier: str = "paid"
    thinking_level: str | None = None
    temperature: float | None = None
    seed: int | None = None
    max_output_tokens: int = 4000
    request_timeout_s: int = 60
    max_attempts: int = 3
    rpm_pacing: int = 10
    rpm: int | None = None
    tpm: int | None = None
    rpd: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ConfigError("model must be a non-empty string")
        if self.tier != "paid":
            raise ConfigError("grading requires tier=paid")
        if self.thinking_level not in {None, "minimal", "low", "medium", "high"}:
            raise ConfigError(
                "thinking_level must be minimal, low, medium, high or empty"
            )
        if self.temperature is not None and (
            isinstance(self.temperature, bool)
            or not isinstance(self.temperature, (int, float))
            or not math.isfinite(self.temperature)
            or not 0 <= self.temperature <= 2
        ):
            raise ConfigError("temperature must be a finite number in [0, 2]")
        if self.seed is not None and (
            isinstance(self.seed, bool) or not isinstance(self.seed, int)
        ):
            raise ConfigError("seed must be an integer or empty")
        for name in (
            "max_output_tokens",
            "request_timeout_s",
            "max_attempts",
            "rpm_pacing",
            "rpm",
            "tpm",
            "rpd",
        ):
            value = getattr(self, name)
            if value is None and name in {"rpm", "tpm", "rpd"}:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ConfigError(f"{name} must be a positive integer")

    def record(self) -> dict[str, object]:
        return asdict(self)

    def settings_hash(self) -> str:
        payload = json.dumps(self.record(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def load_grade_config(path: Path | None = None) -> GradeModelConfig:
    """Empty TOML values represent null; official quotas remain unknown."""
    with (path or Path(__file__).with_name("models.toml")).open("rb") as source:
        document = tomllib.load(source)
    try:
        raw = dict(document["grade"]["gemini"])
        return GradeModelConfig(
            **{key: None if value == "" else value for key, value in raw.items()}
        )
    except (KeyError, TypeError) as exc:
        raise ConfigError("Invalid grade.gemini settings table") from exc
