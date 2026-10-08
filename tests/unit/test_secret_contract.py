"""Pool-level tests for the StateStore secret-generation contract.

Covers the construction-time contract check, the source read made at construction, errors that
must propagate instead of being swallowed, the explicit ``authorize_secret`` operation and the
sync/async parity of both. Every source here is scripted, so each interleaving is deterministic.
"""

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest

from credweave import (
    ConfigurationError,
    Credential,
    CredentialNotFoundError,
    CredentialPool,
    CredentialSourceError,
    CredentialState,
    MemoryStateStore,
    NoCredentialsAvailableError,
    Outcome,
)
from credweave.application.ports.state_store import LeaseReservation
from tests.conftest import TestClock
from tests.lease_helpers import assert_lease_accounting


def _cred(secret: str, cid: str = "c1") -> Credential:
    return Credential(id=cid, secrets={"key": secret})


class ScriptedSource:
    """Serves one snapshot per read; the last snapshot repeats once the script runs out."""

    def __init__(self, *snapshots: Sequence[Credential]) -> None:
        self._snapshots = list(snapshots)
        self.reads = 0

    def get_credentials(self) -> Sequence[Credential]:
        self.reads += 1
        index = min(self.reads, len(self._snapshots)) - 1
        return self._snapshots[index]

    async def get_credentials_async(self) -> Sequence[Credential]:
        return self.get_credentials()

    @property
    def supports_hot_reload(self) -> bool:
        return True


class AlternatingSource:
    """Flips between two secrets on every read, so it never settles."""

    def __init__(self) -> None:
        self._reads = 0

    def get_credentials(self) -> Sequence[Credential]:
        self._reads += 1
        return [_cred("odd" if self._reads % 2 else "even")]

    async def get_credentials_async(self) -> Sequence[Credential]:
        return self.get_credentials()

    @property
    def supports_hot_reload(self) -> bool:
        return True


class SwitchSource:
    """A source whose single secret the test changes explicitly."""

    def __init__(self, secret: str) -> None:
        self._secret = secret

    def set(self, secret: str) -> None:
        self._secret = secret

    def get_credentials(self) -> Sequence[Credential]:
        return [_cred(self._secret)]

    async def get_credentials_async(self) -> Sequence[Credential]:
        return self.get_credentials()

    @property
    def supports_hot_reload(self) -> bool:
        return True


class FailingSource:
    """A source that cannot be read."""

    def get_credentials(self) -> Sequence[Credential]:
        raise CredentialSourceError("source unavailable")

    async def get_credentials_async(self) -> Sequence[Credential]:
        return self.get_credentials()

    @property
    def supports_hot_reload(self) -> bool:
        return True


class LegacyStore(MemoryStateStore):
    """A store written against the pre-generation API: reserve_lease takes no fingerprint."""

    def reserve_lease(  # type: ignore[override]
        self,
        credential_id: str,
        lease_id: str,
        timestamp: Any,
        *,
        max_concurrency: int | None = None,
        expires_at: Any = None,
    ) -> LeaseReservation:
        return super().reserve_lease(
            credential_id, lease_id, timestamp, max_concurrency=max_concurrency
        )


class StoreWithoutSync(MemoryStateStore):
    """A store that cannot synchronise secrets."""

    sync_credential = None  # type: ignore[assignment]


class ForwardingStore(MemoryStateStore):
    """A wrapper that forwards every keyword, including the fingerprint, to the real store."""

    def reserve_lease(self, *args: Any, **kwargs: Any) -> LeaseReservation:
        return super().reserve_lease(*args, **kwargs)


class StoreWithoutAuthorize(MemoryStateStore):
    """A store that predates explicit authorization."""

    authorize_secret = None  # type: ignore[assignment]


def test_store_without_sync_credential_is_refused_at_construction() -> None:
    with pytest.raises(ConfigurationError, match="sync_credential"):
        CredentialPool(
            source=ScriptedSource([_cred("A")]),
            store=StoreWithoutSync(clock=TestClock()),
            clock=TestClock(),
        )


def test_store_whose_reserve_lease_ignores_fingerprints_is_refused_at_construction() -> None:
    with pytest.raises(ConfigurationError, match="secret_fingerprint"):
        CredentialPool(
            source=ScriptedSource([_cred("A")]),
            store=LegacyStore(clock=TestClock()),
            clock=TestClock(),
        )


def test_wrapper_forwarding_keywords_satisfies_the_contract() -> None:
    clock = TestClock()
    pool = CredentialPool(
        source=ScriptedSource([_cred("A")]), store=ForwardingStore(clock=clock), clock=clock
    )
    assert pool.acquire_sync().credential.require_secret("key") == "A"


def test_store_without_authorize_secret_fails_closed_when_authorizing() -> None:
    clock = TestClock()
    pool = CredentialPool(
        source=ScriptedSource([_cred("A")]),
        store=StoreWithoutAuthorize(clock=clock),
        clock=clock,
    )
    with pytest.raises(ConfigurationError, match="authorize_secret"):
        pool.authorize_secret("c1")


def test_construction_reads_the_source_once_and_registers_its_baseline() -> None:
    clock = TestClock()
    source = ScriptedSource([_cred("A")])
    store = MemoryStateStore(clock=clock)
    CredentialPool(source=source, store=store, clock=clock)

    assert source.reads == 1
    assert store._secret_generations["c1"].current == _cred("A").secret_fingerprint


def test_construction_propagates_source_errors_instead_of_swallowing_them() -> None:
    clock = TestClock()
    with pytest.raises(CredentialSourceError, match="unavailable"):
        CredentialPool(source=FailingSource(), store=MemoryStateStore(clock=clock), clock=clock)


def test_stale_pool_built_before_a_rotation_cannot_roll_back_to_its_baseline() -> None:
    """A source that reverts to the secret a pool was built with is refused, not re-adopted."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    stale_source = ScriptedSource([_cred("A")], [_cred("A")], [_cred("A")])
    stale_pool = CredentialPool(source=stale_source, store=store, clock=clock)

    fresh_pool = CredentialPool(source=ScriptedSource([_cred("B")]), store=store, clock=clock)
    fresh_pool.report_sync(fresh_pool.acquire_sync(), Outcome.success())
    assert store.get_record("c1").state is CredentialState.AVAILABLE

    # The stale source now reports A again, which the store adopted at construction.
    with pytest.raises(NoCredentialsAvailableError):
        stale_pool.acquire_sync()
    assert store._secret_generations["c1"].current == _cred("B").secret_fingerprint
    assert_lease_accounting(store)


def test_authorize_secret_for_unknown_credential_raises() -> None:
    clock = TestClock()
    pool = CredentialPool(
        source=ScriptedSource([_cred("A")]), store=MemoryStateStore(clock=clock), clock=clock
    )
    with pytest.raises(CredentialNotFoundError):
        pool.authorize_secret("missing")


def test_authorize_secret_activates_the_secret_the_source_presents_now() -> None:
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    source = ScriptedSource([_cred("A")], [_cred("A")], [_cred("B")])
    pool = CredentialPool(source=source, store=store, clock=clock)
    store.update_state("c1", CredentialState.REVOKED)

    record = pool.authorize_secret("c1")
    assert record.state is CredentialState.AVAILABLE
    assert pool.acquire_sync().credential.require_secret("key") == "B"


def test_authorize_secret_follows_a_source_that_changes_while_it_is_authorized() -> None:
    """The store never stays on an older secret after the source has moved on."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    # Construction reads A; authorization reads A, then the source moves to B before verifying.
    source = ScriptedSource([_cred("A")], [_cred("A")], [_cred("B")], [_cred("B")])
    pool = CredentialPool(source=source, store=store, clock=clock)

    pool.authorize_secret("c1")

    assert store._secret_generations["c1"].current == _cred("B").secret_fingerprint
    assert pool.acquire_sync().credential.require_secret("key") == "B"


def test_authorize_secret_gives_up_when_the_source_never_settles() -> None:
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    pool = CredentialPool(source=AlternatingSource(), store=store, clock=clock)

    with pytest.raises(CredentialSourceError, match="kept changing"):
        pool.authorize_secret("c1")


def test_authorize_secret_rolls_back_to_an_earlier_secret_through_the_pool() -> None:
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    source = SwitchSource("A")
    pool = CredentialPool(source=source, store=store, clock=clock)
    old_a = pool.acquire_sync()  # a lease under the first generation (A)

    source.set("B")
    b_lease = pool.acquire_sync()  # rotation to B
    pool.report_sync(b_lease, Outcome.auth_failed(reason="B revoked"))
    assert store.get_record("c1").state is CredentialState.REVOKED

    source.set("A")  # the operator restores the earlier secret in the source
    record = pool.authorize_secret("c1")

    assert record.state is CredentialState.AVAILABLE
    assert pool.acquire_sync().credential.require_secret("key") == "A"
    pool.report_sync(old_a, Outcome.permanent_failure(reason="pre-rollback lease"))
    assert store.get_record("c1").state is CredentialState.AVAILABLE
    assert_lease_accounting(store)


def test_async_authorize_matches_sync_for_the_same_script() -> None:
    """Identical scripts leave identical store state whether authorized sync or async."""

    def run(asynchronous: bool) -> tuple[Any, ...]:
        clock = TestClock()
        store = MemoryStateStore(clock=clock)
        source = ScriptedSource([_cred("A")], [_cred("A")], [_cred("B")], [_cred("B")])
        pool = CredentialPool(source=source, store=store, clock=clock)
        store.update_state("c1", CredentialState.UNHEALTHY)
        if asynchronous:
            record = asyncio.run(pool.authorize_secret_async("c1"))
        else:
            record = pool.authorize_secret("c1")
        generations = store._secret_generations["c1"]
        return (
            record.state,
            record.consecutive_failures,
            generations.generation,
            generations.current,
        )

    assert run(asynchronous=True) == run(asynchronous=False)


@pytest.mark.asyncio
async def test_async_authorize_refuses_unknown_credentials_like_sync() -> None:
    clock = TestClock()
    pool = CredentialPool(
        source=ScriptedSource([_cred("A")]), store=MemoryStateStore(clock=clock), clock=clock
    )
    with pytest.raises(CredentialNotFoundError):
        await pool.authorize_secret_async("missing")


class StoreWithoutAsyncAuthorize(MemoryStateStore):
    """A store that offers only the synchronous authorization method."""

    authorize_secret_async = None  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_async_authorize_falls_back_to_the_sync_store_method() -> None:
    clock = TestClock()
    store = StoreWithoutAsyncAuthorize(clock=clock)
    pool = CredentialPool(source=SwitchSource("A"), store=store, clock=clock)
    store.update_state("c1", CredentialState.REVOKED)

    record = await pool.authorize_secret_async("c1")

    assert record.state is CredentialState.AVAILABLE
    assert (await pool.acquire()).credential.require_secret("key") == "A"


@pytest.mark.asyncio
async def test_async_authorize_gives_up_when_the_source_never_settles() -> None:
    clock = TestClock()
    pool = CredentialPool(
        source=AlternatingSource(), store=MemoryStateStore(clock=clock), clock=clock
    )
    with pytest.raises(CredentialSourceError, match="kept changing"):
        await pool.authorize_secret_async("c1")
