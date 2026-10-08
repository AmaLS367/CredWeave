"""Pool-level rotation races: competing pools, stale snapshots and rotation sequences.

Interleavings are made deterministic with :class:`_RacingStore`, which runs a hook immediately
before the next reservation, exactly where another pool's rotation would land in production.

Pools observe their starting secrets at construction, so a pool built with a new secret is itself
a rotation. Competing-pool scenarios therefore start every source on the same secret and rotate
explicitly, unless the test is about construction order.
"""

import asyncio
import concurrent.futures
import threading
from collections.abc import Callable, Sequence
from typing import Any

import pytest

from credweave import (
    Credential,
    CredentialPool,
    CredentialSource,
    CredentialState,
    MemoryStateStore,
    NoCredentialsAvailableError,
    Outcome,
)
from credweave.application.ports.state_store import LeaseReservation
from tests.conftest import TestClock
from tests.lease_helpers import assert_lease_accounting


def _cred(cid: str, secret: str) -> Credential:
    return Credential(id=cid, secrets={"key": secret})


class _Source(CredentialSource):
    """Source whose snapshot is changed explicitly by the test."""

    def __init__(self, credentials: Sequence[Credential]) -> None:
        self._lock = threading.Lock()
        self._credentials = list(credentials)

    def set(self, credentials: Sequence[Credential]) -> None:
        with self._lock:
            self._credentials = list(credentials)

    def get_credentials(self) -> Sequence[Credential]:
        with self._lock:
            return list(self._credentials)

    async def get_credentials_async(self) -> Sequence[Credential]:
        return self.get_credentials()

    @property
    def supports_hot_reload(self) -> bool:
        return True


class _RacingStore(MemoryStateStore):
    """Store that lets a test run a competing rotation right before the next reservation."""

    def __init__(self, clock: TestClock) -> None:
        super().__init__(clock=clock)
        self.before_next_reserve: Callable[[], None] | None = None

    def reserve_lease(self, *args: Any, **kwargs: Any) -> LeaseReservation:
        hook, self.before_next_reserve = self.before_next_reserve, None
        if hook is not None:
            hook()
        return super().reserve_lease(*args, **kwargs)


def test_stale_pool_cannot_reactivate_revoked_credential() -> None:
    """A pool still holding the old secret must not revive a credential revoked under a new one."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    old_source = _Source([_cred("c1", "A")])
    new_source = _Source([_cred("c1", "A")])
    old_pool = CredentialPool(source=old_source, store=store, clock=clock)
    new_pool = CredentialPool(source=new_source, store=store, clock=clock)

    old_pool.report_sync(old_pool.acquire_sync(), Outcome.success())
    new_source.set([_cred("c1", "B")])
    new_pool.report_sync(new_pool.acquire_sync(), Outcome.auth_failed(reason="revoked"))
    assert store.get_record("c1").state is CredentialState.REVOKED

    with pytest.raises(NoCredentialsAvailableError):
        old_pool.acquire_sync()
    assert store.get_record("c1").state is CredentialState.REVOKED
    assert_lease_accounting(store)


@pytest.mark.asyncio
async def test_stale_pool_cannot_reactivate_revoked_credential_async() -> None:
    """The async acquire path refuses the superseded secret the same way."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    old_source = _Source([_cred("c1", "A")])
    new_source = _Source([_cred("c1", "A")])
    old_pool = CredentialPool(source=old_source, store=store, clock=clock)
    new_pool = CredentialPool(source=new_source, store=store, clock=clock)

    await old_pool.report(await old_pool.acquire(), Outcome.success())
    new_source.set([_cred("c1", "B")])
    await new_pool.report(await new_pool.acquire(), Outcome.auth_failed(reason="revoked"))
    assert store.get_record("c1").state is CredentialState.REVOKED

    with pytest.raises(NoCredentialsAvailableError):
        await old_pool.acquire()
    assert store.get_record("c1").state is CredentialState.REVOKED
    assert_lease_accounting(store)


def test_rotation_between_selection_and_reservation_never_leases_superseded_secret() -> None:
    """A snapshot selected just before another pool rotated is refused, never handed out."""
    clock = TestClock()
    store = _RacingStore(clock)
    old_source = _Source([_cred("c1", "A")])
    new_source = _Source([_cred("c1", "A")])
    old_pool = CredentialPool(source=old_source, store=store, clock=clock)
    new_pool = CredentialPool(source=new_source, store=store, clock=clock)
    old_pool.report_sync(old_pool.acquire_sync(), Outcome.success())

    held = []

    def rotate() -> None:
        new_source.set([_cred("c1", "B")])
        held.append(new_pool.acquire_sync())  # adopts B as the next generation

    store.before_next_reserve = rotate
    with pytest.raises(NoCredentialsAvailableError):
        old_pool.acquire_sync()

    # Only the lease the competing pool took holds a slot; the superseded selection took none.
    assert old_pool.in_flight_leases == 1
    assert held[0].credential.require_secret("key") == "B"
    assert store.get_record("c1").state is CredentialState.AVAILABLE
    new_pool.report_sync(held[0], Outcome.success())
    assert_lease_accounting(store)


@pytest.mark.asyncio
async def test_rotation_between_selection_and_reservation_async() -> None:
    """The async reservation path enforces the same currency check at the same point."""
    clock = TestClock()
    store = _RacingStore(clock)
    old_source = _Source([_cred("c1", "A")])
    new_source = _Source([_cred("c1", "A")])
    old_pool = CredentialPool(source=old_source, store=store, clock=clock)
    new_pool = CredentialPool(source=new_source, store=store, clock=clock)
    await old_pool.report(await old_pool.acquire(), Outcome.success())

    held = []

    def rotate() -> None:
        new_source.set([_cred("c1", "B")])
        held.append(new_pool.acquire_sync())

    store.before_next_reserve = rotate
    with pytest.raises(NoCredentialsAvailableError):
        await old_pool.acquire()

    assert held[0].credential.require_secret("key") == "B"
    await new_pool.report(held[0], Outcome.success())
    assert_lease_accounting(store)


def test_superseded_candidate_is_skipped_in_favour_of_current_credential() -> None:
    """Within one pool, a superseded credential is skipped and a healthy sibling is leased."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    stale_source = _Source([_cred("c1", "A"), _cred("c2", "X")])
    pool = CredentialPool(source=stale_source, store=store, clock=clock)
    # Constructing the other pool with B is itself a rotation the store observes.
    other = CredentialPool(source=_Source([_cred("c1", "B")]), store=store, clock=clock)
    other.report_sync(other.acquire_sync(), Outcome.success())

    leased_ids = set()
    for _ in range(4):
        lease = pool.acquire_sync()
        leased_ids.add(lease.credential_id)
        pool.report_sync(lease, Outcome.success())
    assert leased_ids == {"c2"}
    assert_lease_accounting(store)


def test_a_b_a_reversion_does_not_apply_outcome_of_the_earlier_a_lease() -> None:
    """A lease granted under the first A cannot revoke the credential after B and a revert to A."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    source_a = _Source([_cred("c1", "A")])
    source_b = _Source([_cred("c1", "A")])
    pool_a = CredentialPool(source=source_a, store=store, clock=clock)
    pool_b = CredentialPool(source=source_b, store=store, clock=clock)

    old_lease = pool_a.acquire_sync()
    source_b.set([_cred("c1", "B")])
    pool_b.report_sync(pool_b.acquire_sync(), Outcome.success())
    source_b.set([_cred("c1", "A")])
    with pytest.raises(NoCredentialsAvailableError):
        pool_b.acquire_sync()

    pool_a.report_sync(old_lease, Outcome.auth_failed(reason="old A rejected"))
    record = store.get_record("c1")
    assert record.state is CredentialState.AVAILABLE
    assert record.consecutive_failures == 0
    assert_lease_accounting(store)


def test_competing_pools_with_unordered_secrets_fail_closed_for_the_earlier_one() -> None:
    """Two distinct secrets that never ordered each other: the later observation wins.

    The store cannot know which of two unseen secrets is newer. It adopts the one it observes
    second and refuses the other, so no pool can flip the credential back and forth.
    """
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    pool_a = CredentialPool(source=_Source([_cred("c1", "A")]), store=store, clock=clock)
    pool_b = CredentialPool(source=_Source([_cred("c1", "B")]), store=store, clock=clock)

    for _ in range(3):
        with pytest.raises(NoCredentialsAvailableError):
            pool_a.acquire_sync()
    lease_b = pool_b.acquire_sync()
    assert lease_b.credential.require_secret("key") == "B"
    pool_b.report_sync(lease_b, Outcome.success())
    assert_lease_accounting(store)


def test_pre_rotation_lease_from_a_competing_pool_keeps_credential_healthy() -> None:
    """A failure reported by a pool still holding the old secret does not penalise the new one."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    source_old = _Source([_cred("c1", "A")])
    source_new = _Source([_cred("c1", "A")])
    pool_old = CredentialPool(source=source_old, store=store, clock=clock)
    pool_new = CredentialPool(source=source_new, store=store, clock=clock)

    old_lease = pool_old.acquire_sync()
    source_new.set([_cred("c1", "B")])
    new_lease = pool_new.acquire_sync()

    pool_old.report_sync(old_lease, Outcome.permanent_failure(reason="stale"))
    assert store.get_record("c1").state is CredentialState.AVAILABLE
    pool_new.report_sync(new_lease, Outcome.success())
    assert pool_new.in_flight_leases == 0
    assert_lease_accounting(store)


@pytest.mark.asyncio
async def test_async_and_sync_pools_sharing_store_agree_on_generations() -> None:
    """Sync and async callers on one store share one generation sequence."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    sync_source = _Source([_cred("c1", "A")])
    async_source = _Source([_cred("c1", "A")])
    sync_pool = CredentialPool(source=sync_source, store=store, clock=clock)
    async_pool = CredentialPool(source=async_source, store=store, clock=clock)

    sync_pool.report_sync(sync_pool.acquire_sync(), Outcome.success())
    async_source.set([_cred("c1", "B")])
    await async_pool.report(await async_pool.acquire(), Outcome.success())

    # The sync pool still holds the superseded secret, so it can no longer lease the credential.
    with pytest.raises(NoCredentialsAvailableError):
        sync_pool.acquire_sync()
    fresh = await async_pool.acquire()
    assert fresh.credential.require_secret("key") == "B"
    await async_pool.report(fresh, Outcome.success())
    assert store.get_record("c1").state is CredentialState.AVAILABLE
    assert_lease_accounting(store)


def test_threaded_competing_pools_preserve_accounting() -> None:
    """Concurrent rotations and reports from competing pools keep the lease registry exact."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    sources = [_Source([_cred("c1", "A"), _cred("c2", "A")]) for _ in range(3)]
    pools = [
        CredentialPool(source=src, store=store, clock=clock, max_concurrency_per_credential=2)
        for src in sources
    ]

    def worker(index: int) -> None:
        pool = pools[index]
        for i in range(30):
            if index == 0 and i % 6 == 0:
                for src in sources:
                    src.set([_cred("c1", f"S{i}"), _cred("c2", f"S{i}")])
            try:
                lease = pool.acquire_sync()
            except NoCredentialsAvailableError:
                continue
            outcome = Outcome.auth_failed(reason="x") if i % 4 == 0 else Outcome.success()
            pool.report_sync(lease, outcome)

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        for future in [executor.submit(worker, idx) for idx in range(3)]:
            future.result()

    assert all(pool.in_flight_leases == 0 for pool in pools)
    assert_lease_accounting(store)


def test_async_pool_callers_share_store_without_leaking_slots() -> None:
    """Concurrent asyncio callers across pools never leave a slot held."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    pools = [
        CredentialPool(source=_Source([_cred("c1", "A")]), store=store, clock=clock)
        for _ in range(4)
    ]

    async def run(pool: CredentialPool) -> None:
        for _ in range(10):
            try:
                lease = await pool.acquire()
            except NoCredentialsAvailableError:
                await asyncio.sleep(0)
                continue
            await pool.report(lease, Outcome.success())

    async def main() -> None:
        await asyncio.gather(*(run(pool) for pool in pools))

    asyncio.run(main())
    assert_lease_accounting(store)
