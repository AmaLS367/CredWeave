"""In-memory state store adapter."""

import heapq
import itertools
import threading
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any

from credweave.application.ports.clock import Clock
from credweave.application.ports.state_store import (
    CredentialRecord,
    LeaseRecord,
    LeaseSettlement,
    StateStore,
)
from credweave.application.services.lifecycle import LifecycleEngine
from credweave.domain.concurrency import validate_max_concurrency
from credweave.domain.enums import CredentialState
from credweave.domain.errors import StateStoreError
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

    Lease slots are tracked in a registry guarded by the same lock as the credential records,
    so slot reservation, settlement and expiry reclamation are atomic across threads, asyncio
    tasks and every pool sharing the store. Leases carry an optional deadline; expired leases
    are found through a min-heap, so reclamation costs O(log n) per reclaimed lease and
    O(1) when nothing is due.
    """

    _TOMBSTONE_LIMIT = 4096
    """How many reclaimed lease ids are remembered to tell a late report from an unknown one."""

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
        self._leases: dict[str, LeaseRecord] = {}
        self._expiry_heap: list[tuple[datetime, int, str]] = []
        self._heap_counter = itertools.count()
        self._reclaimed: dict[str, None] = {}

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

    def reserve_lease(
        self,
        credential_id: str,
        lease_id: str,
        timestamp: datetime,
        *,
        max_concurrency: int | None = None,
        expires_at: datetime | None = None,
    ) -> bool:
        """Atomically claim a concurrency slot and register the lease synchronously."""
        limit = validate_max_concurrency(max_concurrency, "max_concurrency")
        with self._lock:
            if lease_id in self._leases:
                raise StateStoreError(f"Lease {lease_id!r} is already registered.")
            existing = self._records.get(credential_id)
            in_flight = existing.in_flight_leases if existing is not None else 0
            if limit is not None and in_flight >= limit:
                return False
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
                    in_flight_leases=in_flight + 1,
                    total_leases=existing.total_leases + 1,
                    last_used_at=timestamp,
                )
            self._leases[lease_id] = LeaseRecord(
                lease_id=lease_id,
                credential_id=credential_id,
                acquired_at=timestamp,
                expires_at=expires_at,
            )
            if expires_at is not None:
                heapq.heappush(self._expiry_heap, (expires_at, next(self._heap_counter), lease_id))
                self._compact_expiry_heap()
            return True

    async def reserve_lease_async(
        self,
        credential_id: str,
        lease_id: str,
        timestamp: datetime,
        *,
        max_concurrency: int | None = None,
        expires_at: datetime | None = None,
    ) -> bool:
        """Atomically claim a concurrency slot and register the lease asynchronously."""
        return self.reserve_lease(
            credential_id,
            lease_id,
            timestamp,
            max_concurrency=max_concurrency,
            expires_at=expires_at,
        )

    def settle_lease(
        self,
        lease_id: str,
        credential_id: str,
        outcome: Outcome,
        timestamp: datetime,
    ) -> LeaseSettlement:
        """Atomically end a registered lease and apply its outcome synchronously."""
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                return (
                    LeaseSettlement.EXPIRED
                    if lease_id in self._reclaimed
                    else LeaseSettlement.UNKNOWN
                )
            if lease.credential_id != credential_id:
                return LeaseSettlement.MISMATCH

            record = self._records.get(credential_id)
            if record is None:
                record = CredentialRecord(
                    credential_id=credential_id,
                    state=CredentialState.AVAILABLE,
                )
            expired = lease.expires_at is not None and timestamp > lease.expires_at
            # Compute first: if the lifecycle engine raises, nothing has been mutated and the
            # lease stays registered, so the caller can retry without leaking or double-releasing.
            updated = (
                self._lifecycle.reclaim(record)
                if expired
                else self._lifecycle.apply_outcome(record, outcome, timestamp)
            )
            self._records[credential_id] = updated
            self._unregister(lease, reclaimed=expired)
            return LeaseSettlement.EXPIRED if expired else LeaseSettlement.SETTLED

    async def settle_lease_async(
        self,
        lease_id: str,
        credential_id: str,
        outcome: Outcome,
        timestamp: datetime,
    ) -> LeaseSettlement:
        """Atomically end a registered lease and apply its outcome asynchronously."""
        return self.settle_lease(lease_id, credential_id, outcome, timestamp)

    def reclaim_expired_leases(self, now: datetime) -> Sequence[LeaseRecord]:
        """Release the slot of every lease whose deadline is before ``now`` synchronously."""
        with self._lock:
            reclaimed: list[LeaseRecord] = []
            heap = self._expiry_heap
            while heap and heap[0][0] < now:
                expires_at, _, lease_id = heapq.heappop(heap)
                lease = self._leases.get(lease_id)
                if lease is None or lease.expires_at != expires_at:
                    continue  # stale index entry: the lease was already settled
                record = self._records.get(lease.credential_id)
                if record is not None:
                    self._records[lease.credential_id] = self._lifecycle.reclaim(record)
                self._unregister(lease, reclaimed=True)
                reclaimed.append(lease)
            return tuple(reclaimed)

    async def reclaim_expired_leases_async(self, now: datetime) -> Sequence[LeaseRecord]:
        """Release the slot of every lease whose deadline is before ``now`` asynchronously."""
        return self.reclaim_expired_leases(now)

    def list_active_leases(self) -> Sequence[LeaseRecord]:
        """List every lease currently holding a concurrency slot synchronously."""
        with self._lock:
            return tuple(self._leases.values())

    async def list_active_leases_async(self) -> Sequence[LeaseRecord]:
        """List every lease currently holding a concurrency slot asynchronously."""
        return self.list_active_leases()

    def _unregister(self, lease: LeaseRecord, *, reclaimed: bool) -> None:
        """Drop a lease from the registry, remembering it when it expired. Lock must be held."""
        del self._leases[lease.lease_id]
        if reclaimed:
            self._reclaimed[lease.lease_id] = None
            while len(self._reclaimed) > self._TOMBSTONE_LIMIT:
                del self._reclaimed[next(iter(self._reclaimed))]
        self._compact_expiry_heap()

    def _compact_expiry_heap(self) -> None:
        """Rebuild the expiry index when settled leases have left too many stale entries."""
        if len(self._expiry_heap) <= 2 * len(self._leases) + 128:
            return
        self._expiry_heap = [
            (lease.expires_at, next(self._heap_counter), lease.lease_id)
            for lease in self._leases.values()
            if lease.expires_at is not None
        ]
        heapq.heapify(self._expiry_heap)

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
