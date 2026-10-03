"""Unit tests for the MemoryStateStore adapter."""

import concurrent.futures
import itertools
from datetime import datetime, timedelta, timezone

import pytest

from credweave.application.ports.state_store import CredentialRecord, StateStore
from credweave.domain.enums import CredentialState
from credweave.domain.outcomes import Outcome
from credweave.infrastructure.stores.memory import MemoryStateStore
from tests.conftest import TestClock


def test_memory_state_store_protocol_conformance(test_clock: TestClock) -> None:
    """Verify MemoryStateStore satisfies the StateStore protocol."""
    store = MemoryStateStore(clock=test_clock)
    assert isinstance(store, StateStore)


def test_initialize_and_get_record(test_clock: TestClock) -> None:
    """Verify initialize_record creates record and get_record returns it."""
    store = MemoryStateStore(clock=test_clock)

    assert store.get_record("c1") is None

    rec = store.initialize_record("c1", metadata={"tier": "prod"})
    assert rec.credential_id == "c1"
    assert rec.state == CredentialState.AVAILABLE
    assert rec.metadata.get("tier") == "prod"

    fetched = store.get_record("c1")
    assert fetched == rec


@pytest.mark.asyncio
async def test_get_and_list_records_async(test_clock: TestClock) -> None:
    """Verify async get_record and list_records."""
    store = MemoryStateStore(clock=test_clock)
    store.initialize_record("c1")
    store.initialize_record("c2")

    rec1 = await store.get_record_async("c1")
    assert rec1 is not None
    assert rec1.credential_id == "c1"

    all_records = await store.list_records_async()
    assert len(all_records) == 2
    assert {r.credential_id for r in all_records} == {"c1", "c2"}


def test_record_acquire_increments_leases(test_clock: TestClock) -> None:
    """Verify record_acquire increments in-flight and total leases."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    store.record_acquire("c1", now)
    rec1 = store.get_record("c1")
    assert rec1 is not None
    assert rec1.in_flight_leases == 1
    assert rec1.total_leases == 1
    assert rec1.last_used_at == now

    store.record_acquire("c1", now)
    rec2 = store.get_record("c1")
    assert rec2 is not None
    assert rec2.in_flight_leases == 2
    assert rec2.total_leases == 2


@pytest.mark.asyncio
async def test_record_acquire_async(test_clock: TestClock) -> None:
    """Verify record_acquire_async."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    await store.record_acquire_async("c1", now)
    rec = await store.get_record_async("c1")
    assert rec is not None
    assert rec.in_flight_leases == 1


def test_record_outcome_success(test_clock: TestClock) -> None:
    """Verify SUCCESS outcome decrements in-flight, clears failures and cooldown."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    store.record_acquire("c1", now)
    store.record_outcome("c1", Outcome.success(), now)

    rec = store.get_record("c1")
    assert rec is not None
    assert rec.state == CredentialState.AVAILABLE
    assert rec.in_flight_leases == 0
    assert rec.consecutive_failures == 0
    assert rec.cooldown_until is None


def test_record_outcome_without_prior_record(test_clock: TestClock) -> None:
    """Verify record_outcome initializes record if not previously present."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    store.record_outcome("c1", Outcome.success(), now)
    rec = store.get_record("c1")
    assert rec is not None
    assert rec.state == CredentialState.AVAILABLE
    assert rec.in_flight_leases == 0


def test_record_outcome_rate_limited_and_recovery(test_clock: TestClock) -> None:
    """Verify RATE_LIMITED sets cooldown_until and auto-recovers after expiration."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    store.record_acquire("c1", now)
    store.record_outcome("c1", Outcome.rate_limited(retry_after=20.0), now)

    rec = store.get_record("c1")
    assert rec is not None
    assert rec.state == CredentialState.RATE_LIMITED
    assert rec.in_flight_leases == 0
    assert rec.consecutive_failures == 0
    assert rec.cooldown_until is not None

    # Before cooldown
    test_clock.advance(10.0)
    assert store.get_record("c1") is not None
    assert store.get_record("c1").state == CredentialState.RATE_LIMITED  # type: ignore[union-attr]

    # After cooldown expires (21s advanced)
    test_clock.advance(11.0)
    recovered = store.get_record("c1")
    assert recovered is not None
    assert recovered.state == CredentialState.AVAILABLE
    assert recovered.cooldown_until is None


def test_record_outcome_auth_failed(test_clock: TestClock) -> None:
    """Verify AUTH_FAILED sets state to REVOKED."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    store.record_acquire("c1", now)
    store.record_outcome("c1", Outcome.auth_failed(), now)

    rec = store.get_record("c1")
    assert rec is not None
    assert rec.state == CredentialState.REVOKED
    assert rec.in_flight_leases == 0

    # Never auto-recovers
    test_clock.advance(10000.0)
    assert store.get_record("c1").state == CredentialState.REVOKED  # type: ignore[union-attr]


def test_record_outcome_quota_exhausted(test_clock: TestClock) -> None:
    """Verify QUOTA_EXHAUSTED outcome behavior."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    store.record_acquire("c1", now)
    store.record_outcome("c1", Outcome.quota_exhausted(retry_after=30.0), now)

    rec = store.get_record("c1")
    assert rec is not None
    assert rec.state == CredentialState.QUOTA_EXHAUSTED
    assert rec.consecutive_failures == 0
    assert rec.cooldown_until is not None

    test_clock.advance(31.0)
    assert store.get_record("c1").state == CredentialState.AVAILABLE  # type: ignore[union-attr]


def test_record_outcome_repeated_failures_escalate(test_clock: TestClock) -> None:
    """Verify repeated transient errors escalate to UNHEALTHY at threshold."""
    store = MemoryStateStore(clock=test_clock, max_consecutive_failures=2)
    now = test_clock.now()

    # Failure 1
    store.record_acquire("c1", now)
    store.record_outcome("c1", Outcome.transient_error(retry_after=5.0), now)
    assert store.get_record("c1").state == CredentialState.COOLDOWN  # type: ignore[union-attr]

    # Advance clock past cooldown
    test_clock.advance(6.0)
    assert store.get_record("c1").state == CredentialState.AVAILABLE  # type: ignore[union-attr]

    # Failure 2 (reaches max=2)
    store.record_acquire("c1", test_clock.now())
    store.record_outcome("c1", Outcome.transient_error(retry_after=5.0), test_clock.now())

    rec = store.get_record("c1")
    assert rec is not None
    assert rec.state == CredentialState.UNHEALTHY
    assert rec.consecutive_failures == 2


def test_reset_clears_failures_and_unhealthy(test_clock: TestClock) -> None:
    """Verify reset restores credential to AVAILABLE."""
    store = MemoryStateStore(clock=test_clock)
    store.record_acquire("c1", test_clock.now())
    store.record_outcome("c1", Outcome.consecutive_failures_exceeded(), test_clock.now())

    assert store.get_record("c1").state == CredentialState.UNHEALTHY  # type: ignore[union-attr]

    store.reset("c1")
    rec = store.get_record("c1")
    assert rec is not None
    assert rec.state == CredentialState.AVAILABLE
    assert rec.consecutive_failures == 0


def test_concurrent_acquire_and_report(test_clock: TestClock) -> None:
    """Verify thread safety under heavy concurrent acquire and report operations."""
    store = MemoryStateStore(clock=test_clock)
    store.initialize_record("c1")

    def worker(idx: int) -> None:
        t = datetime.now(timezone.utc)
        store.record_acquire("c1", t)
        if idx % 2 == 0:
            store.record_outcome("c1", Outcome.success(), t)
        else:
            store.record_outcome("c1", Outcome.rate_limited(retry_after=1.0), t)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(worker, i) for i in range(100)]
        for f in futures:
            f.result()

    rec = store.get_record("c1")
    assert rec is not None
    assert rec.in_flight_leases == 0
    assert rec.total_leases == 100


def test_release_lease_decrements_in_flight_without_changing_state(test_clock: TestClock) -> None:
    """Verify release_lease decrements in-flight leases without altering health or failures."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    store.record_acquire("c1", now)
    assert store.get_record("c1").in_flight_leases == 1  # type: ignore[union-attr]

    store.release_lease("c1")
    rec = store.get_record("c1")
    assert rec is not None
    assert rec.in_flight_leases == 0
    assert rec.state == CredentialState.AVAILABLE
    assert rec.consecutive_failures == 0


@pytest.mark.asyncio
async def test_release_lease_async(test_clock: TestClock) -> None:
    """Verify release_lease_async decrements in-flight leases asynchronously."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    store.record_acquire("c1", now)
    await store.release_lease_async("c1")
    rec = await store.get_record_async("c1")
    assert rec is not None
    assert rec.in_flight_leases == 0


def test_late_success_never_resurrects_revoked(test_clock: TestClock) -> None:
    """Verify a late SUCCESS outcome from another lease never resurrects a REVOKED credential."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    # Simulate 2 concurrent leases acquired
    store.record_acquire("c1", now)
    store.record_acquire("c1", now)
    assert store.get_record("c1").in_flight_leases == 2  # type: ignore[union-attr]

    # Lease 1 fails with AUTH_FAILED -> REVOKED
    store.record_outcome("c1", Outcome.auth_failed(reason="Revoked key"), now)
    rec1 = store.get_record("c1")
    assert rec1 is not None
    assert rec1.state == CredentialState.REVOKED
    assert rec1.in_flight_leases == 1

    # Lease 2 finishes slightly later with SUCCESS -> must NOT resurrect REVOKED
    test_clock.advance(1.0)
    later = test_clock.now()
    store.record_outcome("c1", Outcome.success(), later)

    rec2 = store.get_record("c1")
    assert rec2 is not None
    assert rec2.state == CredentialState.REVOKED
    assert rec2.in_flight_leases == 0


def test_late_success_never_resurrects_unhealthy(test_clock: TestClock) -> None:
    """Verify a late SUCCESS outcome never resurrects an UNHEALTHY credential."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    # Acquire 2 leases
    store.record_acquire("c1", now)
    store.record_acquire("c1", now)

    # Lease 1 fails permanently -> UNHEALTHY
    store.record_outcome("c1", Outcome.permanent_failure(reason="Corrupt provider response"), now)
    assert store.get_record("c1").state == CredentialState.UNHEALTHY  # type: ignore[union-attr]

    # Lease 2 reports late SUCCESS
    test_clock.advance(1.0)
    store.record_outcome("c1", Outcome.success(), test_clock.now())

    rec = store.get_record("c1")
    assert rec is not None
    assert rec.state == CredentialState.UNHEALTHY
    assert rec.in_flight_leases == 0


def test_late_success_never_clears_active_rate_limit_cooldown(test_clock: TestClock) -> None:
    """Verify a late SUCCESS outcome never clears active RATE_LIMITED cooldown."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    store.record_acquire("c1", now)
    store.record_acquire("c1", now)

    # Lease 1 hit 429
    store.record_outcome("c1", Outcome.rate_limited(retry_after=60.0), now)
    rec1 = store.get_record("c1")
    assert rec1 is not None
    assert rec1.state == CredentialState.RATE_LIMITED
    expected_cooldown = rec1.cooldown_until
    assert expected_cooldown is not None

    # Lease 2 reports late SUCCESS while cooldown is still active
    test_clock.advance(10.0)
    store.record_outcome("c1", Outcome.success(), test_clock.now())

    rec2 = store.get_record("c1")
    assert rec2 is not None
    assert rec2.state == CredentialState.RATE_LIMITED
    assert rec2.cooldown_until == expected_cooldown
    assert rec2.in_flight_leases == 0


def test_late_success_never_clears_active_transient_cooldown(test_clock: TestClock) -> None:
    """Verify a late SUCCESS outcome never clears active COOLDOWN from transient error."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    store.record_acquire("c1", now)
    store.record_acquire("c1", now)

    # Lease 1 hit transient error
    store.record_outcome("c1", Outcome.transient_error(retry_after=30.0), now)
    rec1 = store.get_record("c1")
    assert rec1 is not None
    assert rec1.state == CredentialState.COOLDOWN
    expected_cooldown = rec1.cooldown_until

    # Lease 2 reports late SUCCESS while cooldown is still active
    test_clock.advance(5.0)
    store.record_outcome("c1", Outcome.success(), test_clock.now())

    rec2 = store.get_record("c1")
    assert rec2 is not None
    assert rec2.state == CredentialState.COOLDOWN
    assert rec2.cooldown_until == expected_cooldown
    assert rec2.in_flight_leases == 0


def test_rate_limited_does_not_increment_consecutive_failures_or_trigger_unhealthy(
    test_clock: TestClock,
) -> None:
    """Verify repeated RATE_LIMITED outcomes never make a credential UNHEALTHY."""
    store = MemoryStateStore(clock=test_clock, max_consecutive_failures=2)

    # Repeatedly rate limit 5 times consecutively across cooldown expirations
    for _ in range(5):
        now = test_clock.now()
        store.record_acquire("c1", now)
        store.record_outcome("c1", Outcome.rate_limited(retry_after=10.0), now)

        rec = store.get_record("c1")
        assert rec is not None
        assert rec.state == CredentialState.RATE_LIMITED
        assert rec.consecutive_failures == 0

        # Advance past cooldown to auto-recover to AVAILABLE
        test_clock.advance(11.0)
        recovered = store.get_record("c1")
        assert recovered is not None
        assert recovered.state == CredentialState.AVAILABLE

    # The credential is STILL healthy and was NEVER marked UNHEALTHY
    final_rec = store.get_record("c1")
    assert final_rec is not None
    assert final_rec.state == CredentialState.AVAILABLE
    assert final_rec.consecutive_failures == 0


def test_transient_error_uses_configured_default_cooldown(test_clock: TestClock) -> None:
    """Verify TRANSIENT_ERROR uses configured default_cooldown instead of hardcoded 5.0."""
    store = MemoryStateStore(clock=test_clock, default_cooldown=75.0)
    now = test_clock.now()

    store.record_acquire("c1", now)
    # No retry_after passed
    store.record_outcome("c1", Outcome.transient_error(), now)

    rec = store.get_record("c1")
    assert rec is not None
    assert rec.state == CredentialState.COOLDOWN
    assert rec.cooldown_until is not None
    elapsed = (rec.cooldown_until - now).total_seconds()
    assert elapsed == 75.0


def test_quota_exhausted_precedence_over_rate_limited(test_clock: TestClock) -> None:
    """Verify QUOTA_EXHAUSTED takes precedence over RATE_LIMITED in both report orders."""
    now = test_clock.now()

    # Order 1: RATE_LIMITED then QUOTA_EXHAUSTED
    store1 = MemoryStateStore(clock=test_clock)
    store1.record_acquire("c1", now)
    store1.record_acquire("c1", now)
    store1.record_outcome("c1", Outcome.rate_limited(retry_after=10.0), now)
    store1.record_outcome("c1", Outcome.quota_exhausted(retry_after=None), now)
    rec1 = store1.get_record("c1")
    assert rec1 is not None
    assert rec1.state == CredentialState.QUOTA_EXHAUSTED
    assert rec1.consecutive_failures == 0
    assert rec1.cooldown_until is None

    # Order 2: QUOTA_EXHAUSTED then RATE_LIMITED
    store2 = MemoryStateStore(clock=test_clock)
    store2.record_acquire("c1", now)
    store2.record_acquire("c1", now)
    store2.record_outcome("c1", Outcome.quota_exhausted(retry_after=None), now)
    store2.record_outcome("c1", Outcome.rate_limited(retry_after=10.0), now)
    rec2 = store2.get_record("c1")
    assert rec2 is not None
    assert rec2.state == CredentialState.QUOTA_EXHAUSTED
    assert rec2.consecutive_failures == 0
    assert rec2.cooldown_until is None

    assert rec1 == rec2


def test_quota_exhausted_precedence_over_transient_cooldown(test_clock: TestClock) -> None:
    """Verify QUOTA_EXHAUSTED takes precedence over transient COOLDOWN in both report orders."""
    now = test_clock.now()

    # Order A: TRANSIENT_ERROR then QUOTA_EXHAUSTED
    store_a = MemoryStateStore(clock=test_clock)
    store_a.record_acquire("c1", now)
    store_a.record_acquire("c1", now)
    store_a.record_outcome("c1", Outcome.transient_error(retry_after=5.0), now)
    store_a.record_outcome("c1", Outcome.quota_exhausted(retry_after=None), now)
    rec_a = store_a.get_record("c1")
    assert rec_a is not None
    assert rec_a.state == CredentialState.QUOTA_EXHAUSTED
    assert rec_a.consecutive_failures == 1
    assert rec_a.cooldown_until is None

    # Order B: QUOTA_EXHAUSTED then TRANSIENT_ERROR
    store_b = MemoryStateStore(clock=test_clock)
    store_b.record_acquire("c1", now)
    store_b.record_acquire("c1", now)
    store_b.record_outcome("c1", Outcome.quota_exhausted(retry_after=None), now)
    store_b.record_outcome("c1", Outcome.transient_error(retry_after=5.0), now)
    rec_b = store_b.get_record("c1")
    assert rec_b is not None
    assert rec_b.state == CredentialState.QUOTA_EXHAUSTED
    assert rec_b.consecutive_failures == 1
    assert rec_b.cooldown_until is None

    assert rec_a == rec_b


def test_quota_exhausted_does_not_count_toward_consecutive_failures_or_unhealthy(
    test_clock: TestClock,
) -> None:
    """Verify repeated QUOTA_EXHAUSTED never increments failures or triggers UNHEALTHY."""
    store = MemoryStateStore(clock=test_clock, max_consecutive_failures=2)
    now = test_clock.now()

    for _ in range(5):
        store.record_acquire("c1", now)
        store.record_outcome("c1", Outcome.quota_exhausted(retry_after=None), now)
        rec = store.get_record("c1")
        assert rec is not None
        assert rec.state == CredentialState.QUOTA_EXHAUSTED
        assert rec.consecutive_failures == 0

    # 1 transient error followed by quota exhaustion stays at 1 failure; no UNHEALTHY
    store.record_acquire("c1", now)
    store.record_outcome("c1", Outcome.transient_error(retry_after=5.0), now)
    rec_after_transient = store.get_record("c1")
    assert rec_after_transient is not None
    assert rec_after_transient.consecutive_failures == 1

    # Next 5 quota exhaustions do not increment failures to 2 (which would trigger UNHEALTHY)
    for _ in range(5):
        store.record_acquire("c1", now)
        store.record_outcome("c1", Outcome.quota_exhausted(retry_after=None), now)
        rec = store.get_record("c1")
        assert rec is not None
        assert rec.state == CredentialState.QUOTA_EXHAUSTED
        assert rec.consecutive_failures == 1


def test_quota_exhausted_transitions_active_cooldown(test_clock: TestClock) -> None:
    """Verify QUOTA_EXHAUSTED during an active cooldown correctly transitions state."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    # Step 1: credential enters RATE_LIMITED cooldown
    store.record_acquire("c1", now)
    store.record_outcome("c1", Outcome.rate_limited(retry_after=10.0), now)
    rec = store.get_record("c1")
    assert rec is not None
    assert rec.state == CredentialState.RATE_LIMITED

    # Step 2: during active cooldown, QUOTA_EXHAUSTED arrives (with retry_after)
    test_clock.advance(2.0)
    current_time = test_clock.now()
    store.record_acquire("c1", current_time)
    store.record_outcome("c1", Outcome.quota_exhausted(retry_after=30.0), current_time)

    rec2 = store.get_record("c1")
    assert rec2 is not None
    assert rec2.state == CredentialState.QUOTA_EXHAUSTED
    assert rec2.cooldown_until == current_time + timedelta(seconds=30.0)


@pytest.mark.parametrize(
    ("outcomes", "expected_state", "expected_failures", "expected_cooldown_offset"),
    [
        (
            [
                Outcome.rate_limited(retry_after=10.0),
                Outcome.quota_exhausted(retry_after=None),
            ],
            CredentialState.QUOTA_EXHAUSTED,
            0,
            None,
        ),
        (
            [
                Outcome.transient_error(retry_after=5.0),
                Outcome.quota_exhausted(retry_after=None),
            ],
            CredentialState.QUOTA_EXHAUSTED,
            1,
            None,
        ),
        (
            [
                Outcome.rate_limited(retry_after=10.0),
                Outcome.transient_error(retry_after=5.0),
                Outcome.quota_exhausted(retry_after=None),
            ],
            CredentialState.QUOTA_EXHAUSTED,
            1,
            None,
        ),
        (
            [
                Outcome.rate_limited(retry_after=15.0),
                Outcome.transient_error(retry_after=5.0),
                Outcome.quota_exhausted(retry_after=30.0),
            ],
            CredentialState.QUOTA_EXHAUSTED,
            1,
            30.0,
        ),
        (
            [
                Outcome.rate_limited(retry_after=45.0),
                Outcome.transient_error(retry_after=5.0),
                Outcome.quota_exhausted(retry_after=20.0),
            ],
            CredentialState.QUOTA_EXHAUSTED,
            1,
            45.0,
        ),
        (
            [
                Outcome.success(),
                Outcome.rate_limited(retry_after=20.0),
                Outcome.quota_exhausted(retry_after=None),
            ],
            CredentialState.QUOTA_EXHAUSTED,
            0,
            None,
        ),
        (
            [
                Outcome.success(),
                Outcome.transient_error(retry_after=10.0),
                Outcome.rate_limited(retry_after=20.0),
                Outcome.quota_exhausted(retry_after=None),
            ],
            CredentialState.QUOTA_EXHAUSTED,
            1,
            None,
        ),
        (
            [
                Outcome.success(),
                Outcome.rate_limited(retry_after=20.0),
                Outcome.quota_exhausted(retry_after=None),
                Outcome.permanent_failure(),
            ],
            CredentialState.UNHEALTHY,
            1,
            None,
        ),
        (
            [
                Outcome.success(),
                Outcome.rate_limited(retry_after=20.0),
                Outcome.quota_exhausted(retry_after=None),
                Outcome.auth_failed(),
            ],
            CredentialState.REVOKED,
            1,
            None,
        ),
    ],
)
def test_concurrent_outcomes_order_independence(
    test_clock: TestClock,
    outcomes: list[Outcome],
    expected_state: CredentialState,
    expected_failures: int,
    expected_cooldown_offset: float | None,
) -> None:
    """Verify equivalent concurrent outcome sets produce identical state in any order."""
    now = test_clock.now()
    expected_cooldown = (
        now + timedelta(seconds=expected_cooldown_offset)
        if expected_cooldown_offset is not None
        else None
    )

    all_permutations = list(itertools.permutations(outcomes))
    assert len(all_permutations) >= 2

    records: list[CredentialRecord] = []
    for perm in all_permutations:
        store = MemoryStateStore(
            clock=test_clock, default_cooldown=60.0, max_consecutive_failures=3
        )
        # Acquire all leases concurrently
        for _ in range(len(perm)):
            store.record_acquire("c1", now)

        # Report outcomes in permutation order
        for outcome in perm:
            store.record_outcome("c1", outcome, now)

        rec = store.get_record("c1")
        assert rec is not None
        records.append(rec)

    first = records[0]
    assert first.state == expected_state
    assert first.consecutive_failures == expected_failures
    assert first.cooldown_until == expected_cooldown
    assert first.in_flight_leases == 0

    for idx, rec in enumerate(records[1:], start=1):
        assert rec == first, (
            f"Permutation {all_permutations[idx]} produced mismatched record {rec} != {first}"
        )
