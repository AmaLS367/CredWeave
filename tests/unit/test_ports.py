"""Unit tests verifying application port protocols and candidate models."""

from collections.abc import Sequence
from datetime import datetime, timezone

from credweave.application.ports.credential_source import CredentialSource
from credweave.application.ports.state_store import CredentialRecord, StateStore
from credweave.application.ports.strategy import (
    CredentialCandidate,
    SelectionContext,
    SelectionStrategy,
)
from credweave.domain.enums import CredentialState
from credweave.domain.models import Credential
from credweave.domain.outcomes import Outcome


class MockSource:
    """Mock implementation of CredentialSource."""

    def __init__(self, creds: Sequence[Credential]) -> None:
        self._creds = creds

    def get_credentials(self) -> Sequence[Credential]:
        return self._creds

    async def get_credentials_async(self) -> Sequence[Credential]:
        return self._creds

    @property
    def supports_hot_reload(self) -> bool:
        return False


class MockStrategy:
    """Mock implementation of SelectionStrategy."""

    @property
    def name(self) -> str:
        return "mock-first-available"

    def select(
        self,
        candidates: Sequence[CredentialCandidate],
        context: SelectionContext | None = None,
    ) -> CredentialCandidate | None:
        for c in candidates:
            if c.is_available:
                return c
        return None


class MockStore:
    """Mock implementation of StateStore."""

    def __init__(self) -> None:
        self._records: dict[str, CredentialRecord] = {}

    def get_record(self, credential_id: str) -> CredentialRecord | None:
        return self._records.get(credential_id)

    async def get_record_async(self, credential_id: str) -> CredentialRecord | None:
        return self.get_record(credential_id)

    def list_records(self) -> Sequence[CredentialRecord]:
        return list(self._records.values())

    async def list_records_async(self) -> Sequence[CredentialRecord]:
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

    def record_acquire(
        self,
        credential_id: str,
        timestamp: datetime,
    ) -> None:
        rec = self._records.get(credential_id)
        if rec is not None:
            self._records[credential_id] = CredentialRecord(
                credential_id=credential_id,
                state=rec.state,
                in_flight_leases=rec.in_flight_leases + 1,
                total_leases=rec.total_leases + 1,
                last_used_at=timestamp,
            )

    async def record_acquire_async(
        self,
        credential_id: str,
        timestamp: datetime,
    ) -> None:
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

    def release_lease(
        self,
        credential_id: str,
    ) -> None:
        rec = self._records.get(credential_id)
        if rec is not None:
            self._records[credential_id] = CredentialRecord(
                credential_id=credential_id,
                state=rec.state,
                in_flight_leases=max(0, rec.in_flight_leases - 1),
            )

    async def release_lease_async(
        self,
        credential_id: str,
    ) -> None:
        self.release_lease(credential_id)


def test_credential_source_protocol(sample_credential: Credential) -> None:
    """Verify CredentialSource protocol adherence."""
    source = MockSource([sample_credential])
    assert isinstance(source, CredentialSource)
    assert source.get_credentials() == [sample_credential]


def test_selection_strategy_protocol(sample_credential: Credential) -> None:
    """Verify SelectionStrategy protocol adherence and Candidate evaluation."""
    strategy = MockStrategy()
    assert isinstance(strategy, SelectionStrategy)

    cand1 = CredentialCandidate(
        credential=sample_credential,
        state=CredentialState.AVAILABLE,
    )
    assert cand1.is_available is True
    assert cand1.credential_id == sample_credential.id

    context = SelectionContext(required_tags=frozenset({"fast"}))
    selected = strategy.select([cand1], context)
    assert selected == cand1


def test_state_store_protocol() -> None:
    """Verify StateStore protocol adherence and record updates."""
    store = MockStore()
    assert isinstance(store, StateStore)

    now = datetime.now(timezone.utc)
    store.update_state("cred-1", CredentialState.COOLDOWN, cooldown_until=now)

    record = store.get_record("cred-1")
    assert record is not None
    assert record.credential_id == "cred-1"
    assert record.state == CredentialState.COOLDOWN
    assert record.cooldown_until == now
