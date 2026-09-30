"""Pre-registered per-question limits for both answer systems."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

from kodocs.agent.types import RefuseReason, canonical_hash


@dataclass(frozen=True)
class GuardrailConfig:
    """M6 limits fixed by ADR-029 before answer experiments run."""

    max_steps: int = 5
    max_tokens_per_question: int = 100_000
    request_timeout_s: int = 30
    simple_timeout_s: int = 45
    agentic_timeout_s: int = 120
    max_output_tokens: int = 4_000
    context_top_k: int = 8
    context_char_budget: int = 6_000

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    def record(self) -> dict[str, int]:
        """Return every limit for traces and frozen experiment settings."""
        return asdict(self)

    def settings_hash(self) -> str:
        """Return a stable digest of the complete guardrail configuration."""
        return canonical_hash(self.record())


class Guard:
    """Mutable counters and deadline belonging to exactly one question."""

    def __init__(
        self,
        config: GuardrailConfig,
        *,
        timeout_s: float | None = None,
        deadline: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout_s is not None and deadline is not None:
            raise ValueError("set timeout_s or deadline, not both")
        if timeout_s is not None and (isinstance(timeout_s, bool) or timeout_s <= 0):
            raise ValueError("timeout_s must be positive")
        self.config = config
        self._clock = clock
        duration = config.agentic_timeout_s if timeout_s is None else timeout_s
        self.deadline = clock() + duration if deadline is None else deadline
        self.steps_used = 0
        self.tokens_used = 0

    def check_before_model_call(self) -> RefuseReason | None:
        """Return a refusal reason when another model request must not start."""
        if self.tokens_used >= self.config.max_tokens_per_question:
            return "guardrail_tokens"
        if self._clock() >= self.deadline:
            return "guardrail_timeout"
        return None

    def register_tool_calls(self, count: int) -> RefuseReason | None:
        """Reserve parallel or sequential calls atomically before execution."""
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("tool call count must be a non-negative integer")
        if self.steps_used + count > self.config.max_steps:
            return "guardrail_max_steps"
        self.steps_used += count
        return None

    def add_usage(self, tokens: int) -> None:
        """Accumulate one completed model call's configured token total."""
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            raise ValueError("token usage must be a non-negative integer")
        self.tokens_used += tokens
