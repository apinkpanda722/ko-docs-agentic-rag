"""Load API credentials and the monthly API spending limit."""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from dotenv import load_dotenv

KEY_ENV_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "UPSTAGE_API_KEY",
    "LAW_OPEN_API_KEY",
)

DEFAULT_MONTHLY_BUDGET_USD = 45.0
_MONTHLY_BUDGET_ENV_VAR = "MONTHLY_BUDGET_USD"


class MissingAPIKeyError(RuntimeError):
    """Raised when an API key required for an operation is unavailable."""


@dataclass(frozen=True)
class Settings:
    """Available API keys and the monthly limit, with secrets hidden from repr."""

    api_keys: Mapping[str, str] = field(repr=False)
    monthly_budget_usd: float

    def require_key(self, name: str) -> str:
        """Return a configured API key, failing only when it is needed."""
        if name not in KEY_ENV_VARS:
            expected_names = ", ".join(KEY_ENV_VARS)
            raise ValueError(
                f"Unknown API key environment variable {name!r}; "
                f"expected one of: {expected_names}"
            )

        value = self.api_keys.get(name)
        if value is None:
            raise MissingAPIKeyError(
                f"Required API key environment variable {name} is missing. "
                "See .env.example for setup instructions."
            )
        return value


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Load settings without requiring API keys to be present.

    An explicit ``env`` mapping is isolated from both ``.env`` and the process
    environment. Without one, python-dotenv loads ``.env`` into ``os.environ``.
    """
    if env is None:
        load_dotenv()
        source: Mapping[str, str] = os.environ
    else:
        source = env

    api_keys = {
        name: value
        for name in KEY_ENV_VARS
        if (value := source.get(name)) is not None and value.strip()
    }
    monthly_budget_usd = _parse_monthly_budget(source.get(_MONTHLY_BUDGET_ENV_VAR))

    return Settings(
        api_keys=api_keys,
        monthly_budget_usd=monthly_budget_usd,
    )


def _parse_monthly_budget(raw_value: str | None) -> float:
    if raw_value is None or not raw_value.strip():
        return DEFAULT_MONTHLY_BUDGET_USD

    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(
            f"{_MONTHLY_BUDGET_ENV_VAR} must be a non-negative finite number"
        ) from exc

    if not math.isfinite(value) or value < 0:
        raise ValueError(
            f"{_MONTHLY_BUDGET_ENV_VAR} must be a non-negative finite number"
        )
    return value
