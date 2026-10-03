"""Unit tests for CredWeave domain exceptions."""

from credweave.domain.errors import (
    ConfigurationError,
    CredentialAlreadyExistsError,
    CredentialError,
    CredentialNotFoundError,
    CredentialSourceError,
    CredWeaveError,
    InvalidLeaseError,
    InvalidOutcomeError,
    LeaseError,
    LeaseExpiredError,
    NoCredentialsAvailableError,
    PoolError,
    SecretAccessError,
    StateStoreError,
)


def test_exception_inheritance_hierarchy() -> None:
    """Ensure all custom exceptions inherit from CredWeaveError."""
    assert issubclass(ConfigurationError, CredWeaveError)
    assert issubclass(CredentialError, CredWeaveError)
    assert issubclass(CredentialNotFoundError, CredentialError)
    assert issubclass(CredentialAlreadyExistsError, CredentialError)
    assert issubclass(SecretAccessError, CredentialError)
    assert issubclass(PoolError, CredWeaveError)
    assert issubclass(NoCredentialsAvailableError, PoolError)
    assert issubclass(LeaseError, PoolError)
    assert issubclass(LeaseExpiredError, LeaseError)
    assert issubclass(InvalidLeaseError, LeaseError)
    assert issubclass(InvalidOutcomeError, CredWeaveError)
    assert issubclass(StateStoreError, CredWeaveError)
    assert issubclass(CredentialSourceError, CredWeaveError)


def test_exception_messages() -> None:
    """Test exception formatting and attributes."""
    err_not_found = CredentialNotFoundError("cred-42")
    assert err_not_found.credential_id == "cred-42"
    assert "cred-42" in str(err_not_found)

    err_exists = CredentialAlreadyExistsError("cred-42")
    assert err_exists.credential_id == "cred-42"
    assert "cred-42" in str(err_exists)

    err_secret = SecretAccessError("cred-42", "token_key")
    assert err_secret.credential_id == "cred-42"
    assert err_secret.key == "token_key"
    assert "token_key" in str(err_secret)

    err_lease = InvalidLeaseError("lease-99", reason="expired")
    assert err_lease.lease_id == "lease-99"
    assert "expired" in str(err_lease)

    err_lease_no_reason = InvalidLeaseError("lease-100")
    assert "lease-100" in str(err_lease_no_reason)

    err_expired = LeaseExpiredError("lease-99")
    assert err_expired.lease_id == "lease-99"
    assert "lease-99" in str(err_expired)

    err_no_creds = NoCredentialsAvailableError("Custom pool exhaustion")
    assert "Custom pool exhaustion" in str(err_no_creds)

    err_outcome = InvalidOutcomeError("Malformed payload", details={"code": 400})
    assert err_outcome.details == {"code": 400}
    assert "Malformed payload" in str(err_outcome)
