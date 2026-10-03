"""Unit tests for the Outcome domain model."""

from dataclasses import FrozenInstanceError

import pytest

from credweave.domain.enums import OutcomeType
from credweave.domain.errors import InvalidOutcomeError
from credweave.domain.outcomes import Outcome


def test_outcome_direct_instantiation() -> None:
    """Test instantiating Outcome directly with enum or string."""
    outcome1 = Outcome(type=OutcomeType.SUCCESS)
    assert outcome1.type == OutcomeType.SUCCESS
    assert outcome1.is_success is True
    assert outcome1.is_failure is False

    outcome2 = Outcome(type="rate_limited", retry_after=30.0)  # type: ignore[arg-type]
    assert outcome2.type == OutcomeType.RATE_LIMITED
    assert outcome2.retry_after == 30.0
    assert outcome2.is_rate_limited is True
    assert outcome2.is_failure is True

    outcome3 = Outcome(type="transient_error")  # type: ignore[arg-type]
    assert outcome3.type == OutcomeType.TRANSIENT_ERROR
    assert outcome3.is_transient_error is True

    outcome4 = Outcome(type="temporary_failure")  # type: ignore[arg-type]
    assert outcome4.type == OutcomeType.TRANSIENT_ERROR
    assert outcome4.is_temporary_failure is True

    outcome5 = Outcome(type="consecutive_failures_exceeded")  # type: ignore[arg-type]
    assert outcome5.type == OutcomeType.CONSECUTIVE_FAILURES_EXCEEDED
    assert outcome5.is_consecutive_failures_exceeded is True


def test_outcome_factories() -> None:
    """Test outcome factory helper methods."""
    succ = Outcome.success(metadata={"status_code": 200})
    assert succ.type == OutcomeType.SUCCESS
    assert succ.is_success is True
    assert succ.metadata["status_code"] == 200

    rl = Outcome.rate_limited(retry_after=45.5, reason="HTTP 429 Too Many Requests")
    assert rl.type == OutcomeType.RATE_LIMITED
    assert rl.retry_after == 45.5
    assert rl.reason == "HTTP 429 Too Many Requests"
    assert rl.is_rate_limited is True

    auth = Outcome.auth_failed(reason="Invalid API Key")
    assert auth.type == OutcomeType.AUTH_FAILED
    assert auth.reason == "Invalid API Key"
    assert auth.is_failure is True

    quota = Outcome.quota_exhausted(retry_after=3600.0, reason="Monthly budget exceeded")
    assert quota.type == OutcomeType.QUOTA_EXHAUSTED
    assert quota.retry_after == 3600.0

    temp_fail = Outcome.temporary_failure(retry_after=5.0, reason="Gateway Timeout")
    assert temp_fail.type == OutcomeType.TEMPORARY_FAILURE
    assert temp_fail.retry_after == 5.0
    assert temp_fail.reason == "Gateway Timeout"
    assert temp_fail.is_transient_error is True
    assert temp_fail.is_temporary_failure is True
    assert temp_fail.is_failure is True

    trans_err = Outcome.transient_error(retry_after=10.0, reason="Network timeout")
    assert trans_err.type == OutcomeType.TRANSIENT_ERROR
    assert trans_err.retry_after == 10.0
    assert trans_err.reason == "Network timeout"
    assert trans_err.is_transient_error is True
    assert trans_err.is_temporary_failure is True
    assert trans_err.is_failure is True

    consec_fail = Outcome.consecutive_failures_exceeded(
        retry_after=60.0, reason="Max consecutive failures exceeded"
    )
    assert consec_fail.type == OutcomeType.CONSECUTIVE_FAILURES_EXCEEDED
    assert consec_fail.retry_after == 60.0
    assert consec_fail.reason == "Max consecutive failures exceeded"
    assert consec_fail.is_consecutive_failures_exceeded is True
    assert consec_fail.is_failure is True

    perm_fail = Outcome.permanent_failure(reason="Account suspended")
    assert perm_fail.type == OutcomeType.PERMANENT_FAILURE
    assert perm_fail.reason == "Account suspended"


def test_outcome_validation() -> None:
    """Test validation errors for invalid types and negative retry intervals."""
    with pytest.raises(InvalidOutcomeError):
        Outcome(type="invalid_type_name")  # type: ignore[arg-type]

    with pytest.raises(InvalidOutcomeError):
        Outcome(type=12345)  # type: ignore[arg-type]

    with pytest.raises(InvalidOutcomeError):
        Outcome(type=OutcomeType.RATE_LIMITED, retry_after=-1.0)


def test_outcome_immutability() -> None:
    """Test that outcome attributes and metadata are immutable."""
    initial_metadata = {"tag": "val"}
    outcome = Outcome.success(metadata=initial_metadata)

    # Mutation on passed dict shouldn't affect outcome
    initial_metadata["tag"] = "changed"
    assert outcome.metadata["tag"] == "val"

    # Dataclass is frozen
    with pytest.raises(FrozenInstanceError):
        outcome.reason = "attempted mutation"  # type: ignore[misc]

    # Metadata mapping is read-only
    with pytest.raises(TypeError):
        outcome.metadata["tag"] = "attempted mutation"  # type: ignore[index]
