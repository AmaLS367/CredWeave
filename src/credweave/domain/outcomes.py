"""Domain model representing the reported outcome of a credential lease execution."""

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from credweave.domain._security import SecretSafeMapping, mask_metadata, mask_secret_text
from credweave.domain.enums import OutcomeType
from credweave.domain.errors import InvalidOutcomeError


def _validate_retry_after(retry_after: object) -> float:
    """Validate that retry_after is a finite non-negative real number deadline."""
    if isinstance(retry_after, bool):
        raise InvalidOutcomeError("retry_after cannot be a boolean.")
    if not isinstance(retry_after, (int, float)):
        raise InvalidOutcomeError(
            f"retry_after must be a real number, got {type(retry_after).__name__}."
        )
    if not math.isfinite(retry_after):
        raise InvalidOutcomeError("retry_after must be a finite number.")
    if retry_after < 0:
        raise InvalidOutcomeError("retry_after cannot be negative.")
    hint = float(retry_after)
    try:
        td = timedelta(seconds=hint)
        datetime.min + td
    except (OverflowError, ValueError):
        raise InvalidOutcomeError(
            f"retry_after {retry_after!r} cannot be represented safely as a "
            "datetime/timedelta deadline."
        ) from None
    return hint


@dataclass(frozen=True)
class Outcome:
    """Represents the execution outcome of an operation performed using a leased credential.

    Outcomes are reported back to the credential pool to drive health tracking,
    cooldown calculation, and selection strategies.

    Attributes:
        type: The category of outcome (success, rate limited, auth failure, etc.).
        retry_after: Optional recommended duration in seconds before reusing the credential.
            When provided, must be a finite non-negative real number representable safely as a
            datetime/timedelta deadline.
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
                except ValueError:
                    sanitized_type = mask_secret_text(raw_type)
                    raise InvalidOutcomeError(f"Invalid outcome type: {sanitized_type!r}") from None
            else:
                raise InvalidOutcomeError(
                    f"Outcome type must be an OutcomeType, got {type(raw_type).__name__}."
                ) from None

        if self.retry_after is not None:
            object.__setattr__(self, "retry_after", _validate_retry_after(self.retry_after))

        object.__setattr__(
            self,
            "metadata",
            SecretSafeMapping(self.metadata),
        )

    def redact(self, secrets: Iterable[str] = ()) -> "Outcome":
        """Return a copy of this Outcome with all occurrences of secrets redacted."""
        secrets_tuple = tuple(s for s in secrets if isinstance(s, str) and s)
        new_reason = (
            mask_secret_text(self.reason, raw_secrets=secrets_tuple)
            if self.reason is not None
            else None
        )
        new_meta = mask_metadata(self.metadata, raw_secrets=secrets_tuple)
        return Outcome(
            type=self.type,
            retry_after=self.retry_after,
            reason=new_reason,
            metadata=new_meta,
        )

    def __repr__(self) -> str:
        """Return secret-safe string representation with masked secret values."""
        masked_reason = mask_secret_text(self.reason) if self.reason is not None else None
        masked_meta = mask_metadata(self.metadata)
        return (
            f"{self.__class__.__name__}("
            f"type={self.type!r}, "
            f"retry_after={self.retry_after!r}, "
            f"reason={masked_reason!r}, "
            f"metadata={masked_meta!r}"
            f")"
        )

    def __str__(self) -> str:
        """Return secret-safe string representation with masked secret values."""
        return self.__repr__()

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
