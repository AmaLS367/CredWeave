"""Application service coordinating credential leasing, rotation, and lifecycle reporting."""

import asyncio
import inspect
import math
import threading
import uuid
import weakref
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

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
    CredentialNotFoundError,
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


def _accepts_argument(method: Callable[..., Any], name: str) -> bool:
    """Whether ``method`` takes ``name`` as a parameter or through ``**kwargs``."""
    parameters = inspect.signature(method).parameters.values()
    return any(p.name == name or p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters)


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

        Construction reads the source once, to record the secrets it presents as the baseline
        for rotation safety; a source error is raised, not deferred. A secret the store has not
        seen is adopted as a rotation only by a pool advancing from the secret it last observed.
        A pool built over a store that already holds another secret, or whose last observation
        was superseded, cannot advance it: call :meth:`authorize_secret` to adopt the source's
        secret explicitly. Without trustworthy source revisions, a newer secret is never told
        apart from an older one that was never seen. A rotation never recovers a ``REVOKED`` or
        ``UNHEALTHY`` credential: call :meth:`authorize_secret` once the secret is repaired. A
        custom ``store`` must implement the secret-generation contract documented on
        :class:`~credweave.application.ports.state_store.StateStore`, or construction fails.

        Every change of secret generation made through a pool (acquisition, authorization) is
        committed under the pool lock, and only from a source read that no other generation
        change overtook. Synchronous and asynchronous calls on one pool, from any thread or task,
        therefore cannot invalidate each other's generation guarantees. The source itself may
        still change at any instant; a generation always reflects a read taken under the lock.

        Lease slots live in the state store, so concurrency caps and lease reclamation hold across
        every pool, thread and asyncio task sharing one store. Expired leases are reclaimed
        automatically during acquire and report; no background task is involved.
    """

    _AUTHORIZE_ATTEMPTS = 8
    """How many times :meth:`authorize_secret` re-reads a source that keeps changing."""

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

        self._require_secret_generation_contract()

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
        self._async_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
        """One lock per event loop that runs asynchronous calls on this pool, guarded by
        ``_pool_lock``. An ``asyncio.Lock`` binds to the loop it first waits on, so a single lock
        would break the pool for every later loop."""
        self._observed: dict[str, str] = {}
        """The secret fingerprint this pool last presented to the store, per credential id."""
        self._generation_epoch = 0
        """Bumped by every generation change this pool commits. An asynchronous read taken before
        a bump is not trusted and is read again. Guarded by ``_pool_lock``."""

        # Pre-initialize store records for initial credentials
        for cred in self._initial_credentials:
            if hasattr(self._store, "initialize_record"):
                self._store.initialize_record(cred.id)
            else:
                if self._store.get_record(cred.id) is None:
                    self._store.update_state(cred.id, CredentialState.AVAILABLE)

        self._observe_starting_secrets()

    def _require_secret_generation_contract(self) -> None:
        """Refuse a store that cannot version secrets, rather than run without rotation safety.

        Every acquire synchronises the source's fingerprint with ``sync_credential`` and passes
        the candidate's fingerprint to ``reserve_lease``. A store lacking either would silently
        lose stale-snapshot and rollback protection.
        """
        missing: list[str] = []
        sync = getattr(self._store, "sync_credential", None)
        if sync is None:
            missing.append("sync_credential()")
        elif not _accepts_argument(sync, "last_observed"):
            missing.append("a last_observed argument on sync_credential()")
        if not _accepts_argument(self._store.reserve_lease, "secret_fingerprint"):
            missing.append("a secret_fingerprint argument on reserve_lease()")
        if missing:
            raise ConfigurationError(
                "The state store does not implement secret generations: missing "
                f"{' and '.join(missing)}. See the StateStore docstring for the contract."
            )

    def _observe_starting_secrets(self) -> None:
        """Make the store aware of the secrets the source presents when the pool is built.

        This is the one source read made at construction, and it is deliberate. A secret the
        source presented before the store first synchronised with it could otherwise be adopted
        as a rotation when the source reverts to it. Errors propagate: a pool whose baseline
        cannot be established is not built, so no error is silently deferred.
        """
        with self._pool_lock:
            for cred in self._source.get_credentials():
                self._observe_locked(cred)

    def _observe_locked(self, credential: Credential) -> CredentialRecord:
        """Synchronise the secret a source presents, as this pool's next observation.

        The store adopts an unseen secret only when this pool advances from the secret it last
        observed (see :meth:`StateStore.sync_credential`). Lock must be held.
        """
        sync = self._store_method("sync_credential")
        previous = self._observed.get(credential.id)
        if previous != credential.secret_fingerprint:
            self._generation_epoch += 1
        record: CredentialRecord = sync(
            credential.id,
            secret_fingerprint=credential.secret_fingerprint,
            last_observed=previous,
        )
        self._observed[credential.id] = credential.secret_fingerprint
        return record

    def _source_fingerprint(self, credential_id: str) -> str:
        """Return the secret fingerprint the source presents for ``credential_id`` now."""
        for cred in self._source.get_credentials():
            if cred.id == credential_id:
                return cred.secret_fingerprint
        raise CredentialNotFoundError(credential_id)

    async def _source_fingerprint_async(self, credential_id: str) -> str:
        """Asynchronous equivalent of :meth:`_source_fingerprint`."""
        for cred in await self._source.get_credentials_async():
            if cred.id == credential_id:
                return cred.secret_fingerprint
        raise CredentialNotFoundError(credential_id)

    def authorize_secret(self, credential_id: str) -> CredentialRecord:
        """Explicitly activate the secret the source presents for ``credential_id``.

        This is how a REVOKED or UNHEALTHY credential is recovered after its secret was repaired
        or replaced: rotation alone never recovers it. The secret becomes the active generation
        and the credential is reset to AVAILABLE. To roll back, restore the earlier secret in the
        source first. It then gets a new generation, so leases granted under its earlier
        generation remain isolated and their outcomes are still ignored.

        The source is read again after the store is updated. If the secret changed in between,
        the newer one is authorized instead, so the store never stays on a secret the source has
        already moved away from.

        Raises:
            CredentialNotFoundError: The source does not present ``credential_id``.
            CredentialSourceError: The source cannot be read, or it kept changing throughout.
        """
        # Held like acquire's selection, so no acquire of this pool can read and synchronise a
        # secret between the read and the adoption made here.
        with self._pool_lock:
            fingerprint = self._source_fingerprint(credential_id)
            for _ in range(self._AUTHORIZE_ATTEMPTS):
                record = self._authorize_locked(credential_id, fingerprint)
                latest = self._source_fingerprint(credential_id)
                if latest == fingerprint:
                    return record
                fingerprint = latest
        raise CredentialSourceError(
            f"The source for credential {credential_id!r} kept changing during authorization."
        )

    async def authorize_secret_async(self, credential_id: str) -> CredentialRecord:
        """Asynchronous equivalent of :meth:`authorize_secret`.

        The source is read without holding the pool lock, so the read is committed only if no
        generation change overtook it while it was pending; otherwise it is read again.
        """
        async with self._get_async_lock():
            for _ in range(self._AUTHORIZE_ATTEMPTS):
                with self._pool_lock:
                    epoch = self._generation_epoch
                fingerprint = await self._source_fingerprint_async(credential_id)
                with self._pool_lock:
                    if self._generation_epoch != epoch:
                        continue
                    record = self._authorize_locked(credential_id, fingerprint)
                if await self._source_fingerprint_async(credential_id) == fingerprint:
                    return record
        raise CredentialSourceError(
            f"The source for credential {credential_id!r} kept changing during authorization."
        )

    def _authorize_locked(self, credential_id: str, fingerprint: str) -> CredentialRecord:
        """Make ``fingerprint`` the active secret of ``credential_id``. Lock must be held."""
        authorize = self._store_method("authorize_secret")
        self._generation_epoch += 1
        record: CredentialRecord = authorize(credential_id, fingerprint)
        self._observed[credential_id] = fingerprint
        return record

    def _store_method(self, name: str) -> Any:
        """Return the store's ``name`` method, or fail closed when the store does not offer it."""
        method = getattr(self._store, name, None)
        if method is None:
            raise ConfigurationError(f"The state store does not implement {name}().")
        return method

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
        """Return the lock that serialises asynchronous calls on the running event loop.

        Locks of loops that have since closed are dropped. Correctness across loops does not
        depend on this lock: reservations are atomic in the store and selections run under
        ``_pool_lock``.
        """
        loop = asyncio.get_running_loop()
        with self._pool_lock:
            for closed in [known for known in self._async_locks if known.is_closed()]:
                del self._async_locks[closed]
            lock = self._async_locks.get(loop)
            if lock is None:
                lock = self._async_locks[loop] = asyncio.Lock()
            return lock

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
        stale: set[tuple[str, str]],
    ) -> list[CredentialCandidate]:
        """Snapshot the credentials a strategy may choose from.

        Credentials at their concurrency cap, and those whose reservation was just lost to a
        concurrent acquirer or a concurrent state change (``excluded``), are left out: they are
        temporarily ineligible and their health state is not touched. A superseded secret
        (``stale``, as credential id and fingerprint) is left out only while the source still
        presents that exact secret. Every credential has a record in ``records``.
        """
        candidates: list[CredentialCandidate] = []
        for cred in credentials:
            rec = records[cred.id]
            limit = self._concurrency_limit(cred)
            if (
                cred.id in excluded
                or (cred.id, cred.secret_fingerprint) in stale
                or (limit is not None and rec.in_flight_leases >= limit)
            ):
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
            stale: set[tuple[str, str]] = set()
            while True:
                credentials = self._source.get_credentials()
                if not credentials:
                    raise NoCredentialsAvailableError("No credentials configured in pool.")

                selected = self._select_locked(credentials, excluded, stale, context)
                if selected is None:
                    raise NoCredentialsAvailableError("No eligible credentials available in pool.")

                lease = self._new_lease(selected.credential)
                reservation = self._store.reserve_lease(
                    selected.credential_id,
                    lease.lease_id,
                    lease.acquired_at,
                    max_concurrency=self._concurrency_limit(selected.credential),
                    expires_at=self._expires_at(lease.acquired_at),
                    secret_fingerprint=selected.credential.secret_fingerprint,
                )
                if reservation is LeaseReservation.RESERVED:
                    self._granted_leases[lease.lease_id] = lease
                    return lease
                self._exclude_lost_race(selected, reservation, excluded, stale)

    async def acquire(self, context: SelectionContext | None = None) -> Lease:
        """Acquire a credential lease asynchronously according to the configured strategy.

        Only the source is awaited. The read is committed under the pool lock, and only when no
        generation change overtook it; otherwise it is read again, so a rotation or
        authorization made by a sync caller meanwhile is never leased from a superseded snapshot.
        """
        async with self._get_async_lock():
            await self._reclaim_async()
            excluded: set[str] = set()
            stale: set[tuple[str, str]] = set()
            while True:
                with self._pool_lock:
                    epoch = self._generation_epoch
                credentials = await self._source.get_credentials_async()
                with self._pool_lock:
                    if self._generation_epoch != epoch:
                        continue
                    if not credentials:
                        raise NoCredentialsAvailableError("No credentials configured in pool.")
                    selected = self._select_locked(credentials, excluded, stale, context)
                if selected is None:
                    raise NoCredentialsAvailableError("No eligible credentials available in pool.")

                lease = self._new_lease(selected.credential)
                reservation = await self._store.reserve_lease_async(
                    selected.credential_id,
                    lease.lease_id,
                    lease.acquired_at,
                    max_concurrency=self._concurrency_limit(selected.credential),
                    expires_at=self._expires_at(lease.acquired_at),
                    secret_fingerprint=selected.credential.secret_fingerprint,
                )
                if reservation is LeaseReservation.RESERVED:
                    with self._pool_lock:
                        self._granted_leases[lease.lease_id] = lease
                    return lease
                self._exclude_lost_race(selected, reservation, excluded, stale)

    def _select_locked(
        self,
        credentials: Sequence[Credential],
        excluded: set[str],
        stale: set[tuple[str, str]],
        context: SelectionContext | None,
    ) -> CredentialCandidate | None:
        """Synchronise every presented secret, then select a candidate. Lock must be held.

        Records are read and synchronised in the same critical section as the selection, so no
        generation change can land between the two.
        """
        records = {r.credential_id: r for r in self._store.list_records()}
        for cred in credentials:
            if cred.id not in records:
                records[cred.id] = self._initialize_record_sync(cred.id)
            records[cred.id] = self._observe_locked(cred)
        candidates = self._build_candidates(credentials, records, excluded, stale)
        return self._strategy.select(candidates, context)

    @staticmethod
    def _exclude_lost_race(
        selected: CredentialCandidate,
        reservation: LeaseReservation,
        excluded: set[str],
        stale: set[tuple[str, str]],
    ) -> None:
        """Skip a candidate whose reservation was lost to a race, for the rest of this call.

        A lost race is not a credential fault. A superseded snapshot excludes only its exact
        secret, so once the source presents the current secret the credential is eligible again.
        """
        if reservation is LeaseReservation.STALE:
            stale.add((selected.credential_id, selected.credential.secret_fingerprint))
        else:  # AT_CAPACITY or INELIGIBLE
            excluded.add(selected.credential_id)

    def _initialize_record_sync(self, credential_id: str) -> CredentialRecord:
        if hasattr(self._store, "initialize_record"):
            initialized: CredentialRecord = self._store.initialize_record(credential_id)
            return initialized
        self._store.update_state(credential_id, CredentialState.AVAILABLE)
        return self._store.get_record(credential_id) or CredentialRecord(
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
