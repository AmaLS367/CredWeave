"""State store port for persisting credential state, cooldowns, and metrics."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
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


@runtime_checkable
class StateStore(Protocol):
    """Protocol for state persistence across pool operations and distributed instances.

    Implementations can store credential states, cooldown timers, active leases,
    and execution metrics in-memory, SQLite, or Redis.
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
