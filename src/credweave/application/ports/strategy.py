"""Selection strategy port for credential scheduling and load balancing algorithms."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from credweave.domain.enums import CredentialState
from credweave.domain.models import Credential


@dataclass(frozen=True)
class CredentialCandidate:
    """Runtime snapshot of a candidate credential for evaluation by selection strategies."""

    credential: Credential
    state: CredentialState
    in_flight_leases: int = 0
    consecutive_failures: int = 0
    cooldown_until: datetime | None = None
    total_leases: int = 0
    last_used_at: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(
                self,
                "metadata",
                MappingProxyType(dict(self.metadata)),
            )

    @property
    def credential_id(self) -> str:
        """The underlying credential identifier."""
        return self.credential.id

    @property
    def is_available(self) -> bool:
        """Return True if the candidate is in the AVAILABLE state."""
        return self.state == CredentialState.AVAILABLE


@dataclass(frozen=True)
class SelectionContext:
    """Contextual requirements passed to strategies during credential acquisition."""

    required_tags: frozenset[str] = field(default_factory=frozenset)
    preferred_metadata: Mapping[str, Any] = field(default_factory=dict)
    attempt_number: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.preferred_metadata, MappingProxyType):
            object.__setattr__(
                self,
                "preferred_metadata",
                MappingProxyType(dict(self.preferred_metadata)),
            )


@runtime_checkable
class SelectionStrategy(Protocol):
    """Protocol for scheduling and selection algorithms.

    Built-in implementations: Round-Robin, Random, Weighted, Least-Used,
    Least-Recently-Used and Failover (priority order). Quota-Aware is planned.
    """

    @property
    def name(self) -> str:
        """Human-readable identifier of the strategy."""
        ...

    def select(
        self,
        candidates: Sequence[CredentialCandidate],
        context: SelectionContext | None = None,
    ) -> CredentialCandidate | None:
        """Select the best candidate according to strategy logic.

        Returns None if no candidate is currently eligible.
        """
        ...
