"""Backoff policy value object computing cooldown delays for failing credentials.

The policy is pure: it performs no I/O, never reads the clock and only consumes randomness
through an injected :data:`RandomSource`, which keeps every delay deterministic under test.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from credweave.domain.errors import ConfigurationError

RandomSource = Callable[[], float]
"""Zero-argument callable returning a float in ``[0.0, 1.0)`` (e.g. ``random.Random(7).random``)."""

# Hard ceiling (100 years) so that runaway exponents or absurd hints can never overflow
# ``timedelta``/``datetime`` arithmetic.
MAX_DELAY_CEILING = 100.0 * 365.0 * 24.0 * 3600.0


class RetryAfterMode(str, Enum):
    """How an upstream ``retry_after`` hint combines with the policy-computed delay.

    Attributes:
        FLOOR: The credential cools down for ``max(policy_delay, retry_after)``. The hint is a
            lower bound and may be extended (but never shortened) by accumulated backoff.
        OVERRIDE: The hint replaces the policy delay entirely; the policy only supplies the
            delay when the upstream gave no hint. This is the legacy behavior.
    """

    FLOOR = "floor"
    OVERRIDE = "override"

    def __str__(self) -> str:
        return self.value


def _require_finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ConfigurationError(f"{name} must be a finite number.")
    return float(value)


@dataclass(frozen=True)
class BackoffPolicy:
    """Computes how long a credential cools down after its Nth consecutive failure.

    ``delay(attempt) = min(max_delay, base_delay * multiplier ** (attempt - 1))`` where
    ``attempt`` is 1 for the first failure. A ``multiplier`` of 1.0 yields a fixed cooldown.

    Jitter is a proportion in ``[0, 1]`` shaving a random share off the capped delay, so the
    result lies in ``[delay * (1 - jitter), delay]`` and ``max_delay`` is a hard ceiling.

    Attributes:
        base_delay: Delay in seconds after the first failure.
        multiplier: Growth factor per additional consecutive failure (``>= 1``).
        max_delay: Optional ceiling in seconds for the policy-computed delay.
        jitter: Proportion of the delay randomly removed, ``0`` disables jitter.
        retry_after_mode: How an upstream ``retry_after`` hint is combined with the delay.
    """

    base_delay: float = 60.0
    multiplier: float = 1.0
    max_delay: float | None = None
    jitter: float = 0.0
    retry_after_mode: RetryAfterMode = RetryAfterMode.FLOOR

    def __post_init__(self) -> None:
        base = _require_finite(self.base_delay, "base_delay")
        multiplier = _require_finite(self.multiplier, "multiplier")
        jitter = _require_finite(self.jitter, "jitter")
        if base < 0:
            raise ConfigurationError("base_delay must be >= 0.")
        if multiplier < 1.0:
            raise ConfigurationError("multiplier must be >= 1.0.")
        if not 0.0 <= jitter <= 1.0:
            raise ConfigurationError("jitter must be between 0.0 and 1.0.")
        object.__setattr__(self, "base_delay", base)
        object.__setattr__(self, "multiplier", multiplier)
        object.__setattr__(self, "jitter", jitter)
        if self.max_delay is not None:
            max_delay = _require_finite(self.max_delay, "max_delay")
            if max_delay < base:
                raise ConfigurationError("max_delay must be >= base_delay.")
            object.__setattr__(self, "max_delay", max_delay)
        try:
            object.__setattr__(self, "retry_after_mode", RetryAfterMode(self.retry_after_mode))
        except ValueError as exc:
            raise ConfigurationError(
                f"Invalid retry_after_mode: {self.retry_after_mode!r}."
            ) from exc

    @classmethod
    def fixed(
        cls,
        delay: float,
        *,
        jitter: float = 0.0,
        retry_after_mode: RetryAfterMode = RetryAfterMode.FLOOR,
    ) -> "BackoffPolicy":
        """Create a policy applying the same ``delay`` after every failure."""
        return cls(
            base_delay=delay,
            multiplier=1.0,
            jitter=jitter,
            retry_after_mode=retry_after_mode,
        )

    @classmethod
    def exponential(
        cls,
        base_delay: float,
        *,
        multiplier: float = 2.0,
        max_delay: float | None = None,
        jitter: float = 0.0,
        retry_after_mode: RetryAfterMode = RetryAfterMode.FLOOR,
    ) -> "BackoffPolicy":
        """Create a policy growing the delay by ``multiplier`` after each consecutive failure."""
        return cls(
            base_delay=base_delay,
            multiplier=multiplier,
            max_delay=max_delay,
            jitter=jitter,
            retry_after_mode=retry_after_mode,
        )

    def compute_delay(self, attempt: int, rng: RandomSource | None = None) -> float:
        """Return the policy delay in seconds for the given 1-based consecutive failure count.

        ``rng`` is only consulted when jitter is enabled; jitter is skipped if it is ``None``.
        """
        exponent = max(0, int(attempt) - 1)
        try:
            raw = self.base_delay * self.multiplier**exponent
        except OverflowError:
            raw = math.inf
        ceiling = MAX_DELAY_CEILING if self.max_delay is None else self.max_delay
        delay = min(raw, ceiling, MAX_DELAY_CEILING)
        if self.jitter > 0.0 and rng is not None:
            sample = min(max(float(rng()), 0.0), 1.0)
            delay *= 1.0 - self.jitter * sample
        return delay

    def resolve_delay(
        self,
        attempt: int,
        retry_after: float | None = None,
        rng: RandomSource | None = None,
    ) -> float:
        """Return the cooldown in seconds for a failure, honoring any upstream ``retry_after``.

        The upstream hint always wins over ``max_delay``: it is never shortened or capped, so a
        credential is never retried earlier than the provider asked. With
        :attr:`RetryAfterMode.FLOOR` the result is ``max(policy_delay, retry_after)``; with
        :attr:`RetryAfterMode.OVERRIDE` it is exactly ``retry_after``.
        """
        if retry_after is None:
            return self.compute_delay(attempt, rng)
        hint = min(float(retry_after), MAX_DELAY_CEILING)
        if self.retry_after_mode is RetryAfterMode.OVERRIDE:
            return hint
        return max(self.compute_delay(attempt, rng), hint)
