"""Domain enumerations defining credential states and report outcome types."""

from enum import Enum
from typing import Any


class CredentialState(str, Enum):
    """Lifecycle and health state of a credential managed in a pool.

    Attributes:
        AVAILABLE: Credential is valid, healthy, and ready to be leased.
        COOLDOWN: Temporarily paused following a transient error or cool-off period.
        DISABLED: Manually or administratively deactivated.
        RATE_LIMITED: Temporarily paused due to upstream rate limits (429/Too Many Requests).
        QUOTA_EXHAUSTED: Paused due to quota limits reached for the billing or reset window.
        REVOKED: Permanently deactivated or revoked (e.g. following authentication failure).
        UNHEALTHY: Marked unusable due to persistent failures or invalid credentials.
    """

    AVAILABLE = "available"
    COOLDOWN = "cooldown"
    DISABLED = "disabled"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    REVOKED = "revoked"
    UNHEALTHY = "unhealthy"

    def __str__(self) -> str:
        return self.value


class OutcomeType(str, Enum):
    """Execution outcome reported back to the credential pool.

    Attributes:
        SUCCESS: The workload succeeded using this credential.
        RATE_LIMITED: The upstream provider reported rate limits or throttling.
        AUTH_FAILED: Authentication or authorization failed (e.g. invalid key, expired token).
        QUOTA_EXHAUSTED: Account quota or budget limit has been reached.
        TRANSIENT_ERROR: Transient failure (e.g. network timeout, 5xx server error).
        TEMPORARY_FAILURE: Backward-compatible alias for TRANSIENT_ERROR.
        PERMANENT_FAILURE: Non-retryable failure indicating severe credential or upstream fault.
        CONSECUTIVE_FAILURES_EXCEEDED: Credential repeatedly failed across consecutive executions.
    """

    SUCCESS = "success"
    RATE_LIMITED = "rate_limited"
    AUTH_FAILED = "auth_failed"
    QUOTA_EXHAUSTED = "quota_exhausted"
    TRANSIENT_ERROR = "transient_error"
    PERMANENT_FAILURE = "permanent_failure"
    CONSECUTIVE_FAILURES_EXCEEDED = "consecutive_failures_exceeded"

    # Backward compatibility alias for TRANSIENT_ERROR
    TEMPORARY_FAILURE = TRANSIENT_ERROR

    @classmethod
    def _missing_(cls, value: object) -> Any:
        if value == "temporary_failure":
            return cls.TRANSIENT_ERROR
        return None

    def __str__(self) -> str:
        return self.value
