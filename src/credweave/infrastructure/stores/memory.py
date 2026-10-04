"""In-memory state store adapter."""

import threading
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any

from credweave.application.ports.clock import Clock
from credweave.application.ports.state_store import CredentialRecord, StateStore
from credweave.application.services.lifecycle import LifecycleEngine
from credweave.domain.enums import CredentialState
from credweave.domain.outcomes import Outcome
from credweave.infrastructure.clocks.system import SystemClock


class MemoryStateStore(StateStore):
    """Thread-safe in-memory state store for credential lifecycle and metrics.

    The store only persists records; every lifecycle decision (cooldowns, backoff, health
    escalation, precedence) is delegated to a :class:`LifecycleEngine`.

    Args:
        clock: Optional clock abstraction for time-based cooldown calculations.
        default_cooldown: Fixed cooldown in seconds used when no ``lifecycle`` is given.
        max_consecutive_failures: Failure threshold for UNHEALTHY when no ``lifecycle`` is given.
        lifecycle: Optional lifecycle engine; overrides ``default_cooldown`` and
            ``max_consecutive_failures`` when provided.
    """

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        default_cooldown: float = 60.0,
        max_consecutive_failures: int = 3,
        lifecycle: LifecycleEngine | None = None,
    ) -> None:
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._lifecycle: LifecycleEngine = (
            lifecycle
            if lifecycle is not None
            else LifecycleEngine(
                default_cooldown=default_cooldown,
                max_consecutive_failures=max_consecutive_failures,
            )
        )
        self._records: dict[str, CredentialRecord] = {}
        self._lock = threading.RLock()

    def _check_cooldown_recovery(
        self,
        record: CredentialRecord,
        now: datetime,
    ) -> CredentialRecord:
        """Persist and return the record restored to AVAILABLE if its cooldown has elapsed."""
        recovered = self._lifecycle.recover(record, now)
        if recovered is not record:
            self._records[record.credential_id] = recovered
        return recovered

    def initialize_record(
        self,
        credential_id: str,
        state: CredentialState = CredentialState.AVAILABLE,
        metadata: Mapping[str, Any] | None = None,
    ) -> CredentialRecord:
        """Initialize and store a default record for a credential if not already present."""
        with self._lock:
            existing = self._records.get(credential_id)
            if existing is not None:
                return existing
            record = CredentialRecord(
                credential_id=credential_id,
                state=state,
                metadata=metadata or {},
            )
            self._records[credential_id] = record
            return record

    def get_record(self, credential_id: str) -> CredentialRecord | None:
        """Retrieve the state record for a credential synchronously."""
        with self._lock:
            record = self._records.get(credential_id)
            if record is None:
                return None
            return self._check_cooldown_recovery(record, self._clock.now())

    async def get_record_async(self, credential_id: str) -> CredentialRecord | None:
        """Retrieve the state record for a credential asynchronously."""
        return self.get_record(credential_id)

    def list_records(self) -> Sequence[CredentialRecord]:
        """List all credential state records synchronously."""
        with self._lock:
            now = self._clock.now()
            result: list[CredentialRecord] = []
            for record in self._records.values():
                recovered = self._check_cooldown_recovery(record, now)
                result.append(recovered)
            return tuple(result)

    async def list_records_async(self) -> Sequence[CredentialRecord]:
        """List all credential state records asynchronously."""
        return self.list_records()

    def update_state(
        self,
        credential_id: str,
        state: CredentialState,
        *,
        cooldown_until: datetime | None = None,
    ) -> None:
        """Update the state and cooldown timer for a credential synchronously."""
        with self._lock:
            existing = self._records.get(credential_id)
            if existing is None:
                self._records[credential_id] = CredentialRecord(
                    credential_id=credential_id,
                    state=state,
                    cooldown_until=cooldown_until,
                )
            else:
                consecutive = (
                    0 if state == CredentialState.AVAILABLE else existing.consecutive_failures
                )
                self._records[credential_id] = replace(
                    existing,
                    state=state,
                    cooldown_until=cooldown_until,
                    consecutive_failures=consecutive,
                )

    async def update_state_async(
        self,
        credential_id: str,
        state: CredentialState,
        *,
        cooldown_until: datetime | None = None,
    ) -> None:
        """Update the state and cooldown timer for a credential asynchronously."""
        self.update_state(credential_id, state, cooldown_until=cooldown_until)

    def record_acquire(
        self,
        credential_id: str,
        timestamp: datetime,
    ) -> None:
        """Record a lease acquisition, incrementing in-flight and total leases synchronously."""
        with self._lock:
            existing = self._records.get(credential_id)
            if existing is None:
                self._records[credential_id] = CredentialRecord(
                    credential_id=credential_id,
                    state=CredentialState.AVAILABLE,
                    in_flight_leases=1,
                    total_leases=1,
                    last_used_at=timestamp,
                )
            else:
                self._records[credential_id] = replace(
                    existing,
                    in_flight_leases=existing.in_flight_leases + 1,
                    total_leases=existing.total_leases + 1,
                    last_used_at=timestamp,
                )

    async def record_acquire_async(
        self,
        credential_id: str,
        timestamp: datetime,
    ) -> None:
        """Record a lease acquisition asynchronously."""
        self.record_acquire(credential_id, timestamp)

    def release_lease(self, credential_id: str) -> None:
        """Release an in-flight lease without applying an outcome synchronously."""
        with self._lock:
            existing = self._records.get(credential_id)
            if existing is not None:
                self._records[credential_id] = self._lifecycle.release(existing, self._clock.now())

    async def release_lease_async(self, credential_id: str) -> None:
        """Release an in-flight lease without applying an outcome asynchronously."""
        self.release_lease(credential_id)

    def record_outcome(
        self,
        credential_id: str,
        outcome: Outcome,
        timestamp: datetime,
    ) -> None:
        """Record an operation outcome; the lifecycle engine decides the transition."""
        with self._lock:
            existing = self._records.get(credential_id)
            if existing is None:
                existing = CredentialRecord(
                    credential_id=credential_id,
                    state=CredentialState.AVAILABLE,
                )
            self._records[credential_id] = self._lifecycle.apply_outcome(
                existing, outcome, timestamp
            )

    async def record_outcome_async(
        self,
        credential_id: str,
        outcome: Outcome,
        timestamp: datetime,
    ) -> None:
        """Record an operation outcome asynchronously."""
        self.record_outcome(credential_id, outcome, timestamp)

    def reset(self, credential_id: str) -> None:
        """Reset a credential's state to AVAILABLE, clearing failures and cooldown."""
        with self._lock:
            existing = self._records.get(credential_id)
            if existing is not None:
                self._records[credential_id] = replace(
                    existing,
                    state=CredentialState.AVAILABLE,
                    consecutive_failures=0,
                    cooldown_until=None,
                )

    async def reset_async(self, credential_id: str) -> None:
        """Reset a credential's state asynchronously."""
        self.reset(credential_id)
