"""CredWeave: Provider-agnostic credential pooling, scheduling, rotation and failover.

CredWeave allows applications to manage multiple credentials/accounts for arbitrary
external services through a unified, provider-agnostic abstraction.
"""

from importlib.metadata import PackageNotFoundError, version

from credweave.application.ports.clock import Clock
from credweave.application.ports.credential_source import CredentialSource
from credweave.application.ports.state_store import CredentialRecord, StateStore
from credweave.application.ports.strategy import (
    CredentialCandidate,
    SelectionContext,
    SelectionStrategy,
)
from credweave.application.services.pool import CredentialPool
from credweave.domain.enums import CredentialState, OutcomeType
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
from credweave.domain.models import Credential, Lease
from credweave.domain.outcomes import Outcome
from credweave.infrastructure.clocks.system import SystemClock

try:
    __version__ = version("credweave")
except PackageNotFoundError:
    # Package is not installed (e.g. running directly from source tree without editable install)
    __version__ = "0.1.0"

__all__ = [
    "Clock",
    "ConfigurationError",
    "CredWeaveError",
    "Credential",
    "CredentialAlreadyExistsError",
    "CredentialCandidate",
    "CredentialError",
    "CredentialNotFoundError",
    "CredentialPool",
    "CredentialRecord",
    "CredentialSource",
    "CredentialSourceError",
    "CredentialState",
    "InvalidLeaseError",
    "InvalidOutcomeError",
    "Lease",
    "LeaseError",
    "LeaseExpiredError",
    "NoCredentialsAvailableError",
    "Outcome",
    "OutcomeType",
    "PoolError",
    "SecretAccessError",
    "SelectionContext",
    "SelectionStrategy",
    "StateStore",
    "StateStoreError",
    "SystemClock",
    "__version__",
]
