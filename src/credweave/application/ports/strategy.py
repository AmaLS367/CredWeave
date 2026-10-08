"""Selection strategy port for credential scheduling and load balancing algorithms."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from credweave.domain._security import SecretSafeMapping, mask_metadata
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
        raw_secrets = getattr(self.credential, "_raw_secrets", {})
        secret_values = raw_secrets.values() if hasattr(raw_secrets, "values") else ()
        if not isinstance(self.metadata, SecretSafeMapping):
            object.__setattr__(
                self,
                "metadata",
                SecretSafeMapping(self.metadata, raw_secrets=secret_values),
            )

    @property
    def credential_id(self) -> str:
        """The underlying credential identifier."""
        return self.credential.id

    @property
    def is_available(self) -> bool:
        """Return True if the candidate is in the AVAILABLE state."""
        return self.state == CredentialState.AVAILABLE

    def __repr__(self) -> str:
        """Return secret-safe string representation."""
        raw_secrets = getattr(self.credential, "_raw_secrets", {})
        secret_values = raw_secrets.values() if hasattr(raw_secrets, "values") else ()
        masked_meta = mask_metadata(self.metadata, raw_secrets=secret_values)
        return (
            f"{self.__class__.__name__}("
            f"credential={self.credential!r}, "
            f"state={self.state!r}, "
            f"in_flight_leases={self.in_flight_leases!r}, "
            f"consecutive_failures={self.consecutive_failures!r}, "
            f"cooldown_until={self.cooldown_until!r}, "
            f"total_leases={self.total_leases!r}, "
            f"last_used_at={self.last_used_at!r}, "
            f"metadata={masked_meta!r}"
            f")"
        )

    def __str__(self) -> str:
        return self.__repr__()


@dataclass(frozen=True)
class SelectionContext:
    """Contextual requirements passed to strategies during credential acquisition."""

    required_tags: frozenset[str] = field(default_factory=frozenset)
    preferred_metadata: Mapping[str, Any] = field(default_factory=dict)
    attempt_number: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.preferred_metadata, SecretSafeMapping):
            object.__setattr__(
                self,
                "preferred_metadata",
                SecretSafeMapping(self.preferred_metadata),
            )

    def __repr__(self) -> str:
        """Return secret-safe string representation."""
        masked_meta = mask_metadata(self.preferred_metadata)
        return (
            f"{self.__class__.__name__}("
            f"required_tags={self.required_tags!r}, "
            f"preferred_metadata={masked_meta!r}, "
            f"attempt_number={self.attempt_number!r}"
            f")"
        )

    def __str__(self) -> str:
        return self.__repr__()


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
