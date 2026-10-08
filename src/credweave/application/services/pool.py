"""Application service coordinating credential leasing, rotation, and lifecycle reporting."""

import asyncio
import math
import threading
import uuid
import weakref
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta

from credweave.application.ports.clock import Clock
from credweave.application.ports.credential_source import CredentialSource
from credweave.application.ports.state_store import (
    CredentialRecord,
    LeaseRecord,
    LeaseReservation,
    LeaseSettlement,
    StateStore,
)
from credweave.application.ports.strategy import (
    CredentialCandidate,
    SelectionContext,
    SelectionStrategy,
)
from credweave.application.services.lifecycle import LifecycleEngine
from credweave.domain.backoff import BackoffPolicy, RandomSource
from credweave.domain.concurrency import (
    MAX_CONCURRENCY_METADATA_KEY,
    validate_max_concurrency,
)
from credweave.domain.enums import CredentialState
from credweave.domain.errors import (
    ConfigurationError,
    CredentialAlreadyExistsError,
    CredentialSourceError,
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
StoreFactory = Callable[[Clock, LifecycleEngine], StateStore]

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
        default_cooldown: Fixed cooldown seconds when the outcome carries no ``retry_after``.
            Ignored when ``backoff`` is given.
        backoff: Optional backoff policy (fixed or exponential, with optional jitter) deciding
            cooldown delays. When omitted, a fixed ``default_cooldown`` is used and an upstream
            ``retry_after`` replaces it.
        rng: Optional jitter randomness source returning floats in ``[0, 1)``; pass a seeded
            ``random.Random(seed).random`` for deterministic jitter.
        lease_timeout: Optional max lifespan in seconds (finite, greater than 0) of a lease.
            A lease still unreported after this time is reclaimed automatically: its concurrency
            slot is released without applying any outcome, and a later report raises
            :class:`~credweave.domain.errors.LeaseExpiredError`.
        max_concurrency_per_credential: Optional pool-wide cap on in-flight leases per credential
            (an integer of at least 1; ``None`` means unlimited). A credential may override it
            through its ``max_concurrency`` metadata (``None`` there means unlimited). Credentials
            at their cap are temporarily skipped without any change to their health state.

    Note:
        ``max_consecutive_failures``, ``default_cooldown``, ``backoff`` and ``rng`` configure the
        lifecycle engine of the default state store. A custom ``store`` carries its own
        :class:`LifecycleEngine`.

        Lease slots live in the state store, so concurrency caps and lease reclamation hold across
        every pool, thread and asyncio task sharing one store. Expired leases are reclaimed
        automatically during acquire and report; no background task is involved.
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
        max_concurrency_per_credential: int | None = None,
        backoff: BackoffPolicy | None = None,
        rng: RandomSource | None = None,
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
                LifecycleEngine(
                    backoff=backoff,
                    max_consecutive_failures=max_consecutive_failures,
                    default_cooldown=default_cooldown,
                    rng=rng,
                ),
            )
        else:
            raise ConfigurationError(
                "No store provided and no default store adapter is registered."
            )

        self._max_concurrency_per_credential = validate_max_concurrency(
            max_concurrency_per_credential, "max_concurrency_per_credential"
        )
        self._lease_timeout = lease_timeout
        self._lease_ttl = self._validate_lease_timeout(lease_timeout)
        for cred in self._initial_credentials:
            self._concurrency_limit(cred)

        self._pool_lock = threading.RLock()
        # Leases handed out by this pool, kept only while the caller still holds them. Lets
        # ``active_leases`` report the Credential a lease was actually granted with, even after
        # a dynamic source rotated or removed that credential.
        self._granted_leases: weakref.WeakValueDictionary[str, Lease] = (
            weakref.WeakValueDictionary()
        )
        self._async_lock: asyncio.Lock | None = None

        # Pre-initialize store records for initial credentials
        for cred in self._initial_credentials:
            if hasattr(self._store, "initialize_record"):
                self._store.initialize_record(cred.id)
            else:
                if self._store.get_record(cred.id) is None:
                    self._store.update_state(cred.id, CredentialState.AVAILABLE)

        self._observe_starting_secrets()

    def _observe_starting_secrets(self) -> None:
        """Make the store aware of the secrets this pool starts with.

        The store cannot order two secrets it has never seen, so a snapshot read long before
        another pool rotated would be adopted as the newer secret. Observing the starting secrets
        at construction lets the store recognise such a snapshot as superseded. A source that
        cannot be read yet is skipped: the next acquire reads it again and reports the error.
        """
        if not hasattr(self._store, "sync_credential"):
            return
        try:
            credentials = self._source.get_credentials()
        except CredentialSourceError:
            return
        for cred in credentials:
            self._store.sync_credential(cred.id, secret_fingerprint=cred.secret_fingerprint)

    def _validate_lease_timeout(self, lease_timeout: float | None) -> timedelta | None:
        if lease_timeout is None:
            return None
        if (
            isinstance(lease_timeout, bool)
            or not isinstance(lease_timeout, (int, float))
            or not math.isfinite(lease_timeout)
            or lease_timeout <= 0
        ):
            raise ConfigurationError(
                "lease_timeout must be None or a finite number of seconds greater than 0."
            )
        try:
            ttl = timedelta(seconds=lease_timeout)
            self._clock.now() + ttl
        except OverflowError as exc:
            raise ConfigurationError(
                "lease_timeout is too large to be represented as a deadline."
            ) from exc
        return ttl

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
        """Return the count of leases currently holding a slot in the state store.

        Leases that have timed out but have not been reclaimed yet still count; they are
        released by the next acquire, report or :meth:`reclaim_expired_leases` call.
        """
        return len(self._store.list_active_leases())

    @property
    def active_leases(self) -> tuple[Lease, ...]:
        """Return a snapshot tuple of all leases currently holding a slot in the state store."""
        records = self._store.list_active_leases()
        known = {c.id: c for c in self._source.get_credentials()}
        leases: list[Lease] = []
        for r in records:
            granted = self._granted_leases.get(r.lease_id)
            credential = (
                granted.credential
                if granted is not None and granted.credential_id == r.credential_id
                else known.get(r.credential_id) or Credential(id=r.credential_id)
            )
            leases.append(
                Lease(credential=credential, lease_id=r.lease_id, acquired_at=r.acquired_at)
            )
        return tuple(leases)

    def get_credential(self, credential_id: str) -> Credential | None:
        """Retrieve a credential by its identifier from the configured source."""
        for c in self._source.get_credentials():
            if c.id == credential_id:
                return c
        return None

    def _concurrency_limit(self, credential: Credential) -> int | None:
        """Resolve a credential's concurrency cap: its metadata override, else the pool default.

        An explicit ``max_concurrency`` of ``None`` in the metadata means unlimited.
        """
        if MAX_CONCURRENCY_METADATA_KEY in credential.metadata:
            return validate_max_concurrency(
                credential.metadata[MAX_CONCURRENCY_METADATA_KEY],
                f"Metadata {MAX_CONCURRENCY_METADATA_KEY!r} on credential {credential.id!r}",
            )
        return self._max_concurrency_per_credential

    def _expires_at(self, acquired_at: datetime) -> datetime | None:
        return None if self._lease_ttl is None else acquired_at + self._lease_ttl

    def _build_candidates(
        self,
        credentials: Sequence[Credential],
        records: Mapping[str, CredentialRecord],
        excluded: set[str],
    ) -> list[CredentialCandidate]:
        """Snapshot the credentials a strategy may choose from.

        Credentials at their concurrency cap, and those whose reservation was just lost to a
        concurrent acquirer or a concurrent state change (``excluded``), are left out: they are
        temporarily ineligible and their health state is not touched. Every credential has a
        record in ``records``.
        """
        candidates: list[CredentialCandidate] = []
        for cred in credentials:
            rec = records[cred.id]
            limit = self._concurrency_limit(cred)
            if cred.id in excluded or (limit is not None and rec.in_flight_leases >= limit):
                continue
            candidates.append(
                CredentialCandidate(
                    credential=cred,
                    state=rec.state,
                    in_flight_leases=rec.in_flight_leases,
                    consecutive_failures=rec.consecutive_failures,
                    cooldown_until=rec.cooldown_until,
                    total_leases=rec.total_leases,
                    last_used_at=rec.last_used_at,
                    metadata=rec.metadata,
                )
            )
        return candidates

    def _new_lease(self, credential: Credential) -> Lease:
        return Lease(
            credential=credential,
            lease_id=f"lease_{uuid.uuid4().hex}",
            acquired_at=self._clock.now(),
        )

    def acquire_sync(self, context: SelectionContext | None = None) -> Lease:
        """Acquire a credential lease synchronously according to the configured strategy.

        The source is re-read on every selection round, so a dynamic source's rotated, added or
        removed credentials take effect on the next acquire. Store state is keyed by the stable
        credential id: a lease granted from a snapshot taken just before the source changed
        keeps that snapshot's ``Credential`` object and stays reportable, and its accounting
        is unaffected by the change.

        Expired leases are reclaimed first. The chosen credential's eligibility and concurrency
        slot are then verified and claimed atomically in the state store. If another acquirer
        took the last slot, a concurrent report changed the credential's state (revoked,
        rate limited, cooling down...), or a rotation superseded the selected snapshot, between
        selection and reservation, that credential is
        skipped for this call, candidates are refreshed and another one is selected. Losing
        such a race never changes a credential's health state.
        """
        with self._pool_lock:
            self._reclaim_sync()
            excluded: set[str] = set()
            while True:
                credentials = self._source.get_credentials()
                if not credentials:
                    raise NoCredentialsAvailableError("No credentials configured in pool.")

                records = {r.credential_id: r for r in self._store.list_records()}
                for cred in credentials:
                    if cred.id not in records:
                        records[cred.id] = self._initialize_record_sync(cred.id)
                    fp = getattr(cred, "secret_fingerprint", None)
                    if fp is not None and hasattr(self._store, "sync_credential"):
                        records[cred.id] = self._store.sync_credential(
                            cred.id, secret_fingerprint=fp
                        )

                candidates = self._build_candidates(credentials, records, excluded)
                selected = self._strategy.select(candidates, context)
                if selected is None:
                    raise NoCredentialsAvailableError("No eligible credentials available in pool.")

                lease = self._new_lease(selected.credential)
                reservation = self._store.reserve_lease(
                    selected.credential_id,
                    lease.lease_id,
                    lease.acquired_at,
                    max_concurrency=self._concurrency_limit(selected.credential),
                    expires_at=self._expires_at(lease.acquired_at),
                    secret_fingerprint=getattr(selected.credential, "secret_fingerprint", None),
                )
                if reservation is LeaseReservation.RESERVED:
                    self._granted_leases[lease.lease_id] = lease
                    return lease
                # AT_CAPACITY, INELIGIBLE or STALE: a lost race, not a credential fault.
                excluded.add(selected.credential_id)

    async def acquire(self, context: SelectionContext | None = None) -> Lease:
        """Acquire a credential lease asynchronously according to the configured strategy."""
        async with self._get_async_lock():
            await self._reclaim_async()
            excluded: set[str] = set()
            while True:
                credentials = await self._source.get_credentials_async()
                if not credentials:
                    raise NoCredentialsAvailableError("No credentials configured in pool.")

                records = {r.credential_id: r for r in await self._store.list_records_async()}
                for cred in credentials:
                    if cred.id not in records:
                        records[cred.id] = await self._initialize_record_async(cred.id)
                    fp = getattr(cred, "secret_fingerprint", None)
                    if fp is not None:
                        if hasattr(self._store, "sync_credential_async"):
                            records[cred.id] = await self._store.sync_credential_async(
                                cred.id, secret_fingerprint=fp
                            )
                        elif hasattr(self._store, "sync_credential"):
                            records[cred.id] = self._store.sync_credential(
                                cred.id, secret_fingerprint=fp
                            )

                candidates = self._build_candidates(credentials, records, excluded)
                with self._pool_lock:  # strategies are shared with concurrent sync acquirers
                    selected = self._strategy.select(candidates, context)
                if selected is None:
                    raise NoCredentialsAvailableError("No eligible credentials available in pool.")

                lease = self._new_lease(selected.credential)
                reservation = await self._store.reserve_lease_async(
                    selected.credential_id,
                    lease.lease_id,
                    lease.acquired_at,
                    max_concurrency=self._concurrency_limit(selected.credential),
                    expires_at=self._expires_at(lease.acquired_at),
                    secret_fingerprint=getattr(selected.credential, "secret_fingerprint", None),
                )
                if reservation is LeaseReservation.RESERVED:
                    self._granted_leases[lease.lease_id] = lease
                    return lease
                # AT_CAPACITY, INELIGIBLE or STALE: a lost race, not a credential fault.
                excluded.add(selected.credential_id)

    def _initialize_record_sync(self, credential_id: str) -> CredentialRecord:
        if hasattr(self._store, "initialize_record"):
            initialized: CredentialRecord = self._store.initialize_record(credential_id)
            return initialized
        self._store.update_state(credential_id, CredentialState.AVAILABLE)
        return self._store.get_record(credential_id) or CredentialRecord(
            credential_id=credential_id, state=CredentialState.AVAILABLE
        )

    async def _initialize_record_async(self, credential_id: str) -> CredentialRecord:
        if hasattr(self._store, "initialize_record"):
            initialized: CredentialRecord = self._store.initialize_record(credential_id)
            return initialized
        await self._store.update_state_async(credential_id, CredentialState.AVAILABLE)
        return await self._store.get_record_async(credential_id) or CredentialRecord(
            credential_id=credential_id, state=CredentialState.AVAILABLE
        )

    def reclaim_expired_leases(self) -> tuple[LeaseRecord, ...]:
        """Release the concurrency slot of every lease whose deadline has passed.

        Reclamation also happens automatically on every acquire and report, so calling this is
        only needed to free capacity eagerly (for example from a periodic task) or to learn
        which leases were orphaned. It applies no outcome, leaves credential health untouched,
        releases each lease exactly once and is idempotent. Reclaimed leases can no longer be
        reported (:class:`~credweave.domain.errors.LeaseExpiredError`).

        Returns:
            The leases reclaimed by this call; empty when nothing had expired, in particular
            when no ``lease_timeout`` is configured anywhere.
        """
        return self._reclaim_sync()

    async def reclaim_expired_leases_async(self) -> tuple[LeaseRecord, ...]:
        """Asynchronous equivalent of :meth:`reclaim_expired_leases`."""
        return await self._reclaim_async()

    def _reclaim_sync(self) -> tuple[LeaseRecord, ...]:
        return tuple(self._store.reclaim_expired_leases(self._clock.now()))

    async def _reclaim_async(self) -> tuple[LeaseRecord, ...]:
        return tuple(await self._store.reclaim_expired_leases_async(self._clock.now()))

    @staticmethod
    def _check_report_arguments(lease: Lease, outcome: Outcome) -> None:
        if not isinstance(lease, Lease):
            raise InvalidLeaseError(
                getattr(lease, "lease_id", "unknown"),
                "Expected Lease instance.",
            )
        if not isinstance(outcome, Outcome):
            raise InvalidOutcomeError(f"Expected Outcome instance, got {type(outcome).__name__}.")

    @staticmethod
    def _raise_for_settlement(lease: Lease, settlement: LeaseSettlement) -> None:
        if settlement is LeaseSettlement.SETTLED:
            return
        if settlement is LeaseSettlement.EXPIRED:
            raise LeaseExpiredError(lease.lease_id)
        if settlement is LeaseSettlement.MISMATCH:
            raise InvalidLeaseError(
                lease.lease_id,
                f"Lease credential mismatch (credential {lease.credential_id!r} does not "
                "own this lease).",
            )
        raise InvalidLeaseError(
            lease.lease_id,
            "Lease is unknown, already reported, or has expired.",
        )

    def report_sync(self, lease: Lease, outcome: Outcome) -> None:
        """Report execution outcome for an active lease synchronously.

        The lease is settled atomically in the state store: its slot is released and the
        outcome applied exactly once. If the store fails, the lease stays active and the call
        can be retried. A lease past its deadline, or already reclaimed, raises
        :class:`~credweave.domain.errors.LeaseExpiredError` without applying the outcome.
        """
        self._check_report_arguments(lease, outcome)
        raw_secrets = getattr(lease.credential, "_raw_secrets", {})
        secret_values = raw_secrets.values() if hasattr(raw_secrets, "values") else ()
        safe_outcome = outcome.redact(secret_values)
        self._reclaim_sync()
        settlement = self._store.settle_lease(
            lease.lease_id, lease.credential_id, safe_outcome, self._clock.now()
        )
        self._raise_for_settlement(lease, settlement)

    async def report(self, lease: Lease, outcome: Outcome) -> None:
        """Report execution outcome for an active lease asynchronously."""
        self._check_report_arguments(lease, outcome)
        raw_secrets = getattr(lease.credential, "_raw_secrets", {})
        secret_values = raw_secrets.values() if hasattr(raw_secrets, "values") else ()
        safe_outcome = outcome.redact(secret_values)
        await self._reclaim_async()
        settlement = await self._store.settle_lease_async(
            lease.lease_id, lease.credential_id, safe_outcome, self._clock.now()
        )
        self._raise_for_settlement(lease, settlement)

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
