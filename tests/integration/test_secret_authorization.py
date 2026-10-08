"""Integration tests for explicit authorization, rollback and concurrent rotation.

Thread interleavings vary between runs, so these tests assert invariants that hold for every
interleaving rather than a particular order.
"""

import concurrent.futures
import threading
from collections.abc import Sequence

from credweave import (
    Credential,
    CredentialPool,
    CredentialSource,
    CredentialState,
    MemoryStateStore,
    NoCredentialsAvailableError,
    Outcome,
)
from tests.conftest import TestClock
from tests.lease_helpers import assert_lease_accounting


def _cred(secret: str) -> Credential:
    return Credential(id="c1", secrets={"key": secret})


class SwitchSource(CredentialSource):
    """Thread-safe single-credential source whose secret tests change explicitly."""

    def __init__(self, secret: str) -> None:
        self._lock = threading.Lock()
        self._secret = secret

    def set(self, secret: str) -> None:
        with self._lock:
            self._secret = secret

    def current(self) -> str:
        with self._lock:
            return self._secret

    def get_credentials(self) -> Sequence[Credential]:
        return [_cred(self.current())]

    async def get_credentials_async(self) -> Sequence[Credential]:
        return self.get_credentials()

    @property
    def supports_hot_reload(self) -> bool:
        return True


def test_concurrent_rotations_and_authorizations_converge_on_the_source_secret() -> None:
    """However rotations and authorizations interleave, the store ends on the source's secret."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    source = SwitchSource("sec-start")
    pool = CredentialPool(source=source, store=store, clock=clock, max_concurrency_per_credential=4)

    def worker(worker_id: int) -> None:
        for step in range(20):
            secret = f"sec-{worker_id}-{step}"
            source.set(secret)
            pool.authorize_secret("c1")
            try:
                lease = pool.acquire_sync()
            except NoCredentialsAvailableError:
                continue  # an authorization raced a revocation; the next step recovers it
            outcome = Outcome.auth_failed(reason="race") if step % 6 == 0 else Outcome.success()
            pool.report_sync(lease, outcome)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        for future in [executor.submit(worker, idx) for idx in range(6)]:
            future.result()

    generations = store._secret_generations["c1"]
    # Every generation number is distinct: the history never reuses or loses a number.
    assert len(set(generations.adopted.values())) == len(generations.adopted)
    assert max(generations.adopted.values()) == generations.generation
    # The store is on the secret the source presents, and it is leasable.
    assert generations.current == _cred(source.current()).secret_fingerprint
    final = pool.acquire_sync()
    assert final.credential.require_secret("key") == source.current()
    pool.report_sync(final, Outcome.success())
    assert pool.in_flight_leases == 0
    assert_lease_accounting(store)


def test_leases_granted_before_a_rollback_never_change_the_restored_credential() -> None:
    """Outcomes reported after a rollback, from pre-rollback leases, are discarded."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    source = SwitchSource("A")
    pool = CredentialPool(source=source, store=store, clock=clock)
    pre_rotation = [pool.acquire_sync() for _ in range(2)]

    source.set("B")
    revoked = pool.acquire_sync()
    pool.report_sync(revoked, Outcome.auth_failed(reason="B revoked"))
    assert store.get_record("c1").state is CredentialState.REVOKED

    source.set("A")
    pool.authorize_secret("c1")
    restored = pool.acquire_sync()
    pool.report_sync(restored, Outcome.success())

    for lease in pre_rotation:
        pool.report_sync(lease, Outcome.auth_failed(reason="late report under A"))
    record = store.get_record("c1")
    assert record is not None
    assert record.state is CredentialState.AVAILABLE
    assert record.consecutive_failures == 0
    assert_lease_accounting(store)


def test_async_callers_sharing_a_store_observe_the_same_authorized_secret() -> None:
    """Sync and async authorization write the same generation state into a shared store."""
    import asyncio

    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    source = SwitchSource("A")
    sync_pool = CredentialPool(source=source, store=store, clock=clock)
    async_pool = CredentialPool(source=source, store=store, clock=clock)

    source.set("B")
    sync_pool.acquire_sync()
    source.set("A")
    asyncio.run(async_pool.authorize_secret_async("c1"))

    lease = sync_pool.acquire_sync()
    assert lease.credential.require_secret("key") == "A"
    assert store._secret_generations["c1"].generation == 3
    sync_pool.report_sync(lease, Outcome.success())
    assert_lease_accounting(store)
