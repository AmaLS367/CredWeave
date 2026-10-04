"""State store port for persisting credential state, cooldowns, and metrics."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from credweave.domain.enums import CredentialState
from credweave.domain.outcomes import Outcome


@dataclass(frozen=True)
class CredentialRecord:
    """Persisted state record tracking the lifecycle, health, and usage of a credential."""

    credential_id: str
    state: CredentialState
    in_flight_leases: int = 0
    consecutive_failures: int = 0
    cooldown_until: datetime | None = None
    last_used_at: datetime | None = None
    total_leases: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(
                self,
                "metadata",
                MappingProxyType(dict(self.metadata)),
            )


@dataclass(frozen=True)
class LeaseRecord:
    """A lease that currently holds a concurrency slot in the state store."""

    lease_id: str
    credential_id: str
    acquired_at: datetime
    expires_at: datetime | None = None


class LeaseSettlement(Enum):
    """Result of settling (reporting) a lease against the state store."""

    SETTLED = "settled"
    """The lease was active: the outcome was applied and its slot released."""

    EXPIRED = "expired"
    """The lease was past its deadline or already reclaimed: the slot was released (once)
    without applying any outcome."""

    UNKNOWN = "unknown"
    """The lease is not active: never reserved, already settled, or reclaimed long ago."""

    MISMATCH = "mismatch"
    """The lease exists but belongs to a different credential; nothing was changed."""


@runtime_checkable
class StateStore(Protocol):
    """Protocol for state persistence across pool operations and distributed instances.

    Implementations can store credential states, cooldown timers, active leases,
    and execution metrics in-memory, SQLite, or Redis.

    Lease slots are managed through a registry owned by the store, so concurrency limits and
    lease reclamation hold across every pool, thread and task sharing the store. Each method
    of the registry (``reserve_lease``, ``settle_lease``, ``reclaim_expired_leases``) must be
    atomic: it either fully applies or, if it raises, leaves the store unchanged.
    """

    def get_record(self, credential_id: str) -> CredentialRecord | None:
        """Retrieve the state record for a credential synchronously."""
        ...

    async def get_record_async(self, credential_id: str) -> CredentialRecord | None:
        """Retrieve the state record for a credential asynchronously."""
        ...

    def list_records(self) -> Sequence[CredentialRecord]:
        """List all credential state records synchronously."""
        ...

    async def list_records_async(self) -> Sequence[CredentialRecord]:
        """List all credential state records asynchronously."""
        ...

    def update_state(
        self,
        credential_id: str,
        state: CredentialState,
        *,
        cooldown_until: datetime | None = None,
    ) -> None:
        """Update the state and cooldown timer for a credential synchronously."""
        ...

    async def update_state_async(
        self,
        credential_id: str,
        state: CredentialState,
        *,
        cooldown_until: datetime | None = None,
    ) -> None:
        """Update the state and cooldown timer for a credential asynchronously."""
        ...

    def record_acquire(
        self,
        credential_id: str,
        timestamp: datetime,
    ) -> None:
        """Record a lease acquisition, incrementing in-flight and total leases synchronously."""
        ...

    async def record_acquire_async(
        self,
        credential_id: str,
        timestamp: datetime,
    ) -> None:
        """Record a lease acquisition asynchronously."""
        ...

    def record_outcome(
        self,
        credential_id: str,
        outcome: Outcome,
        timestamp: datetime,
    ) -> None:
        """Record an operation outcome for metrics and state transitions synchronously."""
        ...

    async def record_outcome_async(
        self,
        credential_id: str,
        outcome: Outcome,
        timestamp: datetime,
    ) -> None:
        """Record an operation outcome for metrics and state transitions asynchronously."""
        ...

    def release_lease(
        self,
        credential_id: str,
    ) -> None:
        """Release an in-flight lease without recording an outcome synchronously."""
        ...

    async def release_lease_async(
        self,
        credential_id: str,
    ) -> None:
        """Release an in-flight lease without recording an outcome asynchronously."""
        ...

    def reserve_lease(
        self,
        credential_id: str,
        lease_id: str,
        timestamp: datetime,
        *,
        max_concurrency: int | None = None,
        expires_at: datetime | None = None,
    ) -> bool:
        """Atomically claim a concurrency slot and register the lease synchronously.

        Checks the credential's in-flight count against ``max_concurrency`` (``None`` means
        unlimited) and, only if a slot is free, increments in-flight and total leases, stamps
        ``last_used_at`` and registers ``lease_id`` (reclaimable once ``expires_at`` has
        passed). The check and the claim are one atomic step. Returns ``False``, changing
        nothing, when the credential is at capacity.

        Raises:
            ConfigurationError: ``max_concurrency`` is not ``None`` or an integer >= 1.
            StateStoreError: ``lease_id`` is already registered.
        """
        ...

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
        ...

    def settle_lease(
        self,
        lease_id: str,
        credential_id: str,
        outcome: Outcome,
        timestamp: datetime,
    ) -> LeaseSettlement:
        """Atomically end a registered lease and apply its outcome synchronously.

        An active, unexpired lease is unregistered, its slot released and ``outcome`` applied
        in one step (``SETTLED``). A lease past its deadline, or already reclaimed, releases its
        slot at most once and applies no outcome (``EXPIRED``). Leases that are not active
        (``UNKNOWN``) or that belong to another credential (``MISMATCH``) change nothing. If
        this method raises, the lease stays registered so the caller can retry.
        """
        ...

    async def settle_lease_async(
        self,
        lease_id: str,
        credential_id: str,
        outcome: Outcome,
        timestamp: datetime,
    ) -> LeaseSettlement:
        """Atomically end a registered lease and apply its outcome asynchronously."""
        ...

    def reclaim_expired_leases(self, now: datetime) -> Sequence[LeaseRecord]:
        """Release the slot of every lease whose deadline is before ``now`` synchronously.

        Reclamation applies no outcome and leaves credential health untouched; it only
        decrements in-flight accounting, exactly once per lease, and makes the lease
        unsettleable. Idempotent. Returns the leases reclaimed by this call.
        """
        ...

    async def reclaim_expired_leases_async(self, now: datetime) -> Sequence[LeaseRecord]:
        """Release the slot of every lease whose deadline is before ``now`` asynchronously."""
        ...

    def list_active_leases(self) -> Sequence[LeaseRecord]:
        """List every lease currently holding a concurrency slot synchronously."""
        ...

    async def list_active_leases_async(self) -> Sequence[LeaseRecord]:
        """List every lease currently holding a concurrency slot asynchronously."""
        ...
