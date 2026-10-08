"""Domain exceptions for CredWeave.

Security Guarantee:
    Exception messages produced by CredWeave must never contain raw secret values.
    Errors should reference identifiers, parameter names, or sanitized descriptors.
"""

from collections.abc import Mapping
from typing import Any

from credweave.domain._security import SecretSafeMapping, mask_metadata, mask_secret_text


class CredWeaveError(Exception):
    """Base class for all CredWeave domain and runtime exceptions."""

    def __init__(self, *args: object) -> None:
        sanitized = tuple(mask_secret_text(str(a)) if isinstance(a, str) else a for a in args)
        super().__init__(*sanitized)

    def __str__(self) -> str:
        return mask_secret_text(super().__str__()) or ""

    def __repr__(self) -> str:
        arg_strs = [mask_secret_text(repr(a)) or "" for a in self.args]
        return f"{self.__class__.__name__}({', '.join(arg_strs)})"


class ConfigurationError(CredWeaveError):
    """Raised when an invalid pool, credential, or strategy configuration is detected."""


class CredentialError(CredWeaveError):
    """Base class for credential-specific errors."""


class CredentialNotFoundError(CredentialError):
    """Raised when a requested credential ID does not exist in the pool or store."""

    def __init__(self, credential_id: str) -> None:
        self.credential_id = credential_id
        super().__init__(f"Credential not found: {credential_id!r}")


class CredentialAlreadyExistsError(CredentialError):
    """Raised when registering a credential with an ID that already exists."""

    def __init__(self, credential_id: str) -> None:
        self.credential_id = credential_id
        super().__init__(f"Credential already exists: {credential_id!r}")


class SecretAccessError(CredentialError):
    """Raised when an expected secret key is missing from a credential."""

    def __init__(self, credential_id: str, key: str) -> None:
        self.credential_id = credential_id
        sanitized_key = mask_secret_text(key) or key
        self.key = sanitized_key
        super().__init__(f"Secret key {sanitized_key!r} not found on credential {credential_id!r}.")


class PoolError(CredWeaveError):
    """Base class for credential pool runtime errors."""


class NoCredentialsAvailableError(PoolError):
    """Raised when no credentials match the selection criteria or all are in cooldown/disabled."""

    def __init__(self, message: str = "No eligible credentials available in pool.") -> None:
        super().__init__(message)


class LeaseError(PoolError):
    """Base class for lease lifecycle errors."""


class LeaseExpiredError(LeaseError):
    """Raised when reporting or releasing a lease that has already expired."""

    def __init__(self, lease_id: str) -> None:
        self.lease_id = lease_id
        super().__init__(f"Lease {lease_id!r} has expired.")


class InvalidLeaseError(LeaseError):
    """Raised when a lease is unknown, already released, or mismatched."""

    def __init__(self, lease_id: str, reason: str | None = None) -> None:
        self.lease_id = lease_id
        sanitized_reason = mask_secret_text(reason) if reason else None
        self.reason = sanitized_reason
        msg = f"Invalid lease {lease_id!r}"
        if sanitized_reason:
            msg += f": {sanitized_reason}"
        super().__init__(msg)


class InvalidOutcomeError(CredWeaveError):
    """Raised when an invalid outcome is reported."""

    details: Any

    def __init__(self, message: str, details: Any = None) -> None:
        if isinstance(details, Mapping):
            self.details = SecretSafeMapping(details)
        elif isinstance(details, str):
            self.details = mask_secret_text(details)
        else:
            self.details = details
        super().__init__(message)

    def __repr__(self) -> str:
        if self.details is None:
            return f"{self.__class__.__name__}({str(self)!r})"
        masked = (
            mask_metadata(self.details)
            if isinstance(self.details, Mapping)
            else mask_secret_text(str(self.details))
        )
        return f"{self.__class__.__name__}({str(self)!r}, details={masked!r})"


class StateStoreError(CredWeaveError):
    """Raised when an operation on a state store fails."""


class CredentialSourceError(CredWeaveError):
    """Raised when reading or loading credentials from an external source fails."""
