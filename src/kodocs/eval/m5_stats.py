"""Deterministic standard-library statistics for M5 retrieval comparisons."""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class BootstrapResult:
    """A mean difference and its percentile bootstrap interval."""

    n: int
    mean_diff: float
    ci_low: float
    ci_high: float
    n_boot: int
    seed: int


def paired_bootstrap(
    a: Sequence[float],
    b: Sequence[float],
    *,
    n_boot: int = 10_000,
    seed: int = 20260930,
) -> BootstrapResult:
    """Bootstrap the mean of paired item differences ``a_i - b_i``.

    The percentile interval sorts the ``n_boot`` sampled means and selects
    ``floor(0.025 * (n_boot - 1))`` for its lower endpoint and
    ``ceil(0.975 * (n_boot - 1))`` for its upper endpoint.  A one-pair input
    therefore produces a point interval at that pair's difference.
    """
    if len(a) != len(b):
        raise ValueError("paired samples must have the same length")
    if not a:
        raise ValueError("paired samples must not be empty")
    _check_n_boot(n_boot)

    differences = tuple(float(left) - float(right) for left, right in zip(a, b))
    rng = random.Random(seed)
    boot_means = [
        math.fsum(differences[rng.randrange(len(differences))] for _ in differences)
        / len(differences)
        for _ in range(n_boot)
    ]
    ci_low, ci_high = _percentile_interval(boot_means)
    return BootstrapResult(
        n=len(differences),
        mean_diff=math.fsum(differences) / len(differences),
        ci_low=ci_low,
        ci_high=ci_high,
        n_boot=n_boot,
        seed=seed,
    )


def independent_bootstrap(
    x: Sequence[float],
    y: Sequence[float],
    *,
    n_boot: int = 10_000,
    seed: int = 20260930,
) -> BootstrapResult:
    """Bootstrap ``mean(x) - mean(y)`` by resampling each group separately.

    The result's ``n`` is the total number of observations across both groups.
    The percentile interval uses sorted indices
    ``floor(0.025 * (n_boot - 1))`` and
    ``ceil(0.975 * (n_boot - 1))``.  Two singleton groups therefore produce a
    point interval at their observed difference.
    """
    if not x or not y:
        raise ValueError("independent samples must not be empty")
    _check_n_boot(n_boot)

    left = tuple(float(value) for value in x)
    right = tuple(float(value) for value in y)
    rng = random.Random(seed)
    boot_differences: list[float] = []
    for _ in range(n_boot):
        left_mean = math.fsum(
            left[rng.randrange(len(left))] for _ in range(len(left))
        ) / len(left)
        right_mean = math.fsum(
            right[rng.randrange(len(right))] for _ in range(len(right))
        ) / len(right)
        boot_differences.append(left_mean - right_mean)
    ci_low, ci_high = _percentile_interval(boot_differences)
    return BootstrapResult(
        n=len(left) + len(right),
        mean_diff=math.fsum(left) / len(left) - math.fsum(right) / len(right),
        ci_low=ci_low,
        ci_high=ci_high,
        n_boot=n_boot,
        seed=seed,
    )


def describe(values: Sequence[float]) -> dict[str, int | float | None]:
    """Return count, mean, sample SD, median, minimum, and maximum."""
    if not values:
        raise ValueError("values must not be empty")
    numbers = tuple(float(value) for value in values)
    return {
        "n": len(numbers),
        "mean": math.fsum(numbers) / len(numbers),
        "sd": statistics.stdev(numbers) if len(numbers) >= 2 else None,
        "median": statistics.median(numbers),
        "min": min(numbers),
        "max": max(numbers),
    }


def verdict(
    result: BootstrapResult,
    *,
    direction: Literal["positive", "negative"] = "positive",
) -> str:
    """Apply the preregistered directional decision rule to a bootstrap CI."""
    if direction == "positive":
        if result.ci_low > 0:
            return "채택"
        if result.ci_high < 0:
            return "기각(반대 방향)"
    elif direction == "negative":
        if result.ci_high < 0:
            return "채택"
        if result.ci_low > 0:
            return "기각(반대 방향)"
    else:
        raise ValueError("direction must be 'positive' or 'negative'")
    return "판정 보류(차이 입증 못 함)"


def _percentile_interval(values: list[float]) -> tuple[float, float]:
    ordered = sorted(values)
    last = len(ordered) - 1
    low_index = math.floor(0.025 * last)
    high_index = math.ceil(0.975 * last)
    return ordered[low_index], ordered[high_index]


def _check_n_boot(n_boot: int) -> None:
    if isinstance(n_boot, bool) or not isinstance(n_boot, int) or n_boot <= 0:
        raise ValueError("n_boot must be a positive integer")
