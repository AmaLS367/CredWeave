"""Application service coordinating credential leasing, rotation, and lifecycle reporting."""

import asyncio
import threading
import uuid
from collections.abc import Callable, Sequence

from credweave.application.ports.clock import Clock
from credweave.application.ports.credential_source import CredentialSource
from credweave.application.ports.state_store import CredentialRecord, StateStore
from credweave.application.ports.strategy import (
    CredentialCandidate,
    SelectionContext,
    SelectionStrategy,
)
from credweave.domain.enums import CredentialState
from credweave.domain.errors import (
    ConfigurationError,
    CredentialAlreadyExistsError,
    InvalidLeaseError,
    InvalidOutcomeError,
    LeaseExpiredError,
    NoCredentialsAvailableError,
)
from credweave.domain.models import Credential, Lease
from credweave.domain.outcomes import Outcome

ClockFactory = Callable[[], Clock]
StrategyFactory = Callable[[], SelectionStrategy]
SourceFactory = Callable[[Sequence[Credential]], CredentialSource]
StoreFactory = Callable[[Clock, float, int], StateStore]

_default_clock_factory: ClockFactory | None = None
_default_strategy_factory: StrategyFactory | None = None
_default_source_factory: SourceFactory | None = None
_default_store_factory: StoreFactory | None = None


def register_default_adapters(
    *,
    clock_factory: ClockFactory | None = None,
    strategy_factory: StrategyFactory | None = None,
    source_factory: SourceFactory | None = None,
    store_factory: StoreFactory | None = None,
) -> None:
    """Register default adapter factories for pool initialization.

    Invoked by the composition/public layer to decouple application services
    from concrete infrastructure adapters.
    """
    global _default_clock_factory, _default_strategy_factory
    global _default_source_factory, _default_store_factory
    if clock_factory is not None:
        _default_clock_factory = clock_factory
    if strategy_factory is not None:
        _default_strategy_factory = strategy_factory
    if source_factory is not None:
        _default_source_factory = source_factory
    if store_factory is not None:
        _default_store_factory = store_factory


class CredentialPool:
    """Manages a pool of credentials with rotation, cooldown, and health tracking.

    Args:
        credentials: Optional initial collection of credentials.
        source: Optional dynamic credential source (e.g. env, file, or cloud secret manager).
        strategy: Optional selection algorithm (e.g. round-robin, least-used, failover).
        store: Optional state persistence adapter (e.g. in-memory, SQLite, Redis).
        clock: Optional clock instance for deterministic time and cooldown evaluation.
        max_consecutive_failures: Number of consecutive failures before marking UNHEALTHY.
        default_cooldown: Default cooldown seconds when not specified by outcome.
        lease_timeout: Optional max lifespan in seconds for a lease before LeaseExpiredError.
    """

    def __init__(
        self,
        credentials: Sequence[Credential] | None = None,
        *,
        source: CredentialSource | None = None,
        strategy: SelectionStrategy | None = None,
        store: StateStore | None = None,
        clock: Clock | None = None,
        max_consecutive_failures: int = 3,
        default_cooldown: float = 60.0,
        lease_timeout: float | None = None,
    ) -> None:
        if credentials is None and source is None:
            raise ConfigurationError(
                "CredentialPool requires at least one of 'credentials' or 'source'."
            )

        initial_creds = list(credentials or ())
        seen_ids: set[str] = set()
        for cred in initial_creds:
            if not isinstance(cred, Credential):
                raise ConfigurationError(
                    f"Expected Credential instance, got {type(cred).__name__}."
                )
            if cred.id in seen_ids:
                raise CredentialAlreadyExistsError(cred.id)
            seen_ids.add(cred.id)

        self._initial_credentials: tuple[Credential, ...] = tuple(initial_creds)

        if clock is not None:
            self._clock: Clock = clock
        elif _default_clock_factory is not None:
            self._clock = _default_clock_factory()
        else:
            raise ConfigurationError(
                "No clock provided and no default clock adapter is registered."
            )

        if strategy is not None:
            self._strategy: SelectionStrategy = strategy
        elif _default_strategy_factory is not None:
            self._strategy = _default_strategy_factory()
        else:
            raise ConfigurationError(
                "No strategy provided and no default strategy adapter is registered."
            )

        if source is not None:
            self._source: CredentialSource = source
        elif _default_source_factory is not None:
            self._source = _default_source_factory(self._initial_credentials)
        else:
            raise ConfigurationError(
                "No source provided and no default source adapter is registered."
            )

        if store is not None:
            self._store: StateStore = store
        elif _default_store_factory is not None:
            self._store = _default_store_factory(
                self._clock,
                default_cooldown,
                max_consecutive_failures,
            )
        else:
            raise ConfigurationError(
                "No store provided and no default store adapter is registered."
            )

        self._lease_timeout = lease_timeout

        self._active_leases: dict[str, Lease] = {}
        self._pool_lock = threading.RLock()
        self._async_lock: asyncio.Lock | None = None

        # Pre-initialize store records for initial credentials
        for cred in self._initial_credentials:
            if hasattr(self._store, "initialize_record"):
                self._store.initialize_record(cred.id)
            else:
                if self._store.get_record(cred.id) is None:
                    self._store.update_state(cred.id, CredentialState.AVAILABLE)

    def _get_async_lock(self) -> asyncio.Lock:
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock

    @property
    def initial_credentials(self) -> tuple[Credential, ...]:
        """Return the initial sequence of credentials passed during pool initialization."""
        return self._initial_credentials

    @property
    def source(self) -> CredentialSource:
        """Return the configured credential source."""
        return self._source

    @property
    def strategy(self) -> SelectionStrategy:
        """Return the configured selection strategy."""
        return self._strategy

    @property
    def store(self) -> StateStore:
        """Return the configured state store."""
        return self._store

    @property
    def clock(self) -> Clock:
        """Return the configured clock."""
        return self._clock

    @property
    def in_flight_leases(self) -> int:
        """Return the total count of currently active leases."""
        with self._pool_lock:
            return len(self._active_leases)

    @property
    def active_leases(self) -> tuple[Lease, ...]:
        """Return a snapshot tuple of all currently active leases."""
        with self._pool_lock:
            return tuple(self._active_leases.values())

    def get_credential(self, credential_id: str) -> Credential | None:
        """Retrieve a credential by its identifier from the configured source."""
        for c in self._source.get_credentials():
            if c.id == credential_id:
                return c
        return None

    def acquire_sync(self, context: SelectionContext | None = None) -> Lease:
        """Acquire a credential lease synchronously according to the configured strategy."""
        with self._pool_lock:
            credentials = self._source.get_credentials()
            if not credentials:
                raise NoCredentialsAvailableError("No credentials configured in pool.")

            records = self._store.list_records()
            record_map = {r.credential_id: r for r in records}

            candidates: list[CredentialCandidate] = []
            for cred in credentials:
                rec = record_map.get(cred.id)
                if rec is None:
                    if hasattr(self._store, "initialize_record"):
                        rec = self._store.initialize_record(cred.id)
                    else:
                        self._store.update_state(cred.id, CredentialState.AVAILABLE)
                        rec = self._store.get_record(cred.id)
                        if rec is None:
                            rec = CredentialRecord(
                                credential_id=cred.id,
                                state=CredentialState.AVAILABLE,
                            )
                candidates.append(
                    CredentialCandidate(
                        credential=cred,
                        state=rec.state,
                        in_flight_leases=rec.in_flight_leases,
                        consecutive_failures=rec.consecutive_failures,
                        cooldown_until=rec.cooldown_until,
                        metadata=rec.metadata,
                    )
                )

            selected = self._strategy.select(candidates, context)
            if selected is None:
                raise NoCredentialsAvailableError("No eligible credentials available in pool.")

            now = self._clock.now()
            lease_id = f"lease_{uuid.uuid4().hex}"
            lease = Lease(
                credential=selected.credential,
                lease_id=lease_id,
                acquired_at=now,
            )

            if hasattr(self._store, "record_acquire"):
                self._store.record_acquire(selected.credential_id, now)

            self._active_leases[lease_id] = lease
            return lease

    async def acquire(self, context: SelectionContext | None = None) -> Lease:
        """Acquire a credential lease asynchronously according to the configured strategy."""
        async with self._get_async_lock():
            credentials = await self._source.get_credentials_async()
            if not credentials:
                raise NoCredentialsAvailableError("No credentials configured in pool.")

            records = await self._store.list_records_async()
            record_map = {r.credential_id: r for r in records}

            candidates: list[CredentialCandidate] = []
            for cred in credentials:
                rec = record_map.get(cred.id)
                if rec is None:
                    if hasattr(self._store, "initialize_record"):
                        rec = self._store.initialize_record(cred.id)
                    else:
                        await self._store.update_state_async(cred.id, CredentialState.AVAILABLE)
                        rec = await self._store.get_record_async(cred.id)
                        if rec is None:
                            rec = CredentialRecord(
                                credential_id=cred.id,
                                state=CredentialState.AVAILABLE,
                            )
                candidates.append(
                    CredentialCandidate(
                        credential=cred,
                        state=rec.state,
                        in_flight_leases=rec.in_flight_leases,
                        consecutive_failures=rec.consecutive_failures,
                        cooldown_until=rec.cooldown_until,
                        metadata=rec.metadata,
                    )
                )

            selected = self._strategy.select(candidates, context)
            if selected is None:
                raise NoCredentialsAvailableError("No eligible credentials available in pool.")

            now = self._clock.now()
            lease_id = f"lease_{uuid.uuid4().hex}"
            lease = Lease(
                credential=selected.credential,
                lease_id=lease_id,
                acquired_at=now,
            )

            if hasattr(self._store, "record_acquire_async"):
                await self._store.record_acquire_async(selected.credential_id, now)
            elif hasattr(self._store, "record_acquire"):
                self._store.record_acquire(selected.credential_id, now)

            with self._pool_lock:
                self._active_leases[lease_id] = lease

            return lease

    def report_sync(self, lease: Lease, outcome: Outcome) -> None:
        """Report execution outcome for an active lease synchronously."""
        if not isinstance(lease, Lease):
            raise InvalidLeaseError(
                getattr(lease, "lease_id", "unknown"),
                "Expected Lease instance.",
            )
        if not isinstance(outcome, Outcome):
            raise InvalidOutcomeError(f"Expected Outcome instance, got {type(outcome).__name__}.")

        now = self._clock.now()

        with self._pool_lock:
            if lease.lease_id not in self._active_leases:
                raise InvalidLeaseError(
                    lease.lease_id,
                    "Lease is unknown, already reported, or has expired.",
                )
            tracked_lease = self._active_leases[lease.lease_id]
            if tracked_lease.credential_id != lease.credential_id:
                raise InvalidLeaseError(
                    lease.lease_id,
                    f"Lease credential mismatch (expected {tracked_lease.credential_id!r}, "
                    f"got {lease.credential_id!r}).",
                )

            is_expired = False
            if self._lease_timeout is not None:
                elapsed = (now - tracked_lease.acquired_at).total_seconds()
                if elapsed > self._lease_timeout:
                    is_expired = True

            self._active_leases.pop(lease.lease_id)

        if is_expired:
            try:
                if hasattr(self._store, "release_lease"):
                    self._store.release_lease(lease.credential_id)
            except Exception:
                with self._pool_lock:
                    self._active_leases[lease.lease_id] = tracked_lease
                raise
            raise LeaseExpiredError(lease.lease_id)

        try:
            self._store.record_outcome(lease.credential_id, outcome, now)
        except Exception:
            with self._pool_lock:
                self._active_leases[lease.lease_id] = tracked_lease
            raise

    async def report(self, lease: Lease, outcome: Outcome) -> None:
        """Report execution outcome for an active lease asynchronously."""
        if not isinstance(lease, Lease):
            raise InvalidLeaseError(
                getattr(lease, "lease_id", "unknown"),
                "Expected Lease instance.",
            )
        if not isinstance(outcome, Outcome):
            raise InvalidOutcomeError(f"Expected Outcome instance, got {type(outcome).__name__}.")

        now = self._clock.now()

        with self._pool_lock:
            if lease.lease_id not in self._active_leases:
                raise InvalidLeaseError(
                    lease.lease_id,
                    "Lease is unknown, already reported, or has expired.",
                )
            tracked_lease = self._active_leases[lease.lease_id]
            if tracked_lease.credential_id != lease.credential_id:
                raise InvalidLeaseError(
                    lease.lease_id,
                    f"Lease credential mismatch (expected {tracked_lease.credential_id!r}, "
                    f"got {lease.credential_id!r}).",
                )

            is_expired = False
            if self._lease_timeout is not None:
                elapsed = (now - tracked_lease.acquired_at).total_seconds()
                if elapsed > self._lease_timeout:
                    is_expired = True

            self._active_leases.pop(lease.lease_id)

        if is_expired:
            try:
                if hasattr(self._store, "release_lease_async"):
                    await self._store.release_lease_async(lease.credential_id)
                elif hasattr(self._store, "release_lease"):
                    self._store.release_lease(lease.credential_id)
            except Exception:
                with self._pool_lock:
                    self._active_leases[lease.lease_id] = tracked_lease
                raise
            raise LeaseExpiredError(lease.lease_id)

        try:
            if hasattr(self._store, "record_outcome_async"):
                await self._store.record_outcome_async(lease.credential_id, outcome, now)
            else:
                self._store.record_outcome(lease.credential_id, outcome, now)
        except Exception:
            with self._pool_lock:
                self._active_leases[lease.lease_id] = tracked_lease
            raise

    def reset_credential(self, credential_id: str) -> None:
        """Reset credential state to AVAILABLE and clear failures and cooldowns synchronously."""
        if hasattr(self._store, "reset"):
            self._store.reset(credential_id)
        else:
            self._store.update_state(credential_id, CredentialState.AVAILABLE)

    async def reset_credential_async(self, credential_id: str) -> None:
        """Reset a credential's state asynchronously."""
        if hasattr(self._store, "reset_async"):
            await self._store.reset_async(credential_id)
        elif hasattr(self._store, "reset"):
            self._store.reset(credential_id)
        else:
            await self._store.update_state_async(credential_id, CredentialState.AVAILABLE)

    def get_record(self, credential_id: str) -> CredentialRecord | None:
        """Retrieve the state record for a credential synchronously."""
        return self._store.get_record(credential_id)

    async def get_record_async(self, credential_id: str) -> CredentialRecord | None:
        """Retrieve the state record for a credential asynchronously."""
        return await self._store.get_record_async(credential_id)

    def list_records(self) -> Sequence[CredentialRecord]:
        """List all credential state records synchronously."""
        return self._store.list_records()

    async def list_records_async(self) -> Sequence[CredentialRecord]:
        """List all credential state records asynchronously."""
        return await self._store.list_records_async()
