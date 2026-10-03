"""Domain exceptions for CredWeave.

Security Guarantee:
    Exception messages produced by CredWeave must never contain raw secret values.
    Errors should reference identifiers, parameter names, or sanitized descriptors.
"""

from typing import Any


class CredWeaveError(Exception):
    """Base class for all CredWeave domain and runtime exceptions."""


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
        self.key = key
        super().__init__(f"Secret key {key!r} not found on credential {credential_id!r}.")


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
        msg = f"Invalid lease {lease_id!r}"
        if reason:
            msg += f": {reason}"
        super().__init__(msg)


class InvalidOutcomeError(CredWeaveError):
    """Raised when an invalid outcome is reported."""

    def __init__(self, message: str, details: Any = None) -> None:
        self.details = details
        super().__init__(message)


class StateStoreError(CredWeaveError):
    """Raised when an operation on a state store fails."""


class CredentialSourceError(CredWeaveError):
    """Raised when reading or loading credentials from an external source fails."""
