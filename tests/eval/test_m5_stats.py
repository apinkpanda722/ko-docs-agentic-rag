"""Tests for deterministic M5 bootstrap statistics."""

import math

import pytest

from kodocs.eval.m5_stats import (
    BootstrapResult,
    describe,
    independent_bootstrap,
    paired_bootstrap,
    verdict,
)


def test_paired_bootstrap_equal_vectors_have_zero_interval_and_hold_verdict() -> None:
    result = paired_bootstrap([0.0, 0.5, 1.0], [0.0, 0.5, 1.0], n_boot=200)

    assert result == BootstrapResult(
        n=3,
        mean_diff=0.0,
        ci_low=0.0,
        ci_high=0.0,
        n_boot=200,
        seed=20260930,
    )
    assert verdict(result) == "판정 보류(차이 입증 못 함)"


def test_paired_bootstrap_verdict_checks_the_preregistered_direction() -> None:
    positive = paired_bootstrap([2.0, 3.0, 4.0], [1.0, 2.0, 3.0], n_boot=100)
    negative = paired_bootstrap([1.0, 2.0, 3.0], [2.0, 3.0, 4.0], n_boot=100)

    assert positive.ci_low > 0
    assert verdict(positive) == "채택"
    assert verdict(positive, direction="negative") == "기각(반대 방향)"
    assert negative.ci_high < 0
    assert verdict(negative) == "기각(반대 방향)"
    assert verdict(negative, direction="negative") == "채택"


def test_paired_bootstrap_is_deterministic_for_the_same_seed() -> None:
    first = paired_bootstrap([0.0, 1.0, 5.0], [0.0, 0.0, 0.0], n_boot=100, seed=17)
    second = paired_bootstrap([0.0, 1.0, 5.0], [0.0, 0.0, 0.0], n_boot=100, seed=17)

    assert first == second
    # floor(0.025 * 99) == 2; ceil(0.975 * 99) == 97.
    assert first.ci_low == pytest.approx(1 / 3)
    assert first.ci_high == 5.0


def test_independent_bootstrap_supports_different_group_sizes_deterministically() -> (
    None
):
    first = independent_bootstrap([3.0, 5.0], [0.0, 1.0, 2.0], n_boot=200, seed=7)
    second = independent_bootstrap([3.0, 5.0], [0.0, 1.0, 2.0], n_boot=200, seed=7)

    assert first == second
    assert first.n == 5
    assert first.mean_diff == 3.0
    assert first.ci_low > 0


def test_bootstraps_with_one_observation_have_a_point_interval() -> None:
    paired = paired_bootstrap([3.0], [1.0], n_boot=50)
    independent = independent_bootstrap([3.0], [1.0], n_boot=50)

    assert (paired.ci_low, paired.ci_high) == (2.0, 2.0)
    assert (independent.ci_low, independent.ci_high) == (2.0, 2.0)


def test_bootstrap_input_validation() -> None:
    with pytest.raises(ValueError, match="same length"):
        paired_bootstrap([1.0], [1.0, 2.0])
    with pytest.raises(ValueError, match="must not be empty"):
        paired_bootstrap([], [])
    with pytest.raises(ValueError, match="must not be empty"):
        independent_bootstrap([], [1.0])
    with pytest.raises(ValueError, match="positive"):
        paired_bootstrap([1.0], [0.0], n_boot=0)


def test_describe_reports_sample_statistics_and_singleton_sd_rule() -> None:
    summary = describe([1.0, 2.0, 3.0, 4.0])

    assert summary == {
        "n": 4,
        "mean": 2.5,
        "sd": pytest.approx(math.sqrt(5 / 3)),
        "median": 2.5,
        "min": 1.0,
        "max": 4.0,
    }
    assert describe([7.0]) == {
        "n": 1,
        "mean": 7.0,
        "sd": None,
        "median": 7.0,
        "min": 7.0,
        "max": 7.0,
    }
    with pytest.raises(ValueError, match="must not be empty"):
        describe([])
