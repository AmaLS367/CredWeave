"""Unit tests for domain enumerations."""

from credweave.domain.enums import CredentialState, OutcomeType


def test_credential_state_values() -> None:
    """Test all expected CredentialState members and string representations."""
    assert CredentialState.AVAILABLE.value == "available"
    assert CredentialState.COOLDOWN.value == "cooldown"
    assert CredentialState.DISABLED.value == "disabled"
    assert CredentialState.RATE_LIMITED.value == "rate_limited"
    assert CredentialState.QUOTA_EXHAUSTED.value == "quota_exhausted"
    assert CredentialState.UNHEALTHY.value == "unhealthy"

    assert str(CredentialState.AVAILABLE) == "available"
    assert CredentialState("available") == CredentialState.AVAILABLE


def test_outcome_type_values() -> None:
    """Test all expected OutcomeType members and string representations."""
    assert OutcomeType.SUCCESS.value == "success"
    assert OutcomeType.RATE_LIMITED.value == "rate_limited"
    assert OutcomeType.AUTH_FAILED.value == "auth_failed"
    assert OutcomeType.QUOTA_EXHAUSTED.value == "quota_exhausted"
    assert OutcomeType.TEMPORARY_FAILURE.value == "temporary_failure"
    assert OutcomeType.PERMANENT_FAILURE.value == "permanent_failure"

    assert str(OutcomeType.SUCCESS) == "success"
    assert OutcomeType("success") == OutcomeType.SUCCESS
