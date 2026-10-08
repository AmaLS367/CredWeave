"""Deterministic races on secret generations: stale cross-pool sources and same-pool interleavings.

Each interleaving is forced by a hook that fires at one exact point (a source read, a store
adoption or a reservation), so the outcome never depends on thread or scheduler timing. The
threaded case blocks the caller until the competing operation has finished.
"""

import asyncio
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
from credweave.application.ports.state_store import (
    CredentialRecord,
    LeaseRecord,
    LeaseReservation,
)
from tests.conftest import TestClock
from tests.lease_helpers import assert_lease_accounting


def _cred(secret: str) -> Credential:
    return Credential(id="c1", secrets={"key": secret})


def _fp(secret: str) -> str:
    return _cred(secret).secret_fingerprint


class _Source(CredentialSource):
    """Source whose presented secret is changed explicitly by the test.

    ``before_async_return`` runs after an asynchronous read has taken its snapshot and before the
    snapshot is returned, so the reader holds a value that is already stale when it resumes.
    """

    def __init__(self, secret: str) -> None:
        self._lock = threading.Lock()
        self._secret = secret
        self.before_async_return: Callable[[], None] | None = None

    def set(self, secret: str) -> None:
        with self._lock:
            self._secret = secret

    def _snapshot(self) -> Sequence[Credential]:
        with self._lock:
            return [_cred(self._secret)]

    def get_credentials(self) -> Sequence[Credential]:
        return self._snapshot()

    async def get_credentials_async(self) -> Sequence[Credential]:
        snapshot = self._snapshot()
        hook, self.before_async_return = self.before_async_return, None
        if hook is not None:
            hook()
        return snapshot

    @property
    def supports_hot_reload(self) -> bool:
        return True


class _HookedStore(MemoryStateStore):
    """Store that runs one competing operation right before the next secret synchronisation."""

    def __init__(self, clock: TestClock) -> None:
        super().__init__(clock=clock)
        self.before_next_sync: Callable[[], None] | None = None
        self.before_next_reserve: Callable[[], None] | None = None

    def sync_credential(self, *args: Any, **kwargs: Any) -> CredentialRecord:
        hook, self.before_next_sync = self.before_next_sync, None
        if hook is not None:
            hook()
        return super().sync_credential(*args, **kwargs)

    def reserve_lease(self, *args: Any, **kwargs: Any) -> LeaseReservation:
        hook, self.before_next_reserve = self.before_next_reserve, None
        if hook is not None:
            hook()
        return super().reserve_lease(*args, **kwargs)


def _lease_record(store: MemoryStateStore, lease_id: str) -> LeaseRecord:
    return next(r for r in store.list_active_leases() if r.lease_id == lease_id)


# --------------------------------------------------------------------------- race 1: cross-pool


def test_pool_built_later_with_an_older_unseen_secret_does_not_roll_the_store_back() -> None:
    """Constructing a pool over a store already at a newer secret must not adopt its older one."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    newer = CredentialPool(source=_Source("B"), store=store, clock=clock)
    older = CredentialPool(source=_Source("A"), store=store, clock=clock)

    lease = newer.acquire_sync()
    assert lease.credential.require_secret("key") == "B"
    with pytest.raises(NoCredentialsAvailableError):
        older.acquire_sync()

    newer.report_sync(lease, Outcome.success())
    assert store.get_record("c1").state is CredentialState.AVAILABLE
    assert_lease_accounting(store)


def test_out_of_sync_pool_cannot_advance_the_store_with_an_unseen_secret() -> None:
    """A pool that missed the latest rotation cannot make its unseen secret the next generation."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    source_stale = _Source("A")
    stale = CredentialPool(source=source_stale, store=store, clock=clock)
    source_rotating = _Source("A")
    rotating = CredentialPool(source=source_rotating, store=store, clock=clock)

    source_rotating.set("B")
    rotating.report_sync(rotating.acquire_sync(), Outcome.success())

    source_stale.set("C")  # unseen, and the stale pool never observed B
    with pytest.raises(NoCredentialsAvailableError):
        stale.acquire_sync()

    lease = rotating.acquire_sync()
    assert lease.credential.require_secret("key") == "B"
    rotating.report_sync(lease, Outcome.success())
    assert_lease_accounting(store)


def test_unseen_secret_read_before_a_competing_rotation_is_refused_when_it_lands() -> None:
    """A stale pool's unseen secret, read before a rotation lands, is refused at synchronisation."""
    clock = TestClock()
    store = _HookedStore(clock)
    source_p = _Source("A")
    pool_p = CredentialPool(source=source_p, store=store, clock=clock)
    source_q = _Source("A")
    pool_q = CredentialPool(source=source_q, store=store, clock=clock)

    source_p.set("C")
    source_q.set("B")
    held: list[Any] = []
    store.before_next_sync = lambda: held.append(pool_q.acquire_sync())  # B lands first

    with pytest.raises(NoCredentialsAvailableError):
        pool_p.acquire_sync()

    assert held[0].credential.require_secret("key") == "B"
    pool_q.report_sync(held[0], Outcome.success())
    assert_lease_accounting(store)


# ------------------------------------------------------------- race 2: same-pool interleavings


def test_rotation_during_sync_acquire_is_retried_with_the_new_secret() -> None:
    """A snapshot superseded between selection and reservation is re-read, not turned to failure.

    The superseded candidate is refused by the store (STALE) and the pool must then pick up the
    source's current secret instead of excluding the credential for the rest of the call.
    """
    clock = TestClock()
    store = _HookedStore(clock)
    source_old = _Source("A")
    pool_old = CredentialPool(source=source_old, store=store, clock=clock)
    source_new = _Source("A")
    pool_new = CredentialPool(source=source_new, store=store, clock=clock)

    held: list[Any] = []

    def rotate() -> None:
        source_old.set("B")
        source_new.set("B")
        held.append(pool_new.acquire_sync())  # adopts B while old still holds the A snapshot

    store.before_next_reserve = rotate
    lease = pool_old.acquire_sync()

    assert lease.credential.require_secret("key") == "B"
    assert _lease_record(store, lease.lease_id).secret_fingerprint == _fp("B")
    pool_old.report_sync(lease, Outcome.success())
    pool_new.report_sync(held[0], Outcome.success())
    assert_lease_accounting(store)


@pytest.mark.asyncio
async def test_async_acquire_rereads_when_a_sync_rotation_lands_during_its_source_read() -> None:
    """An async acquire never reserves from a snapshot that a sync rotation has overtaken."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    source = _Source("A")
    pool = CredentialPool(source=source, store=store, clock=clock)

    held: list[Any] = []

    def rotate() -> None:
        source.set("B")
        held.append(pool.acquire_sync())  # sync path adopts B while the async read is pending

    source.before_async_return = rotate
    lease = await pool.acquire()

    assert lease.credential.require_secret("key") == "B"
    await pool.report(lease, Outcome.success())
    pool.report_sync(held[0], Outcome.success())
    assert_lease_accounting(store)


@pytest.mark.asyncio
async def test_async_authorize_never_reactivates_a_secret_the_source_has_left() -> None:
    """A stale authorization must not roll back to a superseded secret or open a new generation."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    source = _Source("A")
    pool = CredentialPool(source=source, store=store, clock=clock)

    held: list[Any] = []

    def rotate() -> None:
        source.set("B")
        held.append(pool.acquire_sync())  # the sync path adopts B; authorize still holds A

    source.before_async_return = rotate
    await pool.authorize_secret_async("c1")

    fresh = pool.acquire_sync()
    assert fresh.credential.require_secret("key") == "B"
    # B was already current, so authorizing it opens no generation: both leases share one.
    assert _lease_record(store, fresh.lease_id).secret_generation == 2
    assert _lease_record(store, fresh.lease_id).secret_fingerprint == _fp("B")
    pool.report_sync(held[0], Outcome.success())
    pool.report_sync(fresh, Outcome.success())
    assert_lease_accounting(store)


def test_async_acquire_interleaved_with_sync_rotation_in_another_thread() -> None:
    """Mixed threads: a rotation run in another thread during an async read is never lost."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    source = _Source("A")
    pool = CredentialPool(source=source, store=store, clock=clock)

    held: list[Any] = []

    def rotate_in_worker_thread() -> None:
        source.set("B")
        worker = threading.Thread(target=lambda: held.append(pool.acquire_sync()))
        worker.start()
        worker.join()

    async def scenario() -> Any:
        source.before_async_return = rotate_in_worker_thread
        return await pool.acquire()  # its read returns A, which is already superseded

    lease = asyncio.run(scenario())

    assert lease.credential.require_secret("key") == "B"
    assert _lease_record(store, lease.lease_id).secret_fingerprint == _fp("B")
    pool.report_sync(held[0], Outcome.success())
    pool.report_sync(lease, Outcome.success())
    assert_lease_accounting(store)


def test_threads_and_asyncio_pools_sharing_a_store_keep_accounting_exact() -> None:
    """Invariant stress: sync threads and an asyncio pool hammer one store while the secret rotates.

    Scheduling varies between runs, so this asserts only properties that hold for every
    interleaving: no error other than NoCredentialsAvailableError escapes, every lease is
    accounted for, and every lease carries a secret the source actually presented.
    """
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    secrets = [f"S{i}" for i in range(6)]
    sources = [_Source("S0") for _ in range(4)]
    sync_pools = [
        CredentialPool(source=src, store=store, clock=clock, max_concurrency_per_credential=2)
        for src in sources[:3]
    ]
    async_pool = CredentialPool(
        source=sources[3], store=store, clock=clock, max_concurrency_per_credential=2
    )
    allowed = {_fp(secret) for secret in secrets}
    failures: list[BaseException] = []
    fingerprints: set[str] = set()
    guard = threading.Lock()

    def record(lease: Any) -> None:
        with guard:
            fingerprints.add(lease.credential.secret_fingerprint)

    def sync_worker(pool: CredentialPool) -> None:
        try:
            for _ in range(200):
                try:
                    lease = pool.acquire_sync()
                except NoCredentialsAvailableError:
                    continue
                record(lease)
                pool.report_sync(lease, Outcome.success())
        except BaseException as exc:  # pragma: no cover - reported below
            failures.append(exc)

    async def async_worker() -> None:
        for _ in range(200):
            try:
                lease = await async_pool.acquire()
            except NoCredentialsAvailableError:
                await asyncio.sleep(0)
                continue
            record(lease)
            await async_pool.report(lease, Outcome.success())

    def asyncio_worker() -> None:
        try:
            asyncio.run(async_worker())
        except BaseException as exc:  # pragma: no cover - reported below
            failures.append(exc)

    def rotator() -> None:
        for secret in secrets[1:]:
            for src in sources:
                src.set(secret)

    threads = [threading.Thread(target=sync_worker, args=(pool,)) for pool in sync_pools]
    threads.append(threading.Thread(target=asyncio_worker))
    threads.append(threading.Thread(target=rotator))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert fingerprints, "no lease was ever granted, so the run exercised nothing"
    assert fingerprints <= allowed
    assert all(pool.in_flight_leases == 0 for pool in [*sync_pools, async_pool])
    assert_lease_accounting(store)
