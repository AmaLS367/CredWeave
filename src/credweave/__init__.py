"""CredWeave: Provider-agnostic credential pooling, scheduling, rotation and failover.

CredWeave allows applications to manage multiple credentials/accounts for arbitrary
external services through a unified, provider-agnostic abstraction.
"""

from importlib.metadata import PackageNotFoundError, version

from credweave._internal.composition import configure_default_adapters
from credweave.application.ports.clock import Clock
from credweave.application.ports.credential_source import CredentialSource
from credweave.application.ports.state_store import CredentialRecord, StateStore
from credweave.application.ports.strategy import (
    CredentialCandidate,
    SelectionContext,
    SelectionStrategy,
)
from credweave.application.services.lifecycle import LifecycleEngine
from credweave.application.services.pool import CredentialPool
from credweave.domain.backoff import BackoffPolicy, RandomSource, RetryAfterMode
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
from credweave.infrastructure.sources.static import StaticSource
from credweave.infrastructure.stores.memory import MemoryStateStore
from credweave.strategies.failover import FailoverStrategy
from credweave.strategies.least_used import LeastUsedStrategy
from credweave.strategies.lru import LeastRecentlyUsedStrategy
from credweave.strategies.random_strategy import RandomStrategy
from credweave.strategies.round_robin import RoundRobinStrategy
from credweave.strategies.weighted import WeightedStrategy

# Wire default adapters into application services at composition root
configure_default_adapters()


try:
    __version__ = version("credweave")
except PackageNotFoundError:
    # Package is not installed (e.g. running directly from source tree without editable install)
    __version__ = "0.1.0"

__all__ = [
    "BackoffPolicy",
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
    "FailoverStrategy",
    "InvalidLeaseError",
    "InvalidOutcomeError",
    "Lease",
    "LeaseError",
    "LeaseExpiredError",
    "LeastRecentlyUsedStrategy",
    "LeastUsedStrategy",
    "LifecycleEngine",
    "MemoryStateStore",
    "NoCredentialsAvailableError",
    "Outcome",
    "OutcomeType",
    "PoolError",
    "RandomSource",
    "RandomStrategy",
    "RetryAfterMode",
    "RoundRobinStrategy",
    "SecretAccessError",
    "SelectionContext",
    "SelectionStrategy",
    "StateStore",
    "StateStoreError",
    "StaticSource",
    "SystemClock",
    "WeightedStrategy",
    "__version__",
]
