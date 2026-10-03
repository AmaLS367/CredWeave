"""Unit tests for the MemoryStateStore adapter."""

import concurrent.futures
from datetime import datetime, timezone

import pytest

from credweave.application.ports.state_store import StateStore
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
    assert rec.consecutive_failures == 1
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
