"""Unit tests for the MemoryStateStore adapter."""

import concurrent.futures
import itertools
from datetime import timedelta

import pytest

from credweave.application.ports.state_store import (
    CredentialRecord,
    LeaseReservation,
    LeaseSettlement,
    StateStore,
)
from credweave.domain.enums import CredentialState
from credweave.domain.outcomes import Outcome
from credweave.infrastructure.stores.memory import MemoryStateStore
from tests.conftest import TestClock
from tests.lease_helpers import assert_lease_accounting, open_leases, settle


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


def test_reserve_lease_increments_leases(test_clock: TestClock) -> None:
    """Verify reserving leases increments in-flight and total leases and registers them."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    assert store.reserve_lease("c1", "l1", now) is LeaseReservation.RESERVED
    rec1 = store.get_record("c1")
    assert rec1 is not None
    assert rec1.in_flight_leases == 1
    assert rec1.total_leases == 1
    assert rec1.last_used_at == now

    assert store.reserve_lease("c1", "l2", now) is LeaseReservation.RESERVED
    rec2 = store.get_record("c1")
    assert rec2 is not None
    assert rec2.in_flight_leases == 2
    assert rec2.total_leases == 2
    assert_lease_accounting(store)


@pytest.mark.asyncio
async def test_reserve_lease_async(test_clock: TestClock) -> None:
    """Verify reserve_lease_async registers the lease."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    assert await store.reserve_lease_async("c1", "l1", now) is LeaseReservation.RESERVED
    rec = await store.get_record_async("c1")
    assert rec is not None
    assert rec.in_flight_leases == 1
    assert_lease_accounting(store)


def test_legacy_counter_mutators_are_gone(test_clock: TestClock) -> None:
    """The lease registry is the only path that may change in-flight accounting."""
    store = MemoryStateStore(clock=test_clock)
    for name in (
        "record_acquire",
        "record_acquire_async",
        "record_outcome",
        "record_outcome_async",
        "release_lease",
        "release_lease_async",
    ):
        assert not hasattr(store, name), name
        assert not hasattr(StateStore, name), name


def test_settle_success(test_clock: TestClock) -> None:
    """Verify SUCCESS outcome decrements in-flight, clears failures and cooldown."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    (lease_id,) = open_leases(store, "c1", 1, now)
    settle(store, "c1", lease_id, Outcome.success(), now)

    rec = store.get_record("c1")
    assert rec is not None
    assert rec.state == CredentialState.AVAILABLE
    assert rec.in_flight_leases == 0
    assert rec.consecutive_failures == 0
    assert rec.cooldown_until is None
    assert_lease_accounting(store)


def test_settle_unknown_lease_creates_no_record(test_clock: TestClock) -> None:
    """Verify settling a lease that was never reserved changes nothing."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    result = store.settle_lease("ghost", "c1", Outcome.success(), now)

    assert result is LeaseSettlement.UNKNOWN
    assert store.get_record("c1") is None
    assert store.list_active_leases() == ()


def test_settle_rate_limited_and_recovery(test_clock: TestClock) -> None:
    """Verify RATE_LIMITED sets cooldown_until and auto-recovers after expiration."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    (lease_id,) = open_leases(store, "c1", 1, now)
    settle(store, "c1", lease_id, Outcome.rate_limited(retry_after=20.0), now)

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


def test_settle_auth_failed(test_clock: TestClock) -> None:
    """Verify AUTH_FAILED sets state to REVOKED."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    (lease_id,) = open_leases(store, "c1", 1, now)
    settle(store, "c1", lease_id, Outcome.auth_failed(), now)

    rec = store.get_record("c1")
    assert rec is not None
    assert rec.state == CredentialState.REVOKED
    assert rec.in_flight_leases == 0

    # Never auto-recovers
    test_clock.advance(10000.0)
    assert store.get_record("c1").state == CredentialState.REVOKED  # type: ignore[union-attr]


def test_settle_quota_exhausted(test_clock: TestClock) -> None:
    """Verify QUOTA_EXHAUSTED outcome behavior."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    (lease_id,) = open_leases(store, "c1", 1, now)
    settle(store, "c1", lease_id, Outcome.quota_exhausted(retry_after=30.0), now)

    rec = store.get_record("c1")
    assert rec is not None
    assert rec.state == CredentialState.QUOTA_EXHAUSTED
    assert rec.consecutive_failures == 0
    assert rec.cooldown_until is not None

    test_clock.advance(31.0)
    assert store.get_record("c1").state == CredentialState.AVAILABLE  # type: ignore[union-attr]


def test_settle_repeated_failures_escalate(test_clock: TestClock) -> None:
    """Verify repeated transient errors escalate to UNHEALTHY at threshold."""
    store = MemoryStateStore(clock=test_clock, max_consecutive_failures=2)
    now = test_clock.now()

    # Failure 1
    (lease_id,) = open_leases(store, "c1", 1, now)
    settle(store, "c1", lease_id, Outcome.transient_error(retry_after=5.0), now)
    assert store.get_record("c1").state == CredentialState.COOLDOWN  # type: ignore[union-attr]

    # Advance clock past cooldown
    test_clock.advance(6.0)
    assert store.get_record("c1").state == CredentialState.AVAILABLE  # type: ignore[union-attr]

    # Failure 2 (reaches max=2)
    (lease_id,) = open_leases(store, "c1", 1, test_clock.now())
    settle(store, "c1", lease_id, Outcome.transient_error(retry_after=5.0), test_clock.now())

    rec = store.get_record("c1")
    assert rec is not None
    assert rec.state == CredentialState.UNHEALTHY
    assert rec.consecutive_failures == 2


def test_reset_clears_failures_and_unhealthy(test_clock: TestClock) -> None:
    """Verify reset restores credential to AVAILABLE."""
    store = MemoryStateStore(clock=test_clock)
    (lease_id,) = open_leases(store, "c1", 1, test_clock.now())
    settle(store, "c1", lease_id, Outcome.consecutive_failures_exceeded(), test_clock.now())

    assert store.get_record("c1").state == CredentialState.UNHEALTHY  # type: ignore[union-attr]

    store.reset("c1")
    rec = store.get_record("c1")
    assert rec is not None
    assert rec.state == CredentialState.AVAILABLE
    assert rec.consecutive_failures == 0


def test_concurrent_acquire_and_report(test_clock: TestClock) -> None:
    """Verify thread safety under heavy concurrent reserve and settle operations."""
    store = MemoryStateStore(clock=test_clock)
    store.initialize_record("c1")
    now = test_clock.now()
    lease_ids = [f"l{i}" for i in range(100)]

    def reserve(lease_id: str) -> LeaseReservation:
        return store.reserve_lease("c1", lease_id, now)

    def report(idx: int) -> LeaseSettlement:
        outcome = Outcome.success() if idx % 2 == 0 else Outcome.rate_limited(retry_after=1.0)
        return store.settle_lease(lease_ids[idx], "c1", outcome, now)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        reservations = list(executor.map(reserve, lease_ids))
    assert all(r is LeaseReservation.RESERVED for r in reservations)
    assert_lease_accounting(store)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        settlements = list(executor.map(report, range(100)))
    assert all(s is LeaseSettlement.SETTLED for s in settlements)

    rec = store.get_record("c1")
    assert rec is not None
    assert rec.in_flight_leases == 0
    assert rec.total_leases == 100
    assert_lease_accounting(store)


def test_late_success_never_resurrects_revoked(test_clock: TestClock) -> None:
    """Verify a late SUCCESS outcome from another lease never resurrects a REVOKED credential."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    # Simulate 2 concurrent leases acquired
    first, second = open_leases(store, "c1", 2, now)
    assert store.get_record("c1").in_flight_leases == 2  # type: ignore[union-attr]

    # Lease 1 fails with AUTH_FAILED -> REVOKED
    settle(store, "c1", first, Outcome.auth_failed(reason="Revoked key"), now)
    rec1 = store.get_record("c1")
    assert rec1 is not None
    assert rec1.state == CredentialState.REVOKED
    assert rec1.in_flight_leases == 1

    # Lease 2 finishes slightly later with SUCCESS -> must NOT resurrect REVOKED
    test_clock.advance(1.0)
    later = test_clock.now()
    settle(store, "c1", second, Outcome.success(), later)

    rec2 = store.get_record("c1")
    assert rec2 is not None
    assert rec2.state == CredentialState.REVOKED
    assert rec2.in_flight_leases == 0


def test_late_success_never_resurrects_unhealthy(test_clock: TestClock) -> None:
    """Verify a late SUCCESS outcome never resurrects an UNHEALTHY credential."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    first, second = open_leases(store, "c1", 2, now)

    # Lease 1 fails permanently -> UNHEALTHY
    settle(store, "c1", first, Outcome.permanent_failure(reason="Corrupt provider response"), now)
    assert store.get_record("c1").state == CredentialState.UNHEALTHY  # type: ignore[union-attr]

    # Lease 2 reports late SUCCESS
    test_clock.advance(1.0)
    settle(store, "c1", second, Outcome.success(), test_clock.now())

    rec = store.get_record("c1")
    assert rec is not None
    assert rec.state == CredentialState.UNHEALTHY
    assert rec.in_flight_leases == 0


def test_late_success_never_clears_active_rate_limit_cooldown(test_clock: TestClock) -> None:
    """Verify a late SUCCESS outcome never clears active RATE_LIMITED cooldown."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    first, second = open_leases(store, "c1", 2, now)

    # Lease 1 hit 429
    settle(store, "c1", first, Outcome.rate_limited(retry_after=60.0), now)
    rec1 = store.get_record("c1")
    assert rec1 is not None
    assert rec1.state == CredentialState.RATE_LIMITED
    expected_cooldown = rec1.cooldown_until
    assert expected_cooldown is not None

    # Lease 2 reports late SUCCESS while cooldown is still active
    test_clock.advance(10.0)
    settle(store, "c1", second, Outcome.success(), test_clock.now())

    rec2 = store.get_record("c1")
    assert rec2 is not None
    assert rec2.state == CredentialState.RATE_LIMITED
    assert rec2.cooldown_until == expected_cooldown
    assert rec2.in_flight_leases == 0


def test_late_success_never_clears_active_transient_cooldown(test_clock: TestClock) -> None:
    """Verify a late SUCCESS outcome never clears active COOLDOWN from transient error."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    first, second = open_leases(store, "c1", 2, now)

    # Lease 1 hit transient error
    settle(store, "c1", first, Outcome.transient_error(retry_after=30.0), now)
    rec1 = store.get_record("c1")
    assert rec1 is not None
    assert rec1.state == CredentialState.COOLDOWN
    expected_cooldown = rec1.cooldown_until

    # Lease 2 reports late SUCCESS while cooldown is still active
    test_clock.advance(5.0)
    settle(store, "c1", second, Outcome.success(), test_clock.now())

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
        (lease_id,) = open_leases(store, "c1", 1, now)
        settle(store, "c1", lease_id, Outcome.rate_limited(retry_after=10.0), now)

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

    (lease_id,) = open_leases(store, "c1", 1, now)
    # No retry_after passed
    settle(store, "c1", lease_id, Outcome.transient_error(), now)

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
    first, second = open_leases(store1, "c1", 2, now)
    settle(store1, "c1", first, Outcome.rate_limited(retry_after=10.0), now)
    settle(store1, "c1", second, Outcome.quota_exhausted(retry_after=None), now)
    rec1 = store1.get_record("c1")
    assert rec1 is not None
    assert rec1.state == CredentialState.QUOTA_EXHAUSTED
    assert rec1.consecutive_failures == 0
    assert rec1.cooldown_until is None

    # Order 2: QUOTA_EXHAUSTED then RATE_LIMITED
    store2 = MemoryStateStore(clock=test_clock)
    first, second = open_leases(store2, "c1", 2, now)
    settle(store2, "c1", first, Outcome.quota_exhausted(retry_after=None), now)
    settle(store2, "c1", second, Outcome.rate_limited(retry_after=10.0), now)
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
    first, second = open_leases(store_a, "c1", 2, now)
    settle(store_a, "c1", first, Outcome.transient_error(retry_after=5.0), now)
    settle(store_a, "c1", second, Outcome.quota_exhausted(retry_after=None), now)
    rec_a = store_a.get_record("c1")
    assert rec_a is not None
    assert rec_a.state == CredentialState.QUOTA_EXHAUSTED
    assert rec_a.consecutive_failures == 1
    assert rec_a.cooldown_until is None

    # Order B: QUOTA_EXHAUSTED then TRANSIENT_ERROR
    store_b = MemoryStateStore(clock=test_clock)
    first, second = open_leases(store_b, "c1", 2, now)
    settle(store_b, "c1", first, Outcome.quota_exhausted(retry_after=None), now)
    settle(store_b, "c1", second, Outcome.transient_error(retry_after=5.0), now)
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

    # An exhausted credential takes no new leases, so every lease is opened up front.
    leases = open_leases(store, "c1", 11, now)

    for lease_id in leases[:5]:
        settle(store, "c1", lease_id, Outcome.quota_exhausted(retry_after=None), now)
        rec = store.get_record("c1")
        assert rec is not None
        assert rec.state == CredentialState.QUOTA_EXHAUSTED
        assert rec.consecutive_failures == 0

    # 1 transient error followed by quota exhaustion stays at 1 failure; no UNHEALTHY
    settle(store, "c1", leases[5], Outcome.transient_error(retry_after=5.0), now)
    rec_after_transient = store.get_record("c1")
    assert rec_after_transient is not None
    assert rec_after_transient.consecutive_failures == 1

    # Next 5 quota exhaustions do not increment failures to 2 (which would trigger UNHEALTHY)
    for lease_id in leases[6:]:
        settle(store, "c1", lease_id, Outcome.quota_exhausted(retry_after=None), now)
        rec = store.get_record("c1")
        assert rec is not None
        assert rec.state == CredentialState.QUOTA_EXHAUSTED
        assert rec.consecutive_failures == 1
    assert_lease_accounting(store)


def test_quota_exhausted_transitions_active_cooldown(test_clock: TestClock) -> None:
    """Verify QUOTA_EXHAUSTED during an active cooldown correctly transitions state."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()
    first, second = open_leases(store, "c1", 2, now)

    # Step 1: credential enters RATE_LIMITED cooldown
    settle(store, "c1", first, Outcome.rate_limited(retry_after=10.0), now)
    rec = store.get_record("c1")
    assert rec is not None
    assert rec.state == CredentialState.RATE_LIMITED

    # Step 2: during active cooldown, QUOTA_EXHAUSTED arrives (with retry_after)
    test_clock.advance(2.0)
    current_time = test_clock.now()
    settle(store, "c1", second, Outcome.quota_exhausted(retry_after=30.0), current_time)

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
        lease_ids = open_leases(store, "c1", len(perm), now)

        # Report outcomes in permutation order
        for lease_id, outcome in zip(lease_ids, perm, strict=True):
            settle(store, "c1", lease_id, outcome, now)

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
