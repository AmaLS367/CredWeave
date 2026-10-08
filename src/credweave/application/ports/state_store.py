"""State store port for persisting credential state, cooldowns, and metrics."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from credweave.domain._security import SecretSafeMapping, mask_metadata
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
        object.__setattr__(
            self,
            "metadata",
            SecretSafeMapping(self.metadata),
        )

    def __repr__(self) -> str:
        """Return secret-safe string representation."""
        masked_meta = mask_metadata(self.metadata)
        return (
            f"{self.__class__.__name__}("
            f"credential_id={self.credential_id!r}, "
            f"state={self.state!r}, "
            f"in_flight_leases={self.in_flight_leases!r}, "
            f"consecutive_failures={self.consecutive_failures!r}, "
            f"cooldown_until={self.cooldown_until!r}, "
            f"last_used_at={self.last_used_at!r}, "
            f"total_leases={self.total_leases!r}, "
            f"metadata={masked_meta!r}"
            f")"
        )

    def __str__(self) -> str:
        return self.__repr__()


@dataclass(frozen=True)
class LeaseRecord:
    """A lease that currently holds a concurrency slot in the state store."""

    lease_id: str
    credential_id: str
    acquired_at: datetime
    expires_at: datetime | None = None
    secret_fingerprint: str | None = None
    secret_generation: int | None = None
    """The rotation generation of the secret this lease was granted under, or ``None`` when the
    store had no secret observed for the credential. A lease whose generation is no longer the
    credential's current one predates a rotation: its outcome is never applied."""


class LeaseReservation(Enum):
    """Result of trying to reserve a lease slot against the state store.

    Both failure results are *races*, not faults: they are expected when several pools,
    threads or tasks share a store, they change nothing in the store (in particular no health
    state) and the caller should simply select another credential.
    """

    RESERVED = "reserved"
    """The credential was eligible and had a free slot: the lease is now registered."""

    AT_CAPACITY = "at_capacity"
    """The credential was eligible but already at its concurrency cap."""

    INELIGIBLE = "ineligible"
    """The credential's authoritative current state is not ``AVAILABLE`` (revoked, disabled,
    unhealthy, rate limited, quota exhausted or cooling down)."""

    STALE = "stale"
    """The secret the caller selected is not the credential's current secret: it was superseded
    by a later rotation, or it is an earlier secret that the store does not re-adopt. The caller
    holds an outdated snapshot and must not lease it; nothing changes in the store."""

    def __bool__(self) -> bool:
        """Truthy only when the lease was reserved, so a failure is never mistaken for success."""
        return self is LeaseReservation.RESERVED


class LeaseSettlement(Enum):
    """Result of settling (reporting) a lease against the state store."""

    SETTLED = "settled"
    """The lease was active: its slot was released. The outcome was applied, unless the lease
    was granted under a secret that has since been rotated, in which case it was discarded."""

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
    lease reclamation hold across every pool, thread and task sharing the store. The registry
    is the *only* way to change ``CredentialRecord.in_flight_leases``: the counter is derived
    from, and always equals, the number of registered leases of that credential. Each method
    of the registry (``reserve_lease``, ``settle_lease``, ``reclaim_expired_leases``) must be
    atomic: it either fully applies or, if it raises, leaves the store unchanged.

    Secrets are versioned per credential by a store-owned *generation*. The store adopts a
    newly seen secret fingerprint as the next generation and never re-adopts an earlier one, so
    a stale snapshot cannot roll the credential back. Leases record the generation they were
    granted under, and ``reserve_lease`` only grants a lease for the current generation.

    Fingerprints are keyed per process (see :mod:`credweave.domain._security`). A store shared
    by several processes must replace them with fingerprints keyed by a secret all of them share;
    otherwise every process would see every synchronised secret as a new rotation.
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

    def reserve_lease(
        self,
        credential_id: str,
        lease_id: str,
        timestamp: datetime,
        *,
        max_concurrency: int | None = None,
        expires_at: datetime | None = None,
        secret_fingerprint: str | None = None,
    ) -> LeaseReservation:
        """Atomically check secret currency, eligibility and capacity, then register the lease.

        In one atomic step, against the authoritative current record:

        0. if ``secret_fingerprint`` is given and is not the credential's current secret, ``STALE``
           is returned: the caller selected a snapshot that has since been superseded;
        1. a timed cooldown that has elapsed at ``timestamp`` is recovered to ``AVAILABLE``;
        2. the state must then be ``AVAILABLE``, otherwise ``INELIGIBLE`` is returned;
        3. the in-flight count must be below ``max_concurrency`` (``None`` means unlimited),
           otherwise ``AT_CAPACITY`` is returned;
        4. only then are in-flight and total leases incremented, ``last_used_at`` stamped and
           ``lease_id`` registered (reclaimable once ``expires_at`` has passed), recording the
           secret generation the lease was granted under.

        A credential never being seen by the store counts as a fresh ``AVAILABLE`` one. A
        non-``RESERVED`` result changes nothing: no counter, no registry entry and no health
        state, so a caller that merely lost a race can safely pick another credential.

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
        secret_fingerprint: str | None = None,
    ) -> LeaseReservation:
        """Atomically check currency, eligibility and capacity, then register asynchronously."""
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
        slot at most once and applies no outcome (``EXPIRED``). A lease granted under an earlier
        secret generation than the credential's current one also releases its slot but applies
        no outcome (``SETTLED``), since its outcome describes a superseded secret. Leases that
        are not active (``UNKNOWN``) or that belong to another credential (``MISMATCH``) change
        nothing. If this method raises, the lease stays registered so the caller can retry.
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
