"""Integration and regression tests for credential rotation and pre-rotation lease safety."""

import concurrent.futures
import threading
from collections.abc import Sequence

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
from tests.conftest import TestClock
from tests.lease_helpers import assert_lease_accounting


def _cred(cid: str, secret: str = "initial-secret") -> Credential:
    return Credential(id=cid, secrets={"key": secret})


class MutableSource(CredentialSource):
    """Dynamic test source with in-memory credential rotation."""

    def __init__(self, credentials: Sequence[Credential]) -> None:
        self._lock = threading.Lock()
        self._credentials = list(credentials)

    def set_credentials(self, credentials: Sequence[Credential]) -> None:
        with self._lock:
            self._credentials = list(credentials)

    def get_credentials(self) -> Sequence[Credential]:
        with self._lock:
            return list(self._credentials)

    async def get_credentials_async(self) -> Sequence[Credential]:
        with self._lock:
            return list(self._credentials)

    @property
    def supports_hot_reload(self) -> bool:
        return True


def test_revoked_credential_recovers_when_secret_changes() -> None:
    """A revoked credential returns to AVAILABLE when its secret actually changes."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    source = MutableSource([_cred("c1", "secret-v1")])
    pool = CredentialPool(source=source, store=store, clock=clock)

    # 1. Lease credential and report AUTH_FAILED (marks REVOKED)
    lease = pool.acquire_sync()
    pool.report_sync(lease, Outcome.auth_failed(reason="invalid key"))

    record = pool.get_record("c1")
    assert record is not None
    assert record.state is CredentialState.REVOKED

    # 2. Source refreshes with identical secret -> stays REVOKED
    source.set_credentials([_cred("c1", "secret-v1")])
    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync()
    record = pool.get_record("c1")
    assert record is not None
    assert record.state is CredentialState.REVOKED

    # 3. Source rotates secret -> recovers to AVAILABLE
    source.set_credentials([_cred("c1", "secret-v2")])
    new_lease = pool.acquire_sync()
    assert new_lease.credential.require_secret("key") == "secret-v2"
    record = pool.get_record("c1")
    assert record is not None
    assert record.state is CredentialState.AVAILABLE
    pool.report_sync(new_lease, Outcome.success())
    assert_lease_accounting(store)


def test_unhealthy_credential_recovers_when_secret_changes() -> None:
    """An unhealthy credential recovers to AVAILABLE when rotated."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    source = MutableSource([_cred("c1", "secret-v1")])
    pool = CredentialPool(source=source, store=store, clock=clock)

    lease = pool.acquire_sync()
    pool.report_sync(lease, Outcome.permanent_failure(reason="broken"))
    record = pool.get_record("c1")
    assert record is not None
    assert record.state is CredentialState.UNHEALTHY

    # Refresh with identical secret -> stays UNHEALTHY
    source.set_credentials([_cred("c1", "secret-v1")])
    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync()
    record = pool.get_record("c1")
    assert record is not None
    assert record.state is CredentialState.UNHEALTHY

    # Rotate secret -> recovers
    source.set_credentials([_cred("c1", "secret-v2")])
    new_lease = pool.acquire_sync()
    assert new_lease.credential.require_secret("key") == "secret-v2"
    record = pool.get_record("c1")
    assert record is not None
    assert record.state is CredentialState.AVAILABLE
    pool.report_sync(new_lease, Outcome.success())
    assert_lease_accounting(store)


def test_pre_rotation_lease_outcome_cannot_corrupt_new_credential() -> None:
    """Outcomes reported from pre-rotation leases release slot without corrupting new state."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    source = MutableSource([_cred("c1", "secret-v1")])
    pool = CredentialPool(source=source, store=store, clock=clock)

    # Worker 1 acquires lease under secret-v1
    old_lease = pool.acquire_sync()
    assert old_lease.credential.require_secret("key") == "secret-v1"
    assert pool.in_flight_leases == 1

    # Operator rotates secret in source
    source.set_credentials([_cred("c1", "secret-v2")])

    # Worker 2 acquires new lease with rotated secret
    new_lease = pool.acquire_sync()
    assert new_lease.credential.require_secret("key") == "secret-v2"
    assert pool.in_flight_leases == 2

    # Worker 1 now reports AUTH_FAILED with the old lease (e.g. from upstream rejected request)
    pool.report_sync(old_lease, Outcome.auth_failed(reason="old key revoked"))

    # Credential c1 MUST NOT be marked REVOKED because the lease was pre-rotation!
    record = pool.get_record("c1")
    assert record is not None
    assert record.state is CredentialState.AVAILABLE
    assert pool.in_flight_leases == 1

    # Worker 2 reports success
    pool.report_sync(new_lease, Outcome.success())
    assert pool.in_flight_leases == 0
    assert_lease_accounting(store)


@pytest.mark.asyncio
async def test_multiple_pools_sharing_store_with_rotation() -> None:
    """Multiple pools sharing a state store coordinate secret rotation safely."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    source_one = MutableSource([_cred("c1", "secret-v1")])
    source_two = MutableSource([_cred("c1", "secret-v1")])
    pool_one = CredentialPool(source=source_one, store=store, clock=clock)
    pool_two = CredentialPool(source=source_two, store=store, clock=clock)

    # Pool 1 acquires and fails
    lease_one = pool_one.acquire_sync()
    pool_one.report_sync(lease_one, Outcome.auth_failed(reason="revoked"))
    rec = store.get_record("c1")
    assert rec is not None
    assert rec.state is CredentialState.REVOKED

    # Pool 2 rotates secret
    source_two.set_credentials([_cred("c1", "secret-v2")])
    lease_two = await pool_two.acquire()
    assert lease_two.credential.require_secret("key") == "secret-v2"
    rec = store.get_record("c1")
    assert rec is not None
    assert rec.state is CredentialState.AVAILABLE

    # Pool 1 updates and immediately acquires
    source_one.set_credentials([_cred("c1", "secret-v2")])
    lease_three = pool_one.acquire_sync()
    assert lease_three.credential.require_secret("key") == "secret-v2"

    await pool_two.report(lease_two, Outcome.success())
    pool_one.report_sync(lease_three, Outcome.success())
    assert_lease_accounting(store)


def test_concurrent_rotation_and_lease_reporting() -> None:
    """Concurrent operations during rapid secret rotations preserve accounting."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    source = MutableSource([_cred("a", "sec-0"), _cred("b", "sec-0")])
    pool = CredentialPool(
        source=source,
        store=store,
        clock=clock,
        max_concurrency_per_credential=10,
    )

    def worker(worker_id: int) -> None:
        for i in range(25):
            lease = None
            try:
                lease = pool.acquire_sync()
                if i % 5 == 0:
                    source.set_credentials(
                        [
                            _cred("a", f"sec-{worker_id}-{i}"),
                            _cred("b", f"sec-{worker_id}-{i}"),
                        ]
                    )
            finally:
                if lease is not None:
                    outcome = Outcome.auth_failed(reason="err") if i % 7 == 0 else Outcome.success()
                    pool.report_sync(lease, outcome)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(worker, idx) for idx in range(8)]
        for f in futures:
            f.result()

    assert pool.in_flight_leases == 0
    assert_lease_accounting(store)
