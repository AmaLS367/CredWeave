"""Integration and regression tests for credential rotation and pre-rotation lease safety."""

import concurrent.futures
import contextlib
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


def test_revoked_credential_recovers_only_when_authorized() -> None:
    """A rotation alone never reactivates a REVOKED credential; explicit authorization does."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    source = MutableSource([_cred("c1", "secret-v1")])
    pool = CredentialPool(source=source, store=store, clock=clock)

    # 1. Lease credential and report AUTH_FAILED (marks REVOKED)
    lease = pool.acquire_sync()
    pool.report_sync(lease, Outcome.auth_failed(reason="invalid key"))
    assert _state(pool) is CredentialState.REVOKED

    # 2. Source refreshes with identical secret -> stays REVOKED
    source.set_credentials([_cred("c1", "secret-v1")])
    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync()
    assert _state(pool) is CredentialState.REVOKED

    # 3. Source rotates secret -> still REVOKED: an unknown secret is not authorization
    source.set_credentials([_cred("c1", "secret-v2")])
    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync()
    assert _state(pool) is CredentialState.REVOKED

    # 4. Explicit authorization of the presented secret recovers the credential
    pool.authorize_secret("c1")
    new_lease = pool.acquire_sync()
    assert new_lease.credential.require_secret("key") == "secret-v2"
    assert _state(pool) is CredentialState.AVAILABLE
    pool.report_sync(new_lease, Outcome.success())
    assert_lease_accounting(store)


def test_unhealthy_credential_recovers_only_when_authorized() -> None:
    """An unhealthy credential is not recovered by rotation; authorization recovers it."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    source = MutableSource([_cred("c1", "secret-v1")])
    pool = CredentialPool(source=source, store=store, clock=clock)

    lease = pool.acquire_sync()
    pool.report_sync(lease, Outcome.permanent_failure(reason="broken"))
    assert _state(pool) is CredentialState.UNHEALTHY

    # Refresh with identical secret -> stays UNHEALTHY
    source.set_credentials([_cred("c1", "secret-v1")])
    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync()
    assert _state(pool) is CredentialState.UNHEALTHY

    # Rotate secret -> still UNHEALTHY until the rotated secret is authorized
    source.set_credentials([_cred("c1", "secret-v2")])
    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync()
    assert _state(pool) is CredentialState.UNHEALTHY

    pool.authorize_secret("c1")
    new_lease = pool.acquire_sync()
    assert new_lease.credential.require_secret("key") == "secret-v2"
    assert _state(pool) is CredentialState.AVAILABLE
    pool.report_sync(new_lease, Outcome.success())
    assert_lease_accounting(store)


def _state(pool: CredentialPool) -> CredentialState:
    record = pool.get_record("c1")
    assert record is not None
    return record.state


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

    # Pool 2 rotates secret; the rotation alone does not recover the credential
    source_two.set_credentials([_cred("c1", "secret-v2")])
    with pytest.raises(NoCredentialsAvailableError):
        await pool_two.acquire()
    rec = store.get_record("c1")
    assert rec is not None
    assert rec.state is CredentialState.REVOKED

    # An operator authorizes the new secret through either pool sharing the store
    pool_two.authorize_secret("c1")
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
                with contextlib.suppress(NoCredentialsAvailableError):
                    # racing reports revoked both credentials until the next authorization
                    lease = pool.acquire_sync()
                if i % 5 == 0:
                    source.set_credentials(
                        [
                            _cred("a", f"sec-{worker_id}-{i}"),
                            _cred("b", f"sec-{worker_id}-{i}"),
                        ]
                    )
                    # Reviving a revoked credential is an explicit act, so each rotation is
                    # authorized. A racing rotation is fine: the pool re-reads the source.
                    pool.authorize_secret("a")
                    pool.authorize_secret("b")
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
