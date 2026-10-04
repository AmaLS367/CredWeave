"""Unit tests for the pure BackoffPolicy value object."""

import math
import random

import pytest

from credweave.domain.backoff import MAX_DELAY_CEILING, BackoffPolicy, RetryAfterMode
from credweave.domain.errors import ConfigurationError


def test_fixed_policy_returns_same_delay_for_every_attempt() -> None:
    policy = BackoffPolicy.fixed(30.0)
    assert [policy.compute_delay(n) for n in (1, 2, 3, 10, 100)] == [30.0] * 5


def test_default_policy_is_fixed_sixty_seconds() -> None:
    policy = BackoffPolicy()
    assert policy.compute_delay(1) == 60.0
    assert policy.compute_delay(5) == 60.0
    assert policy.retry_after_mode is RetryAfterMode.FLOOR


def test_exponential_sequence_with_default_multiplier() -> None:
    policy = BackoffPolicy.exponential(1.0)
    assert [policy.compute_delay(n) for n in range(1, 7)] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0]


def test_exponential_sequence_with_custom_base_and_multiplier() -> None:
    policy = BackoffPolicy.exponential(5.0, multiplier=3.0)
    assert [policy.compute_delay(n) for n in range(1, 5)] == [5.0, 15.0, 45.0, 135.0]


def test_max_delay_caps_exponential_growth() -> None:
    policy = BackoffPolicy.exponential(1.0, multiplier=2.0, max_delay=10.0)
    assert [policy.compute_delay(n) for n in range(1, 8)] == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0, 10.0]


def test_attempt_below_one_is_treated_as_first_attempt() -> None:
    policy = BackoffPolicy.exponential(3.0)
    assert policy.compute_delay(0) == 3.0
    assert policy.compute_delay(-4) == 3.0


def test_huge_attempt_without_cap_never_overflows() -> None:
    policy = BackoffPolicy.exponential(1.0, multiplier=2.0)
    assert policy.compute_delay(5000) == MAX_DELAY_CEILING
    assert math.isfinite(policy.compute_delay(5000))


@pytest.mark.parametrize(
    ("sample", "expected"),
    [(0.0, 100.0), (0.5, 75.0), (0.999999, 50.000025)],
)
def test_jitter_bounds_are_proportional_and_never_exceed_delay(
    sample: float, expected: float
) -> None:
    policy = BackoffPolicy.fixed(100.0, jitter=0.5)
    assert policy.compute_delay(1, lambda: sample) == pytest.approx(expected)


def test_jitter_stays_within_bounds_for_many_draws() -> None:
    policy = BackoffPolicy.exponential(2.0, multiplier=2.0, max_delay=40.0, jitter=0.25)
    rng = random.Random(1234).random
    for attempt in range(1, 9):
        ceiling = min(40.0, 2.0 * 2.0 ** (attempt - 1))
        for _ in range(200):
            delay = policy.compute_delay(attempt, rng)
            assert ceiling * 0.75 <= delay <= ceiling


def test_jitter_never_exceeds_max_delay() -> None:
    policy = BackoffPolicy.exponential(10.0, max_delay=15.0, jitter=1.0)
    rng = random.Random(5).random
    assert all(0.0 <= policy.compute_delay(6, rng) <= 15.0 for _ in range(500))


def test_seeded_jitter_is_deterministic_and_reproducible() -> None:
    policy = BackoffPolicy.exponential(1.0, jitter=0.5)
    run_a = [policy.compute_delay(n, random.Random(99).random) for n in range(1, 6)]
    run_b = [policy.compute_delay(n, random.Random(99).random) for n in range(1, 6)]
    assert run_a == run_b

    reference = random.Random(99)
    expected = [(2.0 ** (n - 1)) * (1.0 - 0.5 * reference.random()) for n in range(1, 6)]
    stream = random.Random(99)
    sequence = [policy.compute_delay(n, stream.random) for n in range(1, 6)]
    assert sequence == pytest.approx(expected)


def test_different_seeds_produce_different_jitter() -> None:
    policy = BackoffPolicy.fixed(100.0, jitter=0.5)
    assert policy.compute_delay(1, random.Random(1).random) != policy.compute_delay(
        1, random.Random(2).random
    )


def test_jitter_is_skipped_without_rng_and_when_disabled() -> None:
    assert BackoffPolicy.fixed(10.0, jitter=0.5).compute_delay(1) == 10.0

    def forbidden() -> float:
        raise AssertionError("rng must not be consulted when jitter is 0")

    assert BackoffPolicy.fixed(10.0).compute_delay(1, forbidden) == 10.0


def test_out_of_range_rng_samples_are_clamped() -> None:
    policy = BackoffPolicy.fixed(10.0, jitter=0.5)
    assert policy.compute_delay(1, lambda: -3.0) == 10.0
    assert policy.compute_delay(1, lambda: 9.0) == 5.0


def test_retry_after_larger_than_calculated_delay_wins() -> None:
    policy = BackoffPolicy.exponential(1.0, max_delay=4.0)
    assert policy.resolve_delay(2, retry_after=30.0) == 30.0


def test_retry_after_smaller_than_calculated_delay_keeps_policy_delay() -> None:
    policy = BackoffPolicy.exponential(1.0, max_delay=100.0)
    assert policy.resolve_delay(5, retry_after=3.0) == 16.0


def test_retry_after_is_never_capped_by_max_delay() -> None:
    policy = BackoffPolicy.exponential(1.0, max_delay=4.0)
    assert policy.resolve_delay(1, retry_after=3600.0) == 3600.0


def test_retry_after_floor_applies_to_jittered_delay() -> None:
    policy = BackoffPolicy.fixed(100.0, jitter=1.0)
    # Jitter would shrink the delay to 0; the hint is still honored as a floor.
    assert policy.resolve_delay(1, retry_after=40.0, rng=lambda: 0.999) == 40.0
    assert policy.resolve_delay(1, retry_after=40.0, rng=lambda: 0.0) == 100.0


def test_retry_after_override_mode_replaces_policy_delay() -> None:
    policy = BackoffPolicy.fixed(60.0, retry_after_mode=RetryAfterMode.OVERRIDE)
    assert policy.resolve_delay(1, retry_after=5.0) == 5.0
    assert policy.resolve_delay(1, retry_after=500.0) == 500.0
    assert policy.resolve_delay(1, retry_after=None) == 60.0


def test_absurd_retry_after_is_clamped_to_ceiling() -> None:
    policy = BackoffPolicy.fixed(1.0)
    assert policy.resolve_delay(1, retry_after=1e300) == MAX_DELAY_CEILING


def test_no_retry_after_uses_policy_delay() -> None:
    policy = BackoffPolicy.exponential(2.0)
    assert policy.resolve_delay(3) == 8.0


def test_policy_is_frozen_and_hashable() -> None:
    policy = BackoffPolicy.fixed(5.0)
    assert policy == BackoffPolicy.fixed(5.0)
    assert hash(policy) == hash(BackoffPolicy.fixed(5.0))
    with pytest.raises(AttributeError):
        policy.base_delay = 1.0  # type: ignore[misc]


def test_retry_after_mode_accepts_string_and_has_str() -> None:
    policy = BackoffPolicy(retry_after_mode="override")  # type: ignore[arg-type]
    assert policy.retry_after_mode is RetryAfterMode.OVERRIDE
    assert str(RetryAfterMode.FLOOR) == "floor"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_delay": -1.0},
        {"base_delay": math.inf},
        {"base_delay": math.nan},
        {"base_delay": True},
        {"base_delay": "5"},
        {"multiplier": 0.5},
        {"multiplier": math.inf},
        {"jitter": -0.1},
        {"jitter": 1.5},
        {"jitter": math.nan},
        {"base_delay": 10.0, "max_delay": 5.0},
        {"max_delay": math.inf},
        {"retry_after_mode": "nonsense"},
    ],
)
def test_invalid_configuration_is_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ConfigurationError):
        BackoffPolicy(**kwargs)  # type: ignore[arg-type]
