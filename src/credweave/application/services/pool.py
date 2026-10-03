"""Application service coordinating credential leasing, rotation, and lifecycle reporting."""

from collections.abc import Sequence

from credweave.application.ports.clock import Clock
from credweave.application.ports.credential_source import CredentialSource
from credweave.application.ports.state_store import StateStore
from credweave.application.ports.strategy import SelectionStrategy
from credweave.domain.errors import ConfigurationError
from credweave.domain.models import Credential, Lease
from credweave.domain.outcomes import Outcome


class CredentialPool:
    """Manages a pool of credentials with rotation, cooldown, and health tracking.

    .. note::
        This is an architectural placeholder for CredWeave 0.1.0.
        The scheduling, rotation, and lease management engines will be implemented
        in subsequent releases. Calling execution methods currently raises
        :exc:`NotImplementedError`.

    Args:
        credentials: Optional initial collection of credentials.
        source: Optional dynamic credential source (e.g. env, file, or cloud secret manager).
        strategy: Optional selection algorithm (e.g. round-robin, least-used, failover).
        store: Optional state persistence adapter (e.g. in-memory, SQLite, Redis).
        clock: Optional clock instance for deterministic time and cooldown evaluation.
    """

    def __init__(
        self,
        credentials: Sequence[Credential] | None = None,
        *,
        source: CredentialSource | None = None,
        strategy: SelectionStrategy | None = None,
        store: StateStore | None = None,
        clock: Clock | None = None,
    ) -> None:
        if credentials is None and source is None:
            raise ConfigurationError(
                "CredentialPool requires at least one of 'credentials' or 'source'."
            )

        self._initial_credentials: tuple[Credential, ...] = tuple(credentials or ())
        self._source = source
        self._strategy = strategy
        self._store = store
        self._clock = clock

    @property
    def initial_credentials(self) -> tuple[Credential, ...]:
        """Return the initial sequence of credentials passed during pool initialization."""
        return self._initial_credentials

    @property
    def source(self) -> CredentialSource | None:
        """Return the configured credential source, if any."""
        return self._source

    @property
    def strategy(self) -> SelectionStrategy | None:
        """Return the configured selection strategy, if any."""
        return self._strategy

    @property
    def store(self) -> StateStore | None:
        """Return the configured state store, if any."""
        return self._store

    @property
    def clock(self) -> Clock | None:
        """Return the configured clock, if any."""
        return self._clock

    async def acquire(self) -> Lease:
        """Acquire a credential lease asynchronously according to the configured strategy.

        Raises:
            NotImplementedError: Scheduling and lease allocation are not yet implemented in v0.1.0.
        """
        raise NotImplementedError(
            "CredentialPool.acquire() is not yet implemented in v0.1.0. "
            "Credential scheduling and rotation engine will arrive in upcoming releases."
        )

    def acquire_sync(self) -> Lease:
        """Acquire a credential lease synchronously according to the configured strategy.

        Raises:
            NotImplementedError: Scheduling and lease allocation are not yet implemented in v0.1.0.
        """
        raise NotImplementedError(
            "CredentialPool.acquire_sync() is not yet implemented in v0.1.0. "
            "Credential scheduling and rotation engine will arrive in upcoming releases."
        )

    async def report(self, lease: Lease, outcome: Outcome) -> None:
        """Report execution outcome for an active lease asynchronously.

        Raises:
            NotImplementedError: Outcome reporting and cooldown engine
                are not yet implemented in v0.1.0.
        """
        raise NotImplementedError(
            "CredentialPool.report() is not yet implemented in v0.1.0. "
            "Outcome processing and cooldown engine will arrive in upcoming releases."
        )

    def report_sync(self, lease: Lease, outcome: Outcome) -> None:
        """Report execution outcome for an active lease synchronously.

        Raises:
            NotImplementedError: Outcome reporting and cooldown engine
                are not yet implemented in v0.1.0.
        """
        raise NotImplementedError(
            "CredentialPool.report_sync() is not yet implemented in v0.1.0. "
            "Outcome processing and cooldown engine will arrive in upcoming releases."
        )
