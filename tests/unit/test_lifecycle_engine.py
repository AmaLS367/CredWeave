"""Deterministic unit tests for the LifecycleEngine cooldown, health and backoff rules."""

import itertools
import random
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from credweave.application.ports.state_store import CredentialRecord
from credweave.application.services.lifecycle import STATE_PRECEDENCE, LifecycleEngine
from credweave.domain.backoff import BackoffPolicy, RetryAfterMode
from credweave.domain.enums import CredentialState
from credweave.domain.errors import InvalidOutcomeError
from credweave.domain.outcomes import Outcome
from tests.conftest import TestClock


def fresh(in_flight: int = 1) -> CredentialRecord:
    return CredentialRecord(
        credential_id="c1",
        state=CredentialState.AVAILABLE,
        in_flight_leases=in_flight,
    )


def secs(now: datetime, record: CredentialRecord) -> float:
    assert record.cooldown_until is not None
    return (record.cooldown_until - now).total_seconds()


def fail_and_recover(
    engine: LifecycleEngine,
    record: CredentialRecord,
    now: datetime,
    outcome: Outcome,
) -> tuple[CredentialRecord, datetime, float]:
    """Report a failure, then jump to the instant its cooldown elapses (and lease again)."""
    failed = engine.apply_outcome(record, outcome, now)
    cooldown = secs(now, failed)
    assert failed.cooldown_until is not None
    now = failed.cooldown_until
    return replace(engine.recover(failed, now), in_flight_leases=1), now, cooldown


def run_failures(engine: LifecycleEngine, now: datetime, count: int) -> list[float]:
    record, cooldowns = fresh(), []
    for _ in range(count):
        record, now, cooldown = fail_and_recover(engine, record, now, Outcome.transient_error())
        cooldowns.append(cooldown)
    return cooldowns


def test_default_engine_is_legacy_fixed_cooldown(test_clock: TestClock) -> None:
    now = test_clock.now()
    engine = LifecycleEngine()
    rec = engine.apply_outcome(fresh(), Outcome.transient_error(), now)
    assert rec.state == CredentialState.COOLDOWN
    assert secs(now, rec) == 60.0
    assert engine.max_consecutive_failures == 3
    assert engine.backoff.retry_after_mode is RetryAfterMode.OVERRIDE


def test_legacy_default_lets_smaller_retry_after_replace_cooldown(test_clock: TestClock) -> None:
    now = test_clock.now()
    rec = LifecycleEngine(default_cooldown=60.0).apply_outcome(
        fresh(), Outcome.transient_error(retry_after=5.0), now
    )
    assert secs(now, rec) == 5.0


def test_fixed_backoff_is_constant_across_failures(test_clock: TestClock) -> None:
    engine = LifecycleEngine(backoff=BackoffPolicy.fixed(20.0), max_consecutive_failures=10)
    assert run_failures(engine, test_clock.now(), 5) == [20.0] * 5


def test_exponential_sequence_through_engine(test_clock: TestClock) -> None:
    engine = LifecycleEngine(backoff=BackoffPolicy.exponential(1.0), max_consecutive_failures=10)
    assert run_failures(engine, test_clock.now(), 5) == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_max_delay_cap_through_engine(test_clock: TestClock) -> None:
    engine = LifecycleEngine(
        backoff=BackoffPolicy.exponential(1.0, max_delay=5.0), max_consecutive_failures=10
    )
    assert run_failures(engine, test_clock.now(), 5) == [1.0, 2.0, 4.0, 5.0, 5.0]


def test_jitter_bounds_through_engine(test_clock: TestClock) -> None:
    engine = LifecycleEngine(
        backoff=BackoffPolicy.fixed(100.0, jitter=0.3),
        max_consecutive_failures=1000,
        rng=random.Random(7).random,
    )
    cooldowns = run_failures(engine, test_clock.now(), 50)
    assert all(70.0 <= c <= 100.0 for c in cooldowns)
    assert len(set(cooldowns)) > 1


def test_seeded_jitter_is_reproducible_between_engines(test_clock: TestClock) -> None:
    def run() -> list[float]:
        engine = LifecycleEngine(
            backoff=BackoffPolicy.exponential(2.0, jitter=0.5),
            max_consecutive_failures=10,
            rng=random.Random(2024).random,
        )
        return run_failures(engine, test_clock.now(), 5)

    first, second = run(), run()
    assert first == second
    assert len(set(first)) == len(first)


def test_default_rng_is_used_when_not_injected(test_clock: TestClock) -> None:
    now = test_clock.now()
    engine = LifecycleEngine(backoff=BackoffPolicy.fixed(100.0, jitter=0.5))
    rec = engine.apply_outcome(fresh(), Outcome.transient_error(), now)
    assert 50.0 <= secs(now, rec) <= 100.0


def test_retry_after_larger_than_calculated_delay(test_clock: TestClock) -> None:
    now = test_clock.now()
    engine = LifecycleEngine(backoff=BackoffPolicy.exponential(1.0))
    rec = engine.apply_outcome(fresh(), Outcome.transient_error(retry_after=45.0), now)
    assert secs(now, rec) == 45.0


def test_retry_after_smaller_than_calculated_delay(test_clock: TestClock) -> None:
    now = test_clock.now()
    engine = LifecycleEngine(backoff=BackoffPolicy.fixed(30.0))
    rec = engine.apply_outcome(fresh(), Outcome.transient_error(retry_after=5.0), now)
    assert secs(now, rec) == 30.0


def test_retry_after_is_never_earlier_than_hint_for_any_attempt(test_clock: TestClock) -> None:
    engine = LifecycleEngine(
        backoff=BackoffPolicy.exponential(1.0, max_delay=8.0, jitter=1.0),
        max_consecutive_failures=100,
        rng=random.Random(3).random,
    )
    now, rec = test_clock.now(), fresh()
    for _ in range(20):
        rec, now, cooldown = fail_and_recover(
            engine, rec, now, Outcome.transient_error(retry_after=6.0)
        )
        assert cooldown >= 6.0


def test_rate_limited_hint_is_exact_and_backoff_is_not_consulted(test_clock: TestClock) -> None:
    now = test_clock.now()
    engine = LifecycleEngine(backoff=BackoffPolicy.fixed(300.0))
    rec = engine.apply_outcome(fresh(), Outcome.rate_limited(retry_after=7.0), now)
    assert rec.state == CredentialState.RATE_LIMITED
    assert secs(now, rec) == 7.0


def test_rate_limited_without_hint_uses_policy_base_delay(test_clock: TestClock) -> None:
    now = test_clock.now()
    engine = LifecycleEngine(backoff=BackoffPolicy.exponential(12.0))
    rec = engine.apply_outcome(fresh(), Outcome.rate_limited(), now)
    assert secs(now, rec) == 12.0


def test_rate_limits_never_become_unhealthy(test_clock: TestClock) -> None:
    engine = LifecycleEngine(max_consecutive_failures=1)
    now, rec = test_clock.now(), fresh()
    for _ in range(25):
        rec, now, _ = fail_and_recover(engine, rec, now, Outcome.rate_limited(retry_after=1.0))
        assert rec.state == CredentialState.AVAILABLE
        assert rec.consecutive_failures == 0


def test_quota_exhausted_is_not_a_health_failure(test_clock: TestClock) -> None:
    now = test_clock.now()
    engine = LifecycleEngine(max_consecutive_failures=1)
    rec = engine.apply_outcome(fresh(), Outcome.quota_exhausted(retry_after=30.0), now)
    assert rec.state == CredentialState.QUOTA_EXHAUSTED
    assert rec.consecutive_failures == 0
    assert secs(now, rec) == 30.0

    indefinite = engine.apply_outcome(fresh(), Outcome.quota_exhausted(), now)
    assert indefinite.state == CredentialState.QUOTA_EXHAUSTED
    assert indefinite.cooldown_until is None
    assert indefinite.consecutive_failures == 0
    # Indefinite exhaustion never recovers on its own.
    far_future = now + timedelta(days=365)
    assert engine.recover(indefinite, far_future).state == CredentialState.QUOTA_EXHAUSTED


def test_repeated_transient_failures_escalate_to_unhealthy(test_clock: TestClock) -> None:
    engine = LifecycleEngine(backoff=BackoffPolicy.exponential(1.0), max_consecutive_failures=3)
    now, rec = test_clock.now(), fresh()
    for _ in range(2):
        rec, now, _ = fail_and_recover(engine, rec, now, Outcome.transient_error())
    assert rec.state == CredentialState.AVAILABLE
    assert rec.consecutive_failures == 2

    rec = engine.apply_outcome(rec, Outcome.transient_error(), now)
    assert rec.state == CredentialState.UNHEALTHY
    assert rec.consecutive_failures == 3
    assert rec.cooldown_until is None
    # UNHEALTHY does not auto-recover.
    assert engine.recover(rec, now + timedelta(days=30)).state == CredentialState.UNHEALTHY


def test_interleaved_rate_limits_neither_hide_nor_advance_failures(test_clock: TestClock) -> None:
    engine = LifecycleEngine(max_consecutive_failures=3, default_cooldown=10.0)
    now, rec = test_clock.now(), fresh()
    for outcome in (
        Outcome.transient_error(),
        Outcome.rate_limited(retry_after=1.0),
        Outcome.transient_error(),
        Outcome.rate_limited(retry_after=1.0),
    ):
        rec, now, _ = fail_and_recover(engine, rec, now, outcome)
    assert rec.consecutive_failures == 2
    rec = engine.apply_outcome(rec, Outcome.transient_error(), now)
    assert rec.state == CredentialState.UNHEALTHY


def test_success_resets_failure_progression_and_backoff(test_clock: TestClock) -> None:
    engine = LifecycleEngine(backoff=BackoffPolicy.exponential(1.0), max_consecutive_failures=10)
    now, rec = test_clock.now(), fresh()
    for _ in range(3):
        rec, now, _ = fail_and_recover(engine, rec, now, Outcome.transient_error())
    assert rec.consecutive_failures == 3

    rec = engine.apply_outcome(rec, Outcome.success(), now)
    assert rec.state == CredentialState.AVAILABLE
    assert rec.consecutive_failures == 0

    # Backoff restarts from the base delay.
    rec = engine.apply_outcome(replace(rec, in_flight_leases=1), Outcome.transient_error(), now)
    assert secs(now, rec) == 1.0


def test_success_does_not_override_stronger_active_state(test_clock: TestClock) -> None:
    now = test_clock.now()
    engine = LifecycleEngine()
    cooling = engine.apply_outcome(fresh(2), Outcome.transient_error(retry_after=30.0), now)
    after = engine.apply_outcome(cooling, Outcome.success(), now + timedelta(seconds=1))
    assert after.state == CredentialState.COOLDOWN
    assert after.cooldown_until == cooling.cooldown_until
    assert after.consecutive_failures == 1
    assert after.in_flight_leases == 0


def test_cooldown_expiration_recovers_and_preserves_failure_count(test_clock: TestClock) -> None:
    now = test_clock.now()
    engine = LifecycleEngine(backoff=BackoffPolicy.fixed(10.0))
    rec = engine.apply_outcome(fresh(), Outcome.transient_error(), now)
    assert engine.recover(rec, now + timedelta(seconds=9.999)).state == CredentialState.COOLDOWN
    recovered = engine.recover(rec, now + timedelta(seconds=10))
    assert recovered.state == CredentialState.AVAILABLE
    assert recovered.cooldown_until is None
    assert recovered.consecutive_failures == 1


@pytest.mark.parametrize(
    "outcome",
    [Outcome.rate_limited(retry_after=10.0), Outcome.quota_exhausted(retry_after=10.0)],
)
def test_timed_throttle_states_recover_automatically(
    test_clock: TestClock, outcome: Outcome
) -> None:
    now = test_clock.now()
    rec = LifecycleEngine().apply_outcome(fresh(), outcome, now)
    recovered = LifecycleEngine().recover(rec, now + timedelta(seconds=10))
    assert recovered.state == CredentialState.AVAILABLE


def test_recover_returns_same_object_when_nothing_changes(test_clock: TestClock) -> None:
    rec = fresh()
    assert LifecycleEngine().recover(rec, test_clock.now()) is rec


def test_auth_failed_revokes_and_permanent_failure_is_unhealthy(test_clock: TestClock) -> None:
    now = test_clock.now()
    engine = LifecycleEngine()
    revoked = engine.apply_outcome(fresh(), Outcome.auth_failed(), now)
    assert revoked.state == CredentialState.REVOKED
    assert revoked.cooldown_until is None
    assert revoked.consecutive_failures == 1

    unhealthy = engine.apply_outcome(fresh(), Outcome.permanent_failure(), now)
    assert unhealthy.state == CredentialState.UNHEALTHY
    assert unhealthy.cooldown_until is None

    exceeded = engine.apply_outcome(fresh(), Outcome.consecutive_failures_exceeded(), now)
    assert exceeded.state == CredentialState.UNHEALTHY

    # REVOKED outranks UNHEALTHY in either order.
    escalated = engine.apply_outcome(
        replace(unhealthy, in_flight_leases=1), Outcome.auth_failed(), now
    )
    assert escalated.state == CredentialState.REVOKED
    kept = engine.apply_outcome(
        replace(revoked, in_flight_leases=1), Outcome.permanent_failure(), now
    )
    assert kept.state == CredentialState.REVOKED


def test_disabled_credentials_ignore_outcomes_but_release_leases(test_clock: TestClock) -> None:
    disabled = CredentialRecord(
        credential_id="c1", state=CredentialState.DISABLED, in_flight_leases=2
    )
    rec = LifecycleEngine().apply_outcome(disabled, Outcome.auth_failed(), test_clock.now())
    assert rec.state == CredentialState.DISABLED
    assert rec.in_flight_leases == 1
    assert rec.consecutive_failures == 0


def test_release_decrements_and_recovers_without_going_negative(test_clock: TestClock) -> None:
    now = test_clock.now()
    engine = LifecycleEngine()
    cooling = engine.apply_outcome(fresh(2), Outcome.transient_error(retry_after=5.0), now)
    released = engine.release(cooling, now + timedelta(seconds=10))
    assert released.in_flight_leases == 0
    assert released.state == CredentialState.AVAILABLE
    assert engine.release(released, now).in_flight_leases == 0


def test_unknown_outcome_type_is_treated_as_generic_failure(test_clock: TestClock) -> None:
    now = test_clock.now()
    odd = Outcome.success()
    object.__setattr__(odd, "type", "custom_unknown")
    rec = LifecycleEngine(default_cooldown=15.0).apply_outcome(fresh(), odd, now)
    assert rec.state == CredentialState.COOLDOWN
    assert rec.consecutive_failures == 1
    assert secs(now, rec) == 15.0


def test_unrepresentable_retry_after_cannot_be_created() -> None:
    for factory in (
        Outcome.transient_error,
        Outcome.rate_limited,
        Outcome.quota_exhausted,
    ):
        with pytest.raises(InvalidOutcomeError):
            factory(retry_after=1e300)


def test_very_large_representable_retry_after_is_not_clamped_to_ceiling(
    test_clock: TestClock,
) -> None:
    now = test_clock.now()
    engine = LifecycleEngine()
    hint_200_years = 200.0 * 365.0 * 24.0 * 3600.0

    for outcome in (
        Outcome.rate_limited(retry_after=hint_200_years),
        Outcome.quota_exhausted(retry_after=hint_200_years),
        Outcome.transient_error(retry_after=hint_200_years),
    ):
        rec = engine.apply_outcome(fresh(), outcome, now)
        assert rec.cooldown_until == now + timedelta(seconds=hint_200_years)


def test_retry_after_overflowing_target_datetime_fails_with_invalid_outcome_error() -> None:
    engine = LifecycleEngine()
    hint_2000_years = 2000.0 * 365.0 * 24.0 * 3600.0
    now_year_9000 = datetime(9000, 1, 1)
    outcome = Outcome.rate_limited(retry_after=hint_2000_years)
    with pytest.raises(InvalidOutcomeError):
        engine.apply_outcome(fresh(), outcome, now_year_9000)


def test_threshold_and_cooldown_are_clamped_to_sane_values(test_clock: TestClock) -> None:
    now = test_clock.now()
    engine = LifecycleEngine(max_consecutive_failures=0, default_cooldown=-5.0)
    assert engine.max_consecutive_failures == 1
    transient = engine.apply_outcome(fresh(), Outcome.transient_error(), now)
    assert transient.state == CredentialState.UNHEALTHY
    assert secs(now, engine.apply_outcome(fresh(), Outcome.rate_limited(), now)) == 0.0


def test_precedence_table_ranks_every_state() -> None:
    assert set(STATE_PRECEDENCE) == set(CredentialState)
    assert len(set(STATE_PRECEDENCE.values())) == len(CredentialState)


@pytest.mark.parametrize(
    "backoff",
    [
        BackoffPolicy.fixed(60.0),
        BackoffPolicy.exponential(2.0, multiplier=2.0, max_delay=100.0),
        BackoffPolicy.exponential(2.0, retry_after_mode=RetryAfterMode.OVERRIDE),
    ],
)
@pytest.mark.parametrize(
    "outcomes",
    [
        (Outcome.transient_error(retry_after=4.0), Outcome.success()),
        (Outcome.transient_error(), Outcome.transient_error(), Outcome.success()),
        (Outcome.rate_limited(retry_after=20.0), Outcome.transient_error(), Outcome.success()),
        (
            Outcome.transient_error(retry_after=9.0),
            Outcome.quota_exhausted(retry_after=15.0),
            Outcome.rate_limited(retry_after=12.0),
        ),
        (Outcome.transient_error(), Outcome.quota_exhausted(), Outcome.rate_limited()),
        (Outcome.transient_error(), Outcome.auth_failed(), Outcome.rate_limited(retry_after=8.0)),
        (Outcome.permanent_failure(), Outcome.transient_error(), Outcome.success()),
    ],
)
def test_concurrent_outcomes_are_order_independent(
    test_clock: TestClock,
    backoff: BackoffPolicy,
    outcomes: tuple[Outcome, ...],
) -> None:
    """Outcomes of simultaneous leases resolve identically in every reporting order."""
    now = test_clock.now()
    results: list[CredentialRecord] = []
    for perm in itertools.permutations(outcomes):
        engine = LifecycleEngine(backoff=backoff, max_consecutive_failures=4)
        rec = fresh(len(perm))
        for outcome in perm:
            rec = engine.apply_outcome(rec, outcome, now)
        assert rec.in_flight_leases == 0
        results.append(rec)
    assert all(r == results[0] for r in results)


def test_reclaim_only_decrements_in_flight(test_clock: TestClock) -> None:
    """Reclaiming an orphaned lease slot never touches health, failures or cooldown."""
    engine = LifecycleEngine()
    deadline = test_clock.now() + timedelta(seconds=30)
    record = CredentialRecord(
        credential_id="c1",
        state=CredentialState.COOLDOWN,
        in_flight_leases=2,
        consecutive_failures=2,
        cooldown_until=deadline,
        total_leases=9,
    )

    reclaimed = engine.reclaim(record)

    assert reclaimed == replace(record, in_flight_leases=1)
    assert engine.reclaim(replace(record, in_flight_leases=0)).in_flight_leases == 0
