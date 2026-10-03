"""Unit tests for domain enumerations."""

from credweave.domain.enums import CredentialState, OutcomeType


def test_credential_state_values() -> None:
    """Test all expected CredentialState members and string representations."""
    assert CredentialState.AVAILABLE.value == "available"
    assert CredentialState.COOLDOWN.value == "cooldown"
    assert CredentialState.DISABLED.value == "disabled"
    assert CredentialState.RATE_LIMITED.value == "rate_limited"
    assert CredentialState.QUOTA_EXHAUSTED.value == "quota_exhausted"
    assert CredentialState.REVOKED.value == "revoked"
    assert CredentialState.UNHEALTHY.value == "unhealthy"

    assert str(CredentialState.AVAILABLE) == "available"
    assert str(CredentialState.REVOKED) == "revoked"
    assert CredentialState("available") == CredentialState.AVAILABLE
    assert CredentialState("revoked") == CredentialState.REVOKED


def test_outcome_type_values() -> None:
    """Test all expected OutcomeType members and string representations."""
    assert OutcomeType.SUCCESS.value == "success"
    assert OutcomeType.RATE_LIMITED.value == "rate_limited"
    assert OutcomeType.AUTH_FAILED.value == "auth_failed"
    assert OutcomeType.QUOTA_EXHAUSTED.value == "quota_exhausted"
    assert OutcomeType.TRANSIENT_ERROR.value == "transient_error"
    assert OutcomeType.PERMANENT_FAILURE.value == "permanent_failure"
    assert OutcomeType.CONSECUTIVE_FAILURES_EXCEEDED.value == "consecutive_failures_exceeded"

    # Backward compatibility alias for TRANSIENT_ERROR
    assert OutcomeType.TEMPORARY_FAILURE is OutcomeType.TRANSIENT_ERROR  # type: ignore[comparison-overlap]
    assert OutcomeType.TEMPORARY_FAILURE.value == "transient_error"
    assert OutcomeType("temporary_failure") == OutcomeType.TRANSIENT_ERROR

    assert str(OutcomeType.SUCCESS) == "success"
    assert str(OutcomeType.TRANSIENT_ERROR) == "transient_error"
    assert OutcomeType("success") == OutcomeType.SUCCESS
    assert OutcomeType("transient_error") == OutcomeType.TRANSIENT_ERROR
    assert OutcomeType("consecutive_failures_exceeded") == OutcomeType.CONSECUTIVE_FAILURES_EXCEEDED
