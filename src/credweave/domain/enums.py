"""Domain enumerations defining credential states and report outcome types."""

from enum import Enum


class CredentialState(str, Enum):
    """Lifecycle and health state of a credential managed in a pool.

    Attributes:
        AVAILABLE: Credential is valid, healthy, and ready to be leased.
        COOLDOWN: Temporarily paused following a transient error or cool-off period.
        DISABLED: Manually or administratively deactivated.
        RATE_LIMITED: Temporarily paused due to upstream rate limits (429/Too Many Requests).
        QUOTA_EXHAUSTED: Paused due to quota limits reached for the billing or reset window.
        UNHEALTHY: Marked unusable due to persistent failures or invalid credentials.
    """

    AVAILABLE = "available"
    COOLDOWN = "cooldown"
    DISABLED = "disabled"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
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
        TEMPORARY_FAILURE: Transient failure (e.g. network timeout, 5xx server error).
        PERMANENT_FAILURE: Non-retryable failure indicating severe credential or upstream fault.
    """

    SUCCESS = "success"
    RATE_LIMITED = "rate_limited"
    AUTH_FAILED = "auth_failed"
    QUOTA_EXHAUSTED = "quota_exhausted"
    TEMPORARY_FAILURE = "temporary_failure"
    PERMANENT_FAILURE = "permanent_failure"

    def __str__(self) -> str:
        return self.value
