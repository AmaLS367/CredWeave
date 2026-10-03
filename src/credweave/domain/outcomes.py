"""Domain model representing the reported outcome of a credential lease execution."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from credweave.domain.enums import OutcomeType
from credweave.domain.errors import InvalidOutcomeError


@dataclass(frozen=True)
class Outcome:
    """Represents the execution outcome of an operation performed using a leased credential.

    Outcomes are reported back to the credential pool to drive health tracking,
    cooldown calculation, and selection strategies.

    Attributes:
        type: The category of outcome (success, rate limited, auth failure, etc.).
        retry_after: Optional recommended duration in seconds before reusing the credential.
        reason: Optional human-readable diagnosis or failure reason (must NOT contain secrets).
        metadata: Optional non-secret diagnostic metadata (e.g. status code, attempt count).
    """

    type: OutcomeType
    retry_after: float | None = None
    reason: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        raw_type: object = self.type
        if not isinstance(raw_type, OutcomeType):
            if isinstance(raw_type, str):
                try:
                    object.__setattr__(self, "type", OutcomeType(raw_type))
                except ValueError as exc:
                    raise InvalidOutcomeError(f"Invalid outcome type: {raw_type!r}") from exc
            else:
                raise InvalidOutcomeError(
                    f"Outcome type must be an OutcomeType, got {type(raw_type).__name__}."
                )

        if self.retry_after is not None and self.retry_after < 0:
            raise InvalidOutcomeError("retry_after cannot be negative.")

        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(
                self,
                "metadata",
                MappingProxyType(dict(self.metadata)),
            )

    @property
    def is_success(self) -> bool:
        """Return True if this outcome represents a successful execution."""
        return self.type == OutcomeType.SUCCESS

    @property
    def is_failure(self) -> bool:
        """Return True if this outcome represents any failure state."""
        return self.type != OutcomeType.SUCCESS

    @property
    def is_rate_limited(self) -> bool:
        """Return True if this outcome was caused by rate limiting."""
        return self.type == OutcomeType.RATE_LIMITED

    @property
    def is_transient_error(self) -> bool:
        """Return True if this outcome was caused by a transient failure."""
        return self.type == OutcomeType.TRANSIENT_ERROR

    @property
    def is_temporary_failure(self) -> bool:
        """Return True if this outcome was caused by a transient failure (compatibility alias)."""
        return self.is_transient_error

    @property
    def is_consecutive_failures_exceeded(self) -> bool:
        """Return True if this outcome indicates consecutive failure threshold was exceeded."""
        return self.type == OutcomeType.CONSECUTIVE_FAILURES_EXCEEDED

    @classmethod
    def success(
        cls,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> "Outcome":
        """Factory for a successful operation outcome."""
        return cls(type=OutcomeType.SUCCESS, metadata=metadata or {})

    @classmethod
    def rate_limited(
        cls,
        *,
        retry_after: float | None = None,
        reason: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "Outcome":
        """Factory for a rate-limited outcome (e.g. HTTP 429)."""
        return cls(
            type=OutcomeType.RATE_LIMITED,
            retry_after=retry_after,
            reason=reason,
            metadata=metadata or {},
        )

    @classmethod
    def auth_failed(
        cls,
        *,
        reason: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "Outcome":
        """Factory for an authentication or authorization failure (e.g. revoked key)."""
        return cls(
            type=OutcomeType.AUTH_FAILED,
            reason=reason,
            metadata=metadata or {},
        )

    @classmethod
    def quota_exhausted(
        cls,
        *,
        retry_after: float | None = None,
        reason: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "Outcome":
        """Factory for a quota exhaustion outcome (e.g. monthly billing limit)."""
        return cls(
            type=OutcomeType.QUOTA_EXHAUSTED,
            retry_after=retry_after,
            reason=reason,
            metadata=metadata or {},
        )

    @classmethod
    def transient_error(
        cls,
        *,
        retry_after: float | None = None,
        reason: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "Outcome":
        """Factory for a transient failure (e.g. network timeout or 503)."""
        return cls(
            type=OutcomeType.TRANSIENT_ERROR,
            retry_after=retry_after,
            reason=reason,
            metadata=metadata or {},
        )

    @classmethod
    def temporary_failure(
        cls,
        *,
        retry_after: float | None = None,
        reason: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "Outcome":
        """Factory for a transient failure (compatibility alias for :meth:`transient_error`)."""
        return cls.transient_error(
            retry_after=retry_after,
            reason=reason,
            metadata=metadata,
        )

    @classmethod
    def consecutive_failures_exceeded(
        cls,
        *,
        retry_after: float | None = None,
        reason: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "Outcome":
        """Factory for an outcome indicating repeated consecutive failures have exceeded limits."""
        return cls(
            type=OutcomeType.CONSECUTIVE_FAILURES_EXCEEDED,
            retry_after=retry_after,
            reason=reason,
            metadata=metadata or {},
        )

    @classmethod
    def permanent_failure(
        cls,
        *,
        reason: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "Outcome":
        """Factory for an unrecoverable credential or upstream failure."""
        return cls(
            type=OutcomeType.PERMANENT_FAILURE,
            reason=reason,
            metadata=metadata or {},
        )
