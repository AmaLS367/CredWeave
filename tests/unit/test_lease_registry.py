"""Unit tests for the MemoryStateStore lease registry: slot reservation, settlement, reclamation."""

import concurrent.futures
from datetime import timedelta

import pytest

from credweave.application.ports.state_store import (
    LeaseRecord,
    LeaseReservation,
    LeaseSettlement,
    StateStore,
)
from credweave.application.services.lifecycle import LifecycleEngine
from credweave.domain.enums import CredentialState
from credweave.domain.errors import ConfigurationError, InvalidOutcomeError, StateStoreError
from credweave.domain.outcomes import Outcome
from credweave.infrastructure.stores.memory import MemoryStateStore
from tests.conftest import TestClock


def _store(clock: TestClock) -> MemoryStateStore:
    return MemoryStateStore(clock=clock)


def _in_flight(store: MemoryStateStore, credential_id: str) -> int:
    record = store.get_record(credential_id)
    assert record is not None
    return record.in_flight_leases


# --- reserve_lease -------------------------------------------------------------------------


def test_reserve_lease_registers_lease_and_updates_counters(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    expires = now + timedelta(seconds=30)

    assert store.reserve_lease("c1", "l1", now, expires_at=expires) is LeaseReservation.RESERVED

    record = store.get_record("c1")
    assert record is not None
    assert record.in_flight_leases == 1
    assert record.total_leases == 1
    assert record.last_used_at == now
    assert store.list_active_leases() == (
        LeaseRecord(lease_id="l1", credential_id="c1", acquired_at=now, expires_at=expires),
    )


def test_reserve_lease_without_cap_is_unlimited(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    assert all(store.reserve_lease("c1", f"l{i}", now) for i in range(200))
    assert _in_flight(store, "c1") == 200


def test_reserve_lease_refuses_at_capacity_without_side_effects(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    assert store.reserve_lease("c1", "l1", now, max_concurrency=1)
    before = store.get_record("c1")

    later = now + timedelta(seconds=5)
    result = store.reserve_lease("c1", "l2", later, max_concurrency=1)
    assert result is LeaseReservation.AT_CAPACITY
    assert not result

    assert store.get_record("c1") == before
    assert [lease.lease_id for lease in store.list_active_leases()] == ["l1"]


def test_reserve_lease_cap_greater_than_one(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    results = [store.reserve_lease("c1", f"l{i}", now, max_concurrency=3) for i in range(5)]
    assert results == [
        LeaseReservation.RESERVED,
        LeaseReservation.RESERVED,
        LeaseReservation.RESERVED,
        LeaseReservation.AT_CAPACITY,
        LeaseReservation.AT_CAPACITY,
    ]
    assert _in_flight(store, "c1") == 3


def test_reserve_lease_cap_applies_per_call_and_per_credential(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    assert store.reserve_lease("a", "a1", now, max_concurrency=1)
    assert store.reserve_lease("b", "b1", now, max_concurrency=2)
    assert store.reserve_lease("b", "b2", now, max_concurrency=2)
    assert not store.reserve_lease("a", "a2", now, max_concurrency=1)
    assert not store.reserve_lease("b", "b3", now, max_concurrency=2)
    # A raised cap immediately admits more.
    assert store.reserve_lease("a", "a2", now, max_concurrency=2)


def test_reserve_lease_does_not_change_health_state(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    store.reserve_lease("c1", "l1", now, max_concurrency=1)
    store.reserve_lease("c1", "l2", now, max_concurrency=1)
    record = store.get_record("c1")
    assert record is not None
    assert record.state == CredentialState.AVAILABLE
    assert record.consecutive_failures == 0
    assert record.cooldown_until is None


def test_reserve_lease_rejects_duplicate_lease_id(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    store.reserve_lease("c1", "l1", now)
    with pytest.raises(StateStoreError):
        store.reserve_lease("c1", "l1", now)
    assert _in_flight(store, "c1") == 1


@pytest.mark.parametrize("bad", [0, -1, True, "2", 1.5])
def test_reserve_lease_rejects_invalid_cap(test_clock: TestClock, bad: object) -> None:
    store = _store(test_clock)
    with pytest.raises(ConfigurationError):
        store.reserve_lease("c1", "l1", test_clock.now(), max_concurrency=bad)  # type: ignore[arg-type]
    assert store.list_active_leases() == ()


@pytest.mark.asyncio
async def test_reserve_lease_async_matches_sync(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    assert (
        await store.reserve_lease_async("c1", "l1", now, max_concurrency=1)
        is LeaseReservation.RESERVED
    )
    assert (
        await store.reserve_lease_async("c1", "l2", now, max_concurrency=1)
        is LeaseReservation.AT_CAPACITY
    )
    assert [lease.lease_id for lease in await store.list_active_leases_async()] == ["l1"]


# --- settle_lease --------------------------------------------------------------------------


def test_settle_lease_applies_outcome_and_releases_slot(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    store.reserve_lease("c1", "l1", now)

    result = store.settle_lease("l1", "c1", Outcome.rate_limited(retry_after=30.0), now)

    assert result is LeaseSettlement.SETTLED
    record = store.get_record("c1")
    assert record is not None
    assert record.in_flight_leases == 0
    assert record.state == CredentialState.RATE_LIMITED
    assert store.list_active_leases() == ()


def test_settle_lease_twice_releases_exactly_once(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    store.reserve_lease("c1", "l1", now)
    store.reserve_lease("c1", "l2", now)

    assert store.settle_lease("l1", "c1", Outcome.success(), now) is LeaseSettlement.SETTLED
    assert store.settle_lease("l1", "c1", Outcome.success(), now) is LeaseSettlement.UNKNOWN

    assert _in_flight(store, "c1") == 1
    assert [lease.lease_id for lease in store.list_active_leases()] == ["l2"]


def test_settle_unknown_lease_changes_nothing(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    store.reserve_lease("c1", "l1", now)
    before = store.get_record("c1")

    assert store.settle_lease("nope", "c1", Outcome.auth_failed(), now) is LeaseSettlement.UNKNOWN

    assert store.get_record("c1") == before


def test_settle_lease_credential_mismatch_changes_nothing(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    store.reserve_lease("c1", "l1", now)
    store.reserve_lease("c2", "l2", now)
    before = store.list_records()

    assert store.settle_lease("l1", "c2", Outcome.success(), now) is LeaseSettlement.MISMATCH

    assert store.list_records() == before
    assert len(store.list_active_leases()) == 2


def test_settle_expired_lease_releases_slot_without_applying_outcome(
    test_clock: TestClock,
) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    store.reserve_lease("c1", "l1", now, expires_at=now + timedelta(seconds=10))
    test_clock.advance(11.0)

    result = store.settle_lease("l1", "c1", Outcome.auth_failed(), test_clock.now())

    assert result is LeaseSettlement.EXPIRED
    record = store.get_record("c1")
    assert record is not None
    assert record.state == CredentialState.AVAILABLE
    assert record.consecutive_failures == 0
    assert record.in_flight_leases == 0
    # Settling again keeps reporting EXPIRED and never releases a second time.
    assert (
        store.settle_lease("l1", "c1", Outcome.success(), test_clock.now())
        is LeaseSettlement.EXPIRED
    )
    assert _in_flight(store, "c1") == 0


def test_settle_at_exact_deadline_is_not_expired(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    deadline = now + timedelta(seconds=10)
    store.reserve_lease("c1", "l1", now, expires_at=deadline)

    assert store.settle_lease("l1", "c1", Outcome.success(), deadline) is LeaseSettlement.SETTLED


def test_settle_failure_leaves_lease_active_for_retry(test_clock: TestClock) -> None:
    class ExplodingEngine(LifecycleEngine):
        armed = True

        def apply_outcome(self, record, outcome, now):  # type: ignore[no-untyped-def]
            if self.armed:
                raise InvalidOutcomeError("boom")
            return super().apply_outcome(record, outcome, now)

    engine = ExplodingEngine()
    store = MemoryStateStore(clock=test_clock, lifecycle=engine)
    now = test_clock.now()
    store.reserve_lease("c1", "l1", now)

    with pytest.raises(InvalidOutcomeError):
        store.settle_lease("l1", "c1", Outcome.success(), now)

    assert _in_flight(store, "c1") == 1
    assert [lease.lease_id for lease in store.list_active_leases()] == ["l1"]

    engine.armed = False
    assert store.settle_lease("l1", "c1", Outcome.success(), now) is LeaseSettlement.SETTLED
    assert _in_flight(store, "c1") == 0


@pytest.mark.asyncio
async def test_settle_lease_async(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    await store.reserve_lease_async("c1", "l1", now)
    assert (
        await store.settle_lease_async("l1", "c1", Outcome.success(), now)
        is LeaseSettlement.SETTLED
    )
    assert (
        await store.settle_lease_async("l1", "c1", Outcome.success(), now)
        is LeaseSettlement.UNKNOWN
    )


# --- reclaim_expired_leases ----------------------------------------------------------------


def test_reclaim_frees_only_expired_leases(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    store.reserve_lease("c1", "old", now, expires_at=now + timedelta(seconds=10))
    store.reserve_lease("c1", "fresh", now, expires_at=now + timedelta(seconds=100))
    store.reserve_lease("c1", "forever", now)

    reclaimed = store.reclaim_expired_leases(now + timedelta(seconds=50))

    assert [lease.lease_id for lease in reclaimed] == ["old"]
    assert reclaimed[0].credential_id == "c1"
    assert reclaimed[0].acquired_at == now
    assert _in_flight(store, "c1") == 2
    assert {lease.lease_id for lease in store.list_active_leases()} == {"fresh", "forever"}


def test_reclaim_boundary_is_strict(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    deadline = now + timedelta(seconds=10)
    store.reserve_lease("c1", "l1", now, expires_at=deadline)

    assert store.reclaim_expired_leases(deadline) == ()
    assert len(store.reclaim_expired_leases(deadline + timedelta(microseconds=1))) == 1


def test_reclaim_is_idempotent(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    store.reserve_lease("c1", "l1", now, expires_at=now + timedelta(seconds=1))
    store.reserve_lease("c1", "l2", now)
    later = now + timedelta(seconds=5)

    assert len(store.reclaim_expired_leases(later)) == 1
    assert store.reclaim_expired_leases(later) == ()
    assert store.reclaim_expired_leases(later + timedelta(days=1)) == ()
    assert _in_flight(store, "c1") == 1


def test_reclaim_preserves_health_state_exactly(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    store.reserve_lease("c1", "l1", now, expires_at=now + timedelta(seconds=10))
    store.reserve_lease("c1", "l2", now)
    store.settle_lease("l2", "c1", Outcome.transient_error(retry_after=100.0), now)
    before = store.get_record("c1")
    assert before is not None
    assert before.state == CredentialState.COOLDOWN
    assert before.consecutive_failures == 1

    store.reclaim_expired_leases(now + timedelta(seconds=20))

    after = store.get_record("c1")
    assert after is not None
    assert after.in_flight_leases == 0
    assert after.state == before.state
    assert after.consecutive_failures == before.consecutive_failures
    assert after.cooldown_until == before.cooldown_until
    assert after.total_leases == before.total_leases
    assert after.last_used_at == before.last_used_at


def test_reclaimed_lease_cannot_be_settled(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    store.reserve_lease("c1", "l1", now, expires_at=now + timedelta(seconds=1))
    store.reclaim_expired_leases(now + timedelta(seconds=5))

    result = store.settle_lease("l1", "c1", Outcome.auth_failed(), now + timedelta(seconds=5))

    assert result is LeaseSettlement.EXPIRED
    record = store.get_record("c1")
    assert record is not None
    assert record.state == CredentialState.AVAILABLE
    assert record.in_flight_leases == 0


def test_settled_lease_is_not_later_reclaimed(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    store.reserve_lease("c1", "l1", now, expires_at=now + timedelta(seconds=1))
    store.reserve_lease("c1", "l2", now)
    store.settle_lease("l1", "c1", Outcome.success(), now)

    assert store.reclaim_expired_leases(now + timedelta(seconds=60)) == ()
    assert _in_flight(store, "c1") == 1


def test_reclaim_frees_capacity_for_new_reservations(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    store.reserve_lease("c1", "l1", now, max_concurrency=1, expires_at=now + timedelta(seconds=1))
    assert not store.reserve_lease("c1", "l2", now, max_concurrency=1)

    store.reclaim_expired_leases(now + timedelta(seconds=2))

    assert store.reserve_lease("c1", "l2", now, max_concurrency=1)


def test_reclaim_tombstones_are_bounded(
    test_clock: TestClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(MemoryStateStore, "_TOMBSTONE_LIMIT", 2)
    store = _store(test_clock)
    now = test_clock.now()
    for i in range(4):
        store.reserve_lease("c1", f"l{i}", now, expires_at=now + timedelta(seconds=1))
    store.reclaim_expired_leases(now + timedelta(seconds=5))
    later = now + timedelta(seconds=5)

    # Oldest tombstones age out and degrade to UNKNOWN; the newest stay EXPIRED.
    assert store.settle_lease("l0", "c1", Outcome.success(), later) is LeaseSettlement.UNKNOWN
    assert store.settle_lease("l3", "c1", Outcome.success(), later) is LeaseSettlement.EXPIRED
    assert _in_flight(store, "c1") == 0


def test_expiry_index_does_not_grow_with_settled_leases(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    for i in range(2_000):
        store.reserve_lease("c1", f"l{i}", now, expires_at=now + timedelta(days=1))
        store.settle_lease(f"l{i}", "c1", Outcome.success(), now)
    assert len(store._expiry_heap) <= 2 * len(store.list_active_leases()) + 128


@pytest.mark.asyncio
async def test_reclaim_async(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    await store.reserve_lease_async("c1", "l1", now, expires_at=now + timedelta(seconds=1))
    reclaimed = await store.reclaim_expired_leases_async(now + timedelta(seconds=2))
    assert [lease.lease_id for lease in reclaimed] == ["l1"]


def test_store_satisfies_protocol_with_lease_registry(test_clock: TestClock) -> None:
    assert isinstance(_store(test_clock), StateStore)


# --- atomicity under contention ------------------------------------------------------------


def test_concurrent_reserve_never_exceeds_cap(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    cap = 7

    def attempt(i: int) -> LeaseReservation:
        return store.reserve_lease("c1", f"l{i}", now, max_concurrency=cap)

    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(attempt, range(500)))

    assert results.count(LeaseReservation.RESERVED) == cap
    assert results.count(LeaseReservation.AT_CAPACITY) == 500 - cap
    assert _in_flight(store, "c1") == cap
    assert len(store.list_active_leases()) == cap


def test_concurrent_settle_and_reclaim_release_exactly_once(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    n = 300
    deadline = now + timedelta(seconds=1)
    for i in range(n):
        store.reserve_lease("c1", f"l{i}", now, expires_at=deadline)
    later = now + timedelta(seconds=2)
    statuses: list[LeaseSettlement] = []
    reclaimed_counts: list[int] = []

    def settler(i: int) -> LeaseSettlement:
        return store.settle_lease(f"l{i}", "c1", Outcome.success(), later)

    def reclaimer(_: int) -> int:
        return len(store.reclaim_expired_leases(later))

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        settle_futures = [pool.submit(settler, i) for i in range(n)]
        reclaim_futures = [pool.submit(reclaimer, i) for i in range(50)]
        statuses = [f.result() for f in settle_futures]
        reclaimed_counts = [f.result() for f in reclaim_futures]

    # Every lease was past its deadline: it is released exactly once by one of the two paths.
    assert all(s is LeaseSettlement.EXPIRED for s in statuses)
    assert sum(reclaimed_counts) <= n
    assert _in_flight(store, "c1") == 0
    assert store.list_active_leases() == ()


def test_settle_recreates_a_missing_record(test_clock: TestClock) -> None:
    store = _store(test_clock)
    now = test_clock.now()
    store.reserve_lease("c1", "l1", now)
    store._records.clear()  # e.g. a store whose records were evicted underneath the registry

    assert store.settle_lease("l1", "c1", Outcome.success(), now) is LeaseSettlement.SETTLED
    assert _in_flight(store, "c1") == 0
