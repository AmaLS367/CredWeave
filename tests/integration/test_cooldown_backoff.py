"""Integration tests: backoff policy configured through CredentialPool end to end."""

import asyncio
import random
import threading
from datetime import datetime, timedelta

import pytest

import credweave
from credweave import (
    BackoffPolicy,
    Credential,
    CredentialPool,
    CredentialState,
    LifecycleEngine,
    MemoryStateStore,
    NoCredentialsAvailableError,
    Outcome,
    RetryAfterMode,
)
from tests.conftest import TestClock


def make_pool(
    clock: TestClock,
    creds: list[Credential],
    **kwargs: object,
) -> CredentialPool:
    return CredentialPool(credentials=creds, clock=clock, **kwargs)  # type: ignore[arg-type]


def cooldown_seconds(pool: CredentialPool, credential_id: str, now: datetime) -> float:
    record = pool.get_record(credential_id)
    assert record is not None
    assert record.cooldown_until is not None
    return (record.cooldown_until - now).total_seconds()


def test_default_pool_behavior_is_backward_compatible(
    sample_credential: Credential, test_clock: TestClock
) -> None:
    pool = make_pool(test_clock, [sample_credential])
    now = test_clock.now()

    pool.report_sync(pool.acquire_sync(), Outcome.transient_error())
    assert cooldown_seconds(pool, sample_credential.id, now) == 60.0

    test_clock.advance(61.0)
    pool.report_sync(pool.acquire_sync(), Outcome.transient_error(retry_after=5.0))
    assert cooldown_seconds(pool, sample_credential.id, test_clock.now()) == 5.0


def test_pool_exponential_backoff_with_cap_and_recovery(
    sample_credential: Credential, test_clock: TestClock
) -> None:
    pool = make_pool(
        test_clock,
        [sample_credential],
        backoff=BackoffPolicy.exponential(10.0, max_delay=35.0),
        max_consecutive_failures=10,
    )
    observed = []
    for _ in range(5):
        now = test_clock.now()
        pool.report_sync(pool.acquire_sync(), Outcome.transient_error())
        observed.append(cooldown_seconds(pool, sample_credential.id, now))
        with pytest.raises(NoCredentialsAvailableError):
            pool.acquire_sync()
        test_clock.advance(observed[-1])
        record = pool.get_record(sample_credential.id)
        assert record is not None
        assert record.state == CredentialState.AVAILABLE
    assert observed == [10.0, 20.0, 35.0, 35.0, 35.0]


def test_pool_backoff_escalates_to_unhealthy_then_requires_reset(
    sample_credential: Credential, test_clock: TestClock
) -> None:
    pool = make_pool(
        test_clock,
        [sample_credential],
        backoff=BackoffPolicy.exponential(1.0),
        max_consecutive_failures=3,
    )
    for _ in range(3):
        pool.report_sync(pool.acquire_sync(), Outcome.transient_error())
        test_clock.advance(100.0)
    record = pool.get_record(sample_credential.id)
    assert record is not None
    assert record.state == CredentialState.UNHEALTHY
    assert record.consecutive_failures == 3
    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync()

    pool.reset_credential(sample_credential.id)
    pool.report_sync(pool.acquire_sync(), Outcome.success())


def test_pool_success_resets_backoff_progression(
    sample_credential: Credential, test_clock: TestClock
) -> None:
    pool = make_pool(
        test_clock,
        [sample_credential],
        backoff=BackoffPolicy.exponential(1.0),
        max_consecutive_failures=10,
    )
    for _ in range(3):
        pool.report_sync(pool.acquire_sync(), Outcome.transient_error())
        test_clock.advance(100.0)
    pool.report_sync(pool.acquire_sync(), Outcome.success())

    now = test_clock.now()
    pool.report_sync(pool.acquire_sync(), Outcome.transient_error())
    assert cooldown_seconds(pool, sample_credential.id, now) == 1.0


def test_pool_retry_after_floor_semantics(
    sample_credential: Credential, test_clock: TestClock
) -> None:
    pool = make_pool(
        test_clock,
        [sample_credential],
        backoff=BackoffPolicy.fixed(30.0),
        max_consecutive_failures=10,
    )
    now = test_clock.now()
    pool.report_sync(pool.acquire_sync(), Outcome.transient_error(retry_after=5.0))
    assert cooldown_seconds(pool, sample_credential.id, now) == 30.0

    test_clock.advance(31.0)
    now = test_clock.now()
    pool.report_sync(pool.acquire_sync(), Outcome.transient_error(retry_after=120.0))
    assert cooldown_seconds(pool, sample_credential.id, now) == 120.0


def test_pool_retry_after_override_semantics(
    sample_credential: Credential, test_clock: TestClock
) -> None:
    pool = make_pool(
        test_clock,
        [sample_credential],
        backoff=BackoffPolicy.fixed(30.0, retry_after_mode=RetryAfterMode.OVERRIDE),
    )
    now = test_clock.now()
    pool.report_sync(pool.acquire_sync(), Outcome.transient_error(retry_after=5.0))
    assert cooldown_seconds(pool, sample_credential.id, now) == 5.0


def test_pool_retry_after_very_large_hint_is_not_clamped(
    sample_credential: Credential, test_clock: TestClock
) -> None:
    pool = make_pool(
        test_clock,
        [sample_credential],
        backoff=BackoffPolicy.fixed(30.0),
    )
    now = test_clock.now()
    hint_200_years = 200.0 * 365.0 * 24.0 * 3600.0
    pool.report_sync(
        pool.acquire_sync(),
        Outcome.rate_limited(retry_after=hint_200_years),
    )
    assert cooldown_seconds(pool, sample_credential.id, now) == hint_200_years


def test_pool_rate_limits_never_make_credential_unhealthy(
    sample_credential: Credential, test_clock: TestClock
) -> None:
    pool = make_pool(
        test_clock,
        [sample_credential],
        backoff=BackoffPolicy.exponential(1.0),
        max_consecutive_failures=1,
    )
    for _ in range(20):
        pool.report_sync(pool.acquire_sync(), Outcome.rate_limited(retry_after=1.0))
        record = pool.get_record(sample_credential.id)
        assert record is not None
        assert record.state == CredentialState.RATE_LIMITED
        assert record.consecutive_failures == 0
        test_clock.advance(1.0)


def test_pool_default_cooldown_is_ignored_when_backoff_is_given(
    sample_credential: Credential, test_clock: TestClock
) -> None:
    pool = make_pool(
        test_clock,
        [sample_credential],
        default_cooldown=999.0,
        backoff=BackoffPolicy.fixed(7.0),
    )
    now = test_clock.now()
    pool.report_sync(pool.acquire_sync(), Outcome.transient_error())
    assert cooldown_seconds(pool, sample_credential.id, now) == 7.0


def _jittered_run_sync(clock: TestClock, cred: Credential, seed: int) -> list[float]:
    pool = make_pool(
        clock,
        [cred],
        backoff=BackoffPolicy.exponential(4.0, jitter=0.5, max_delay=50.0),
        max_consecutive_failures=100,
        rng=random.Random(seed).random,
    )
    out = []
    for _ in range(6):
        now = clock.now()
        pool.report_sync(pool.acquire_sync(), Outcome.transient_error())
        out.append(cooldown_seconds(pool, cred.id, now))
        clock.advance(out[-1])
    return out


async def _jittered_run_async(clock: TestClock, cred: Credential, seed: int) -> list[float]:
    pool = make_pool(
        clock,
        [cred],
        backoff=BackoffPolicy.exponential(4.0, jitter=0.5, max_delay=50.0),
        max_consecutive_failures=100,
        rng=random.Random(seed).random,
    )
    out = []
    for _ in range(6):
        now = clock.now()
        await pool.report(await pool.acquire(), Outcome.transient_error())
        out.append(cooldown_seconds(pool, cred.id, now))
        clock.advance(out[-1])
    return out


async def test_sync_and_async_paths_are_equivalent_with_seeded_jitter(
    sample_credential: Credential,
) -> None:
    sync_run = _jittered_run_sync(TestClock(), sample_credential, seed=11)
    async_run = await _jittered_run_async(TestClock(), sample_credential, seed=11)
    assert sync_run == async_run
    assert len(set(sync_run)) > 1
    assert sync_run != _jittered_run_sync(TestClock(), sample_credential, seed=12)


async def test_sync_and_async_mixed_outcomes_produce_identical_records(
    sample_credential: Credential,
) -> None:
    outcomes = [
        Outcome.transient_error(retry_after=3.0),
        Outcome.rate_limited(retry_after=15.0),
        Outcome.transient_error(),
        Outcome.success(),
        Outcome.quota_exhausted(retry_after=40.0),
        Outcome.transient_error(),
    ]

    def build(clock: TestClock) -> CredentialPool:
        return make_pool(
            clock,
            [sample_credential],
            backoff=BackoffPolicy.exponential(2.0, max_delay=30.0),
            max_consecutive_failures=5,
        )

    sync_clock, async_clock = TestClock(), TestClock()
    sync_pool, async_pool = build(sync_clock), build(async_clock)
    for outcome in outcomes:
        sync_pool.report_sync(sync_pool.acquire_sync(), outcome)
        await async_pool.report(await async_pool.acquire(), outcome)
        assert sync_pool.get_record(sample_credential.id) == async_pool.get_record(
            sample_credential.id
        )
        sync_clock.advance(100.0)
        async_clock.advance(100.0)
        assert sync_pool.get_record(sample_credential.id) == async_pool.get_record(
            sample_credential.id
        )


def test_concurrent_threads_mixed_outcomes_respect_precedence(
    sample_credential: Credential, test_clock: TestClock
) -> None:
    pool = make_pool(
        test_clock,
        [sample_credential],
        backoff=BackoffPolicy.exponential(5.0, max_delay=60.0),
        max_consecutive_failures=50,
    )
    workers = 24
    leases = [pool.acquire_sync() for _ in range(workers)]
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []

    def outcome_for(idx: int) -> Outcome:
        if idx % 4 == 0:
            return Outcome.success()
        if idx % 4 == 1:
            return Outcome.transient_error(retry_after=2.0)
        if idx % 4 == 2:
            return Outcome.rate_limited(retry_after=20.0)
        return Outcome.transient_error()

    def worker(idx: int) -> None:
        try:
            barrier.wait()
            pool.report_sync(leases[idx], outcome_for(idx))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    record = pool.get_record(sample_credential.id)
    assert record is not None
    assert record.in_flight_leases == 0
    # RATE_LIMITED outranks COOLDOWN regardless of reporting order, and the cooldown deadline
    # is the latest requested one: 12 transient failures grow the backoff to its 60s cap.
    assert record.state == CredentialState.RATE_LIMITED
    assert record.consecutive_failures == 12
    assert record.cooldown_until == test_clock.now() + timedelta(seconds=60.0)
    assert pool.in_flight_leases == 0


async def test_concurrent_async_mixed_outcomes_respect_precedence(
    sample_credential: Credential, test_clock: TestClock
) -> None:
    pool = make_pool(
        test_clock,
        [sample_credential],
        backoff=BackoffPolicy.exponential(1.0),
        max_consecutive_failures=3,
    )
    leases = [await pool.acquire() for _ in range(5)]
    outcomes = [
        Outcome.success(),
        Outcome.transient_error(),
        Outcome.rate_limited(retry_after=10.0),
        Outcome.transient_error(),
        Outcome.transient_error(),
    ]
    await asyncio.gather(
        *(pool.report(lease, o) for lease, o in zip(leases, outcomes, strict=True))
    )

    record = await pool.get_record_async(sample_credential.id)
    assert record is not None
    # Three transient failures hit the threshold; UNHEALTHY outranks RATE_LIMITED.
    assert record.state == CredentialState.UNHEALTHY
    assert record.consecutive_failures == 3
    assert record.in_flight_leases == 0
    assert record.cooldown_until is None


def test_custom_store_reuses_the_same_lifecycle_engine(
    sample_credential: Credential, test_clock: TestClock
) -> None:
    engine = LifecycleEngine(
        backoff=BackoffPolicy.exponential(3.0),
        max_consecutive_failures=5,
        rng=random.Random(0).random,
    )
    store = MemoryStateStore(clock=test_clock, lifecycle=engine)
    pool = CredentialPool(credentials=[sample_credential], clock=test_clock, store=store)
    now = test_clock.now()
    pool.report_sync(pool.acquire_sync(), Outcome.transient_error())
    assert cooldown_seconds(pool, sample_credential.id, now) == 3.0


def test_public_api_exports_lifecycle_primitives() -> None:
    for name in ("BackoffPolicy", "RetryAfterMode", "LifecycleEngine", "RandomSource"):
        assert name in credweave.__all__
        assert hasattr(credweave, name)
