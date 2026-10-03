"""Tests for edge cases and branch coverage across pool, store, and strategies."""

from datetime import datetime

import pytest

from credweave.application.ports.state_store import CredentialRecord, StateStore
from credweave.application.ports.strategy import CredentialCandidate, SelectionContext
from credweave.application.services.pool import CredentialPool
from credweave.domain.enums import CredentialState, OutcomeType
from credweave.domain.errors import (
    InvalidLeaseError,
    InvalidOutcomeError,
    LeaseExpiredError,
    NoCredentialsAvailableError,
)
from credweave.domain.models import Credential, Lease
from credweave.domain.outcomes import Outcome
from credweave.infrastructure.sources.static import StaticSource
from credweave.infrastructure.stores.memory import MemoryStateStore
from credweave.strategies.round_robin import RoundRobinStrategy
from tests.conftest import TestClock


class DummyStore(StateStore):
    """Minimal StateStore without initialize_record, reset, or async acquire methods."""

    def __init__(self) -> None:
        self._records: dict[str, CredentialRecord] = {}

    def get_record(self, credential_id: str) -> CredentialRecord | None:
        return self._records.get(credential_id)

    async def get_record_async(self, credential_id: str) -> CredentialRecord | None:
        return self.get_record(credential_id)

    def list_records(self) -> tuple[CredentialRecord, ...]:
        return tuple(self._records.values())

    async def list_records_async(self) -> tuple[CredentialRecord, ...]:
        return self.list_records()

    def update_state(
        self,
        credential_id: str,
        state: CredentialState,
        *,
        cooldown_until: datetime | None = None,
    ) -> None:
        self._records[credential_id] = CredentialRecord(
            credential_id=credential_id,
            state=state,
            cooldown_until=cooldown_until,
        )

    async def update_state_async(
        self,
        credential_id: str,
        state: CredentialState,
        *,
        cooldown_until: datetime | None = None,
    ) -> None:
        self.update_state(credential_id, state, cooldown_until=cooldown_until)

    def record_acquire(self, credential_id: str, timestamp: datetime) -> None:
        rec = self._records.get(credential_id)
        if rec:
            self._records[credential_id] = CredentialRecord(
                credential_id=credential_id,
                state=rec.state,
                in_flight_leases=rec.in_flight_leases + 1,
            )

    async def record_acquire_async(self, credential_id: str, timestamp: datetime) -> None:
        self.record_acquire(credential_id, timestamp)

    def record_outcome(
        self,
        credential_id: str,
        outcome: Outcome,
        timestamp: datetime,
    ) -> None:
        pass

    async def record_outcome_async(
        self,
        credential_id: str,
        outcome: Outcome,
        timestamp: datetime,
    ) -> None:
        pass


def test_pool_with_dummy_store_branches(sample_credential: Credential) -> None:
    """Test pool initialization and acquire with a minimal StateStore."""
    store = DummyStore()
    pool = CredentialPool(credentials=[sample_credential], store=store)

    # acquire_sync and acquire trigger branches where store lacks initialize_record
    lease = pool.acquire_sync()
    assert lease.credential_id == sample_credential.id

    pool.reset_credential(sample_credential.id)
    rec = store.get_record(sample_credential.id)
    assert rec is not None
    assert rec.state == CredentialState.AVAILABLE


@pytest.mark.asyncio
async def test_pool_async_edge_cases(sample_credential: Credential, test_clock: TestClock) -> None:
    """Test async report errors, lease timeout, and reset fallbacks."""
    store = DummyStore()
    pool = CredentialPool(
        credentials=[sample_credential],
        store=store,
        clock=test_clock,
        lease_timeout=5.0,
    )

    # Empty source in acquire
    empty_pool = CredentialPool(source=StaticSource([]))
    with pytest.raises(NoCredentialsAvailableError):
        await empty_pool.acquire()

    with pytest.raises(NoCredentialsAvailableError):
        empty_pool.acquire_sync()

    # Async acquire with dummy store
    lease = await pool.acquire()

    # Async report invalid types
    with pytest.raises(InvalidLeaseError):
        await pool.report("invalid", Outcome.success())  # type: ignore[arg-type]

    with pytest.raises(InvalidOutcomeError):
        await pool.report(lease, "invalid")  # type: ignore[arg-type]

    # Async report credential mismatch
    other_cred = Credential(id="other-cred", secrets={"k": "v"})
    tampered_lease = Lease(
        credential=other_cred,
        lease_id=lease.lease_id,
        acquired_at=lease.acquired_at,
    )
    with pytest.raises(InvalidLeaseError):
        await pool.report(tampered_lease, Outcome.success())

    # Async report lease expired
    test_clock.advance(10.0)
    with pytest.raises(LeaseExpiredError):
        await pool.report(lease, Outcome.success())

    # Async reset fallback
    await pool.reset_credential_async(sample_credential.id)


def test_memory_state_store_additional_branches(test_clock: TestClock) -> None:
    """Test memory store initialize duplicate, update_state branches, unknown outcome, etc."""
    store = MemoryStateStore(clock=test_clock)

    # 1. initialize_record duplicate
    rec1 = store.initialize_record("c1")
    rec2 = store.initialize_record("c1")
    assert rec1 is rec2

    # 2. update_state on non-existent record
    store.update_state("non-existent", CredentialState.DISABLED)
    rec_non = store.get_record("non-existent")
    assert rec_non is not None
    assert rec_non.state == CredentialState.DISABLED

    # 3. update_state on existing record with cooldown
    now = test_clock.now()
    store.update_state("c1", CredentialState.COOLDOWN, cooldown_until=now)
    assert store.get_record("c1") is not None

    # 4. record_acquire on non-existent record
    store.record_acquire("brand-new", now)
    rec_new = store.get_record("brand-new")
    assert rec_new is not None
    assert rec_new.in_flight_leases == 1

    # 5. outcome CONSECUTIVE_FAILURES_EXCEEDED and PERMANENT_FAILURE
    store.record_outcome("c1", Outcome.permanent_failure(), now)
    assert store.get_record("c1").state == CredentialState.UNHEALTHY  # type: ignore[union-attr]

    # 6. Custom or unknown outcome type
    custom_outcome = Outcome(type=OutcomeType.SUCCESS)
    object.__setattr__(custom_outcome, "type", "custom_unknown")
    store.record_outcome("c1", custom_outcome, now)

    # 7. reset non-existent
    store.reset("does-not-exist")


@pytest.mark.asyncio
async def test_memory_state_store_reset_async(test_clock: TestClock) -> None:
    """Test reset_async on MemoryStateStore."""
    store = MemoryStateStore(clock=test_clock)
    store.initialize_record("c1", state=CredentialState.UNHEALTHY)
    await store.reset_async("c1")
    rec = store.get_record("c1")
    assert rec is not None
    assert rec.state == CredentialState.AVAILABLE


def test_round_robin_tag_types_and_fallback(sample_credentials: list[Credential]) -> None:
    """Test round robin tag filtering with string tag, unsupported tag type, and removed last_id."""
    # Credential with single string tag
    cred_str_tag = Credential(id="tag-str", secrets={"k": "v"}, metadata={"tags": "fast"})
    # Credential with non-iterable tag
    cred_bad_tag = Credential(id="tag-bad", secrets={"k": "v"}, metadata={"tags": 12345})

    strat = RoundRobinStrategy()
    cands = [
        CredentialCandidate(credential=cred_str_tag, state=CredentialState.AVAILABLE),
        CredentialCandidate(credential=cred_bad_tag, state=CredentialState.AVAILABLE),
    ]

    # Filter with required tag 'fast' -> matches tag-str
    ctx = SelectionContext(required_tags=frozenset({"fast"}))
    sel = strat.select(cands, ctx)
    assert sel is not None
    assert sel.credential_id == "tag-str"

    # Last selected ID fallback when last selected is no longer in candidate list
    all_cands = [
        CredentialCandidate(credential=c, state=CredentialState.AVAILABLE)
        for c in sample_credentials
    ]
    strat._last_selected_id = "removed-credential-id"
    fallback_sel = strat.select(all_cands)
    assert fallback_sel is not None
    assert fallback_sel.credential_id == "cred-alpha"
