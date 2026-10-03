"""In-memory state store adapter."""

import threading
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from credweave.application.ports.clock import Clock
from credweave.application.ports.state_store import CredentialRecord, StateStore
from credweave.domain.enums import CredentialState, OutcomeType
from credweave.domain.outcomes import Outcome
from credweave.infrastructure.clocks.system import SystemClock


class MemoryStateStore(StateStore):
    """Thread-safe in-memory state store for credential lifecycle and metrics.

    Args:
        clock: Optional clock abstraction for time-based cooldown calculations.
        default_cooldown: Default cooldown in seconds when not specified by outcome.
        max_consecutive_failures: Failure count threshold before escalating to UNHEALTHY.
    """

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        default_cooldown: float = 60.0,
        max_consecutive_failures: int = 3,
    ) -> None:
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._default_cooldown = max(0.0, float(default_cooldown))
        self._max_consecutive_failures = max(1, int(max_consecutive_failures))
        self._records: dict[str, CredentialRecord] = {}
        self._lock = threading.RLock()

    def _check_cooldown_recovery(
        self,
        record: CredentialRecord,
        now: datetime,
    ) -> CredentialRecord:
        """Automatically restore credentials to AVAILABLE once their cooldown has elapsed."""
        if (
            record.state
            in (
                CredentialState.COOLDOWN,
                CredentialState.RATE_LIMITED,
                CredentialState.QUOTA_EXHAUSTED,
            )
            and record.cooldown_until is not None
            and now >= record.cooldown_until
        ):
            recovered = replace(
                record,
                state=CredentialState.AVAILABLE,
                cooldown_until=None,
            )
            self._records[record.credential_id] = recovered
            return recovered
        return record

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

    def record_outcome(
        self,
        credential_id: str,
        outcome: Outcome,
        timestamp: datetime,
    ) -> None:
        """Record an operation outcome and apply lifecycle state transitions."""
        with self._lock:
            existing = self._records.get(credential_id)
            if existing is None:
                existing = CredentialRecord(
                    credential_id=credential_id,
                    state=CredentialState.AVAILABLE,
                )

            new_in_flight = max(0, existing.in_flight_leases - 1)

            if outcome.type == OutcomeType.SUCCESS:
                new_state = CredentialState.AVAILABLE
                new_consecutive_failures = 0
                new_cooldown_until = None

            elif outcome.type == OutcomeType.AUTH_FAILED:
                new_state = CredentialState.REVOKED
                new_consecutive_failures = existing.consecutive_failures + 1
                new_cooldown_until = None

            elif outcome.type == OutcomeType.RATE_LIMITED:
                new_consecutive_failures = existing.consecutive_failures + 1
                if new_consecutive_failures >= self._max_consecutive_failures:
                    new_state = CredentialState.UNHEALTHY
                    new_cooldown_until = None
                else:
                    new_state = CredentialState.RATE_LIMITED
                    retry_secs = (
                        outcome.retry_after
                        if outcome.retry_after is not None
                        else self._default_cooldown
                    )
                    new_cooldown_until = timestamp + timedelta(seconds=retry_secs)

            elif outcome.type == OutcomeType.QUOTA_EXHAUSTED:
                new_consecutive_failures = existing.consecutive_failures + 1
                new_state = CredentialState.QUOTA_EXHAUSTED
                if outcome.retry_after is not None:
                    new_cooldown_until = timestamp + timedelta(seconds=outcome.retry_after)
                else:
                    new_cooldown_until = None

            elif outcome.type in (
                OutcomeType.CONSECUTIVE_FAILURES_EXCEEDED,
                OutcomeType.PERMANENT_FAILURE,
            ):
                new_consecutive_failures = existing.consecutive_failures + 1
                new_state = CredentialState.UNHEALTHY
                new_cooldown_until = None

            elif outcome.type == OutcomeType.TRANSIENT_ERROR:
                new_consecutive_failures = existing.consecutive_failures + 1
                if new_consecutive_failures >= self._max_consecutive_failures:
                    new_state = CredentialState.UNHEALTHY
                    new_cooldown_until = None
                else:
                    new_state = CredentialState.COOLDOWN
                    retry_secs = outcome.retry_after if outcome.retry_after is not None else 5.0
                    new_cooldown_until = timestamp + timedelta(seconds=retry_secs)

            else:
                new_consecutive_failures = existing.consecutive_failures + 1
                new_state = existing.state
                new_cooldown_until = existing.cooldown_until

            self._records[credential_id] = replace(
                existing,
                state=new_state,
                in_flight_leases=new_in_flight,
                consecutive_failures=new_consecutive_failures,
                cooldown_until=new_cooldown_until,
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
