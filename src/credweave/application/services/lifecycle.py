"""Credential lifecycle engine: the single home of cooldown, health and backoff rules.

The engine is a pure state-transition function over :class:`CredentialRecord`. It performs no
I/O, no locking and no networking; state stores only need to load a record, call the engine
and persist the result atomically. SQLite/Redis stores therefore reuse the exact same
business rules as :class:`~credweave.infrastructure.stores.memory.MemoryStateStore`.

Lifecycle summary:
    * ``AUTH_FAILED`` -> ``REVOKED``; ``PERMANENT_FAILURE`` -> ``UNHEALTHY``.
    * ``RATE_LIMITED`` / ``QUOTA_EXHAUSTED`` are upstream throttling, not credential health:
      they never advance the consecutive failure counter nor escalate to ``UNHEALTHY``.
    * ``TRANSIENT_ERROR`` advances the counter; reaching ``max_consecutive_failures`` marks the
      credential ``UNHEALTHY``, otherwise it cools down for the backoff delay.
    * ``SUCCESS`` resets the counter, but only when no stronger state set by another in-flight
      lease is still active.
    * When a timed cooldown elapses the credential returns to ``AVAILABLE`` with its failure
      counter intact. That is the half-open "probe" window: it is merely eligible again, so the
      next failure escalates the backoff and the next success resets it. CredWeave itself never
      issues a probe request.
"""

import random
from dataclasses import replace
from datetime import datetime, timedelta

from credweave.application.ports.state_store import CredentialRecord
from credweave.domain.backoff import (
    BackoffPolicy,
    RandomSource,
    RetryAfterMode,
)
from credweave.domain.enums import CredentialState, OutcomeType
from credweave.domain.errors import InvalidOutcomeError
from credweave.domain.outcomes import Outcome

# Lower rank = higher precedence (more severe / more restrictive state).
STATE_PRECEDENCE: dict[CredentialState, int] = {
    CredentialState.REVOKED: 0,
    CredentialState.DISABLED: 1,
    CredentialState.UNHEALTHY: 2,
    CredentialState.QUOTA_EXHAUSTED: 3,
    CredentialState.RATE_LIMITED: 4,
    CredentialState.COOLDOWN: 5,
    CredentialState.AVAILABLE: 6,
}

_TIMED_STATES = frozenset(
    {
        CredentialState.COOLDOWN,
        CredentialState.RATE_LIMITED,
        CredentialState.QUOTA_EXHAUSTED,
    }
)
_PERMANENT_STATES = frozenset(
    {
        CredentialState.REVOKED,
        CredentialState.DISABLED,
        CredentialState.UNHEALTHY,
        CredentialState.AVAILABLE,
    }
)
# Upstream throttling says nothing about the credential's own health.
_NON_HEALTH_OUTCOMES = frozenset(
    {
        OutcomeType.SUCCESS,
        OutcomeType.RATE_LIMITED,
        OutcomeType.QUOTA_EXHAUSTED,
    }
)


class LifecycleEngine:
    """Applies lifecycle rules and backoff policy to credential records.

    Args:
        backoff: Delay policy for cooldowns. Defaults to a fixed ``default_cooldown`` in which
            an upstream ``retry_after`` replaces the delay (legacy behavior).
        max_consecutive_failures: Consecutive health failures before escalating to UNHEALTHY.
        default_cooldown: Fixed cooldown in seconds used only when ``backoff`` is omitted.
        rng: Source of randomness for jitter in ``[0, 1)``; inject a seeded
            ``random.Random(seed).random`` for deterministic delays.
    """

    def __init__(
        self,
        *,
        backoff: BackoffPolicy | None = None,
        max_consecutive_failures: int = 3,
        default_cooldown: float = 60.0,
        rng: RandomSource | None = None,
    ) -> None:
        if backoff is None:
            backoff = BackoffPolicy(
                base_delay=max(0.0, float(default_cooldown)),
                retry_after_mode=RetryAfterMode.OVERRIDE,
            )
        self._backoff = backoff
        self._max_consecutive_failures = max(1, int(max_consecutive_failures))
        self._rng: RandomSource = rng if rng is not None else random.Random().random

    @property
    def backoff(self) -> BackoffPolicy:
        """Return the configured backoff policy."""
        return self._backoff

    @property
    def max_consecutive_failures(self) -> int:
        """Return the consecutive failure threshold that triggers UNHEALTHY."""
        return self._max_consecutive_failures

    def recover(self, record: CredentialRecord, now: datetime) -> CredentialRecord:
        """Return ``record`` restored to AVAILABLE if its timed cooldown has elapsed.

        The consecutive failure counter is preserved so backoff keeps escalating until the
        next SUCCESS (the probe-eligibility window).
        """
        if (
            record.state in _TIMED_STATES
            and record.cooldown_until is not None
            and now >= record.cooldown_until
        ):
            return replace(record, state=CredentialState.AVAILABLE, cooldown_until=None)
        return record

    def release(self, record: CredentialRecord, now: datetime) -> CredentialRecord:
        """Return ``record`` after releasing one in-flight lease without an outcome."""
        recovered = self.recover(record, now)
        return replace(recovered, in_flight_leases=max(0, recovered.in_flight_leases - 1))

    def apply_outcome(
        self,
        record: CredentialRecord,
        outcome: Outcome,
        now: datetime,
    ) -> CredentialRecord:
        """Return the record resulting from reporting ``outcome`` for one in-flight lease."""
        existing = self.recover(record, now)
        in_flight = max(0, existing.in_flight_leases - 1)

        # Administratively disabled credentials ignore outcome state changes.
        if existing.state == CredentialState.DISABLED:
            return replace(existing, in_flight_leases=in_flight)

        candidate_failures = existing.consecutive_failures
        if outcome.type not in _NON_HEALTH_OUTCOMES:
            candidate_failures += 1

        target_state, outcome_cooldown = self._target(outcome, candidate_failures, now)

        # Centralized precedence: the more severe of the existing and target states wins.
        new_state = (
            target_state
            if STATE_PRECEDENCE[target_state] < STATE_PRECEDENCE[existing.state]
            else existing.state
        )
        new_failures = 0 if new_state == CredentialState.AVAILABLE else candidate_failures

        return replace(
            existing,
            state=new_state,
            in_flight_leases=in_flight,
            consecutive_failures=new_failures,
            cooldown_until=self._merge_cooldown(existing, new_state, outcome, outcome_cooldown),
        )

    def _target(
        self,
        outcome: Outcome,
        candidate_failures: int,
        now: datetime,
    ) -> tuple[CredentialState, datetime | None]:
        """Map an outcome to its target state and the cooldown deadline it asks for."""
        kind = outcome.type
        if kind == OutcomeType.AUTH_FAILED:
            return CredentialState.REVOKED, None
        if kind in (OutcomeType.PERMANENT_FAILURE, OutcomeType.CONSECUTIVE_FAILURES_EXCEEDED):
            return CredentialState.UNHEALTHY, None
        if kind == OutcomeType.QUOTA_EXHAUSTED:
            if outcome.retry_after is None:
                return CredentialState.QUOTA_EXHAUSTED, None
            return CredentialState.QUOTA_EXHAUSTED, self._deadline(now, outcome.retry_after)
        if kind == OutcomeType.RATE_LIMITED:
            # Throttling is not a health failure: the upstream hint is authoritative and the
            # failure progression is neither consulted nor advanced.
            delay = (
                outcome.retry_after
                if outcome.retry_after is not None
                else self._backoff.compute_delay(1, self._rng)
            )
            return CredentialState.RATE_LIMITED, self._deadline(now, delay)
        if kind == OutcomeType.SUCCESS:
            return CredentialState.AVAILABLE, None
        # TRANSIENT_ERROR, and any unrecognized outcome type, is a generic health failure.
        if candidate_failures >= self._max_consecutive_failures:
            return CredentialState.UNHEALTHY, None
        delay = self._backoff.resolve_delay(candidate_failures, outcome.retry_after, self._rng)
        return CredentialState.COOLDOWN, self._deadline(now, delay)

    @staticmethod
    def _deadline(now: datetime, delay: float) -> datetime:
        try:
            return now + timedelta(seconds=delay)
        except (OverflowError, ValueError) as exc:
            raise InvalidOutcomeError(
                f"Cooldown delay {delay!r} cannot be represented safely as a datetime "
                f"deadline from {now.isoformat()}."
            ) from exc

    @staticmethod
    def _merge_cooldown(
        existing: CredentialRecord,
        new_state: CredentialState,
        outcome: Outcome,
        outcome_cooldown: datetime | None,
    ) -> datetime | None:
        """Combine the stored and requested cooldown deadlines; the later one always wins."""
        if new_state in _PERMANENT_STATES:
            return None
        if new_state == CredentialState.QUOTA_EXHAUSTED and (
            (outcome.type == OutcomeType.QUOTA_EXHAUSTED and outcome.retry_after is None)
            or (
                existing.state == CredentialState.QUOTA_EXHAUSTED
                and existing.cooldown_until is None
            )
        ):
            # Indefinite quota exhaustion takes precedence over timed cooldowns.
            return None
        if existing.cooldown_until is not None and outcome_cooldown is not None:
            return max(existing.cooldown_until, outcome_cooldown)
        if outcome_cooldown is not None:
            return outcome_cooldown
        return existing.cooldown_until
