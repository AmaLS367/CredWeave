"""Deterministic race tests for atomic lease reservation (lifecycle eligibility + capacity).

A strategy selects from a snapshot taken *before* the reservation. Between the two, other
pools, threads or tasks may report outcomes, reset or disable credentials. The store must
therefore re-check the authoritative current state inside the reservation itself, and the pool
must treat a lost race as "pick another credential" without touching health state.
"""

import concurrent.futures
import itertools
import random
import threading
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

import pytest

from credweave.application.ports.state_store import (
    CredentialRecord,
    LeaseReservation,
    LeaseSettlement,
)
from credweave.application.ports.strategy import (
    CredentialCandidate,
    SelectionContext,
    SelectionStrategy,
)
from credweave.application.services.pool import CredentialPool
from credweave.domain.enums import CredentialState
from credweave.domain.errors import NoCredentialsAvailableError
from credweave.domain.models import Credential, Lease
from credweave.domain.outcomes import Outcome
from credweave.infrastructure.stores.memory import MemoryStateStore
from credweave.strategies.failover import FailoverStrategy
from tests.conftest import TestClock
from tests.lease_helpers import assert_lease_accounting, open_leases, settle


def _cred(cid: str, **metadata: object) -> Credential:
    return Credential(id=cid, secrets={"k": f"secret-{cid}"}, metadata=metadata)


def _record(store: MemoryStateStore, cid: str) -> CredentialRecord:
    record = store.get_record(cid)
    assert record is not None
    return record


class _HookedStrategy(SelectionStrategy):
    """Delegates to an inner strategy, then runs a hook between selection and reservation."""

    def __init__(
        self,
        inner: SelectionStrategy,
        hook: Callable[[CredentialCandidate], None],
    ) -> None:
        self._inner = inner
        self._hook = hook
        self.selections: list[str] = []

    @property
    def name(self) -> str:
        return "hooked"

    def select(
        self,
        candidates: Sequence[CredentialCandidate],
        context: SelectionContext | None = None,
    ) -> CredentialCandidate | None:
        chosen = self._inner.select(candidates, context)
        if chosen is not None:
            self.selections.append(chosen.credential_id)
            self._hook(chosen)
        return chosen


class _FirstCandidateStrategy(SelectionStrategy):
    """Picks the first candidate regardless of its state, then runs a hook.

    Models a stale snapshot: the selection was made on a view that no longer holds.
    """

    def __init__(self, hook: Callable[[], None]) -> None:
        self._hook = hook

    @property
    def name(self) -> str:
        return "first-candidate"

    def select(
        self,
        candidates: Sequence[CredentialCandidate],
        context: SelectionContext | None = None,
    ) -> CredentialCandidate | None:
        if not candidates:
            return None
        self._hook()
        return candidates[0]


# --- result semantics ----------------------------------------------------------------------


def test_only_reserved_is_truthy() -> None:
    assert bool(LeaseReservation.RESERVED) is True
    assert bool(LeaseReservation.AT_CAPACITY) is False
    assert bool(LeaseReservation.INELIGIBLE) is False


# --- store: lifecycle eligibility is enforced atomically ----------------------------------


_FORCED_STATES = [
    (CredentialState.REVOKED, None),
    (CredentialState.DISABLED, None),
    (CredentialState.UNHEALTHY, None),
    (CredentialState.QUOTA_EXHAUSTED, None),
    (CredentialState.QUOTA_EXHAUSTED, 60.0),
    (CredentialState.RATE_LIMITED, 60.0),
    (CredentialState.COOLDOWN, 60.0),
]


@pytest.mark.parametrize(("state", "cooldown"), _FORCED_STATES)
def test_reserve_rejects_ineligible_state_without_side_effects(
    test_clock: TestClock, state: CredentialState, cooldown: float | None
) -> None:
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()
    store.initialize_record("c1")
    store.update_state(
        "c1",
        state,
        cooldown_until=None if cooldown is None else now + timedelta(seconds=cooldown),
    )
    before = store.get_record("c1")

    result = store.reserve_lease("c1", "l1", now, max_concurrency=5)

    assert result is LeaseReservation.INELIGIBLE
    assert store.get_record("c1") == before
    assert store.list_active_leases() == ()
    assert_lease_accounting(store)


@pytest.mark.asyncio
async def test_reserve_async_rejects_ineligible_state(test_clock: TestClock) -> None:
    store = MemoryStateStore(clock=test_clock)
    store.initialize_record("c1")
    store.update_state("c1", CredentialState.REVOKED)

    result = await store.reserve_lease_async("c1", "l1", test_clock.now())

    assert result is LeaseReservation.INELIGIBLE
    assert store.list_active_leases() == ()


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (Outcome.auth_failed(), CredentialState.REVOKED),
        (Outcome.rate_limited(retry_after=60.0), CredentialState.RATE_LIMITED),
        (Outcome.transient_error(retry_after=60.0), CredentialState.COOLDOWN),
        (Outcome.permanent_failure(), CredentialState.UNHEALTHY),
        (Outcome.quota_exhausted(retry_after=None), CredentialState.QUOTA_EXHAUSTED),
    ],
)
def test_reserve_after_concurrent_report_is_rejected(
    test_clock: TestClock, outcome: Outcome, expected: CredentialState
) -> None:
    """Another lease reports a stronger state; a later reservation must not slip through."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()
    (reporting,) = open_leases(store, "c1", 1, now)
    assert _record(store, "c1").state == CredentialState.AVAILABLE  # what a strategy saw

    settle(store, "c1", reporting, outcome, now)

    assert store.reserve_lease("c1", "late", now) is LeaseReservation.INELIGIBLE
    record = _record(store, "c1")
    assert record.state == expected
    assert record.in_flight_leases == 0
    assert record.total_leases == 1
    assert_lease_accounting(store)


def test_ineligibility_takes_precedence_over_capacity(test_clock: TestClock) -> None:
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()
    open_leases(store, "c1", 1, now)
    store.update_state("c1", CredentialState.REVOKED)

    assert store.reserve_lease("c1", "l2", now, max_concurrency=1) is LeaseReservation.INELIGIBLE


def test_capacity_is_checked_only_for_eligible_credentials(test_clock: TestClock) -> None:
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()
    open_leases(store, "c1", 1, now)

    assert store.reserve_lease("c1", "l2", now, max_concurrency=1) is LeaseReservation.AT_CAPACITY


# --- store: cooldown recovery at reservation time ------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    [
        Outcome.transient_error(retry_after=10.0),
        Outcome.rate_limited(retry_after=10.0),
        Outcome.quota_exhausted(retry_after=10.0),
    ],
)
def test_cooldown_expiring_between_snapshot_and_reservation_recovers(
    test_clock: TestClock, outcome: Outcome
) -> None:
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()
    (first,) = open_leases(store, "c1", 1, now)
    settle(store, "c1", first, outcome, now)
    snapshot = _record(store, "c1")
    assert snapshot.state != CredentialState.AVAILABLE
    assert snapshot.cooldown_until == now + timedelta(seconds=10)

    # The deadline passes after the snapshot; the reservation timestamp is authoritative.
    at_deadline = now + timedelta(seconds=10)
    assert store.reserve_lease("c1", "l2", at_deadline) is LeaseReservation.RESERVED

    record = store._records["c1"]  # raw: the recovery must be persisted by the reservation
    assert record.state == CredentialState.AVAILABLE
    assert record.cooldown_until is None
    assert record.in_flight_leases == 1
    assert record.total_leases == 2
    assert record.last_used_at == at_deadline
    assert_lease_accounting(store)


def test_recovery_keeps_failure_counter_for_probe_window(test_clock: TestClock) -> None:
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()
    (first,) = open_leases(store, "c1", 1, now)
    settle(store, "c1", first, Outcome.transient_error(retry_after=10.0), now)

    assert store.reserve_lease("c1", "l2", now + timedelta(seconds=11)) is LeaseReservation.RESERVED

    assert _record(store, "c1").consecutive_failures == 1


def test_reservation_one_instant_before_the_deadline_is_rejected(test_clock: TestClock) -> None:
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()
    (first,) = open_leases(store, "c1", 1, now)
    settle(store, "c1", first, Outcome.rate_limited(retry_after=10.0), now)
    before = store._records["c1"]

    just_before = now + timedelta(seconds=10) - timedelta(microseconds=1)
    assert store.reserve_lease("c1", "l2", just_before) is LeaseReservation.INELIGIBLE

    assert store._records["c1"] == before


def test_recovery_uses_the_reservation_timestamp_not_the_store_clock(
    test_clock: TestClock,
) -> None:
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()
    (first,) = open_leases(store, "c1", 1, now)
    settle(store, "c1", first, Outcome.rate_limited(retry_after=10.0), now)
    # The store's own clock still says the cooldown is active.
    assert test_clock.now() == now

    later = now + timedelta(seconds=30)
    assert store.reserve_lease("c1", "l2", later) is LeaseReservation.RESERVED


def test_lost_capacity_race_does_not_persist_a_recovery(test_clock: TestClock) -> None:
    """A rejected reservation changes nothing, not even the lazy cooldown recovery."""
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()
    first, second = open_leases(store, "c1", 2, now)
    settle(store, "c1", first, Outcome.transient_error(retry_after=10.0), now)
    before = store._records["c1"]
    assert before.state == CredentialState.COOLDOWN
    assert before.in_flight_leases == 1

    result = store.reserve_lease("c1", "l3", now + timedelta(seconds=20), max_concurrency=1)

    assert result is LeaseReservation.AT_CAPACITY
    assert store._records["c1"] == before
    assert [lease.lease_id for lease in store.list_active_leases()] == [second]


def test_unknown_credential_is_reserved_as_a_fresh_available_record(
    test_clock: TestClock,
) -> None:
    store = MemoryStateStore(clock=test_clock)
    now = test_clock.now()

    assert store.reserve_lease("new", "l1", now, max_concurrency=1) is LeaseReservation.RESERVED

    record = _record(store, "new")
    assert record.state == CredentialState.AVAILABLE
    assert record.in_flight_leases == 1
    assert_lease_accounting(store)


# --- pool: lifecycle-state race between selection and reservation --------------------------

_RACING_OUTCOMES = [
    (Outcome.auth_failed(), CredentialState.REVOKED),
    (Outcome.rate_limited(retry_after=60.0), CredentialState.RATE_LIMITED),
    (Outcome.transient_error(retry_after=60.0), CredentialState.COOLDOWN),
    (Outcome.permanent_failure(), CredentialState.UNHEALTHY),
    (Outcome.quota_exhausted(retry_after=None), CredentialState.QUOTA_EXHAUSTED),
]


def _racing_pool(
    test_clock: TestClock,
    outcome: Outcome,
    credentials: Sequence[Credential],
) -> tuple[CredentialPool, MemoryStateStore, _HookedStrategy, list[CredentialRecord]]:
    """Pool whose first selection of ``a`` is raced by a concurrent report on ``a``."""
    store = MemoryStateStore(clock=test_clock)
    pool_holder: list[CredentialPool] = []
    victim: list[Lease] = []
    seen_after_race: list[CredentialRecord] = []

    def race(chosen: CredentialCandidate) -> None:
        if chosen.credential_id == "a" and not seen_after_race:
            pool_holder[0].report_sync(victim[0], outcome)
            seen_after_race.append(_record(store, "a"))

    strategy = _HookedStrategy(FailoverStrategy(["a", "b"]), race)
    pool = CredentialPool(credentials=credentials, strategy=strategy, store=store, clock=test_clock)
    pool_holder.append(pool)
    # An unrelated, already-running lease on "a" that will report mid-selection.
    victim.append(
        Lease(
            credential=credentials[0],
            lease_id="victim",
            acquired_at=test_clock.now(),
        )
    )
    store.reserve_lease("a", "victim", test_clock.now())
    return pool, store, strategy, seen_after_race


@pytest.mark.parametrize(("outcome", "expected"), _RACING_OUTCOMES)
def test_state_change_between_selection_and_reservation_is_rejected(
    test_clock: TestClock, outcome: Outcome, expected: CredentialState
) -> None:
    pool, store, strategy, seen = _racing_pool(test_clock, outcome, [_cred("a"), _cred("b")])

    lease = pool.acquire_sync()

    assert strategy.selections == ["a", "b"]  # "a" was selected, then re-selected away from
    assert lease.credential_id == "b"
    record_a = _record(store, "a")
    assert record_a.state == expected
    # Losing the race changed nothing about "a": it is exactly what the racing report left.
    assert record_a == seen[0]
    assert record_a.in_flight_leases == 0
    assert record_a.total_leases == 1
    assert _record(store, "b").in_flight_leases == 1
    assert_lease_accounting(store)


@pytest.mark.parametrize(("outcome", "expected"), _RACING_OUTCOMES)
def test_state_change_race_with_no_alternative_raises_and_keeps_health(
    test_clock: TestClock, outcome: Outcome, expected: CredentialState
) -> None:
    pool, store, strategy, seen = _racing_pool(test_clock, outcome, [_cred("a")])

    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync()

    assert strategy.selections == ["a"]
    record_a = _record(store, "a")
    assert record_a == seen[0]
    assert record_a.state == expected
    assert record_a.in_flight_leases == 0
    assert store.list_active_leases() == ()
    assert_lease_accounting(store)


@pytest.mark.asyncio
@pytest.mark.parametrize(("outcome", "expected"), _RACING_OUTCOMES)
async def test_state_change_between_selection_and_reservation_async(
    test_clock: TestClock, outcome: Outcome, expected: CredentialState
) -> None:
    pool, store, strategy, seen = _racing_pool(test_clock, outcome, [_cred("a"), _cred("b")])

    lease = await pool.acquire()

    assert strategy.selections == ["a", "b"]
    assert lease.credential_id == "b"
    assert _record(store, "a") == seen[0]
    assert _record(store, "a").state == expected
    assert_lease_accounting(store)


def test_disabled_between_selection_and_reservation_is_rejected(test_clock: TestClock) -> None:
    store = MemoryStateStore(clock=test_clock)

    def disable(chosen: CredentialCandidate) -> None:
        if chosen.credential_id == "a":
            store.update_state("a", CredentialState.DISABLED)

    strategy = _HookedStrategy(FailoverStrategy(["a", "b"]), disable)
    pool = CredentialPool(
        credentials=[_cred("a"), _cred("b")], strategy=strategy, store=store, clock=test_clock
    )

    assert pool.acquire_sync().credential_id == "b"
    assert _record(store, "a").state == CredentialState.DISABLED
    assert _record(store, "a").total_leases == 0
    assert_lease_accounting(store)


def test_reset_after_a_lost_race_makes_the_credential_selectable_again(
    test_clock: TestClock,
) -> None:
    store = MemoryStateStore(clock=test_clock)
    fired: list[bool] = []

    def revoke_once(chosen: CredentialCandidate) -> None:
        if chosen.credential_id == "a" and not fired:
            fired.append(True)
            store.update_state("a", CredentialState.REVOKED)

    strategy = _HookedStrategy(FailoverStrategy(["a", "b"]), revoke_once)
    pool = CredentialPool(
        credentials=[_cred("a"), _cred("b")], strategy=strategy, store=store, clock=test_clock
    )

    assert pool.acquire_sync().credential_id == "b"
    pool.reset_credential("a")
    assert pool.acquire_sync().credential_id == "a"
    assert_lease_accounting(store)


# --- pool: cooldown expiry between snapshot and reservation -------------------------------


def test_pool_recovers_cooldown_that_expires_after_the_snapshot(test_clock: TestClock) -> None:
    store = MemoryStateStore(clock=test_clock)
    strategy = _FirstCandidateStrategy(hook=lambda: test_clock.advance(11.0))
    pool = CredentialPool(
        credentials=[_cred("a")], strategy=strategy, store=store, clock=test_clock
    )
    (first,) = open_leases(store, "a", 1, test_clock.now())
    settle(store, "a", first, Outcome.transient_error(retry_after=10.0), test_clock.now())
    snapshot = _record(store, "a")
    assert snapshot.state == CredentialState.COOLDOWN

    # The snapshot sees COOLDOWN; the strategy then "waits" past the deadline before reserving.
    lease = pool.acquire_sync()

    assert lease.credential_id == "a"
    record = _record(store, "a")
    assert record.state == CredentialState.AVAILABLE
    assert record.cooldown_until is None
    assert record.consecutive_failures == 1
    assert record.in_flight_leases == 1
    assert_lease_accounting(store)


@pytest.mark.asyncio
async def test_pool_recovers_cooldown_that_expires_after_the_snapshot_async(
    test_clock: TestClock,
) -> None:
    store = MemoryStateStore(clock=test_clock)
    strategy = _FirstCandidateStrategy(hook=lambda: test_clock.advance(11.0))
    pool = CredentialPool(
        credentials=[_cred("a")], strategy=strategy, store=store, clock=test_clock
    )
    (first,) = open_leases(store, "a", 1, test_clock.now())
    settle(store, "a", first, Outcome.rate_limited(retry_after=10.0), test_clock.now())

    lease = await pool.acquire()

    assert lease.credential_id == "a"
    assert _record(store, "a").state == CredentialState.AVAILABLE


def test_pool_rejects_a_stale_choice_whose_cooldown_has_not_expired(
    test_clock: TestClock,
) -> None:
    store = MemoryStateStore(clock=test_clock)
    strategy = _FirstCandidateStrategy(hook=lambda: test_clock.advance(5.0))
    pool = CredentialPool(
        credentials=[_cred("a")], strategy=strategy, store=store, clock=test_clock
    )
    (first,) = open_leases(store, "a", 1, test_clock.now())
    settle(store, "a", first, Outcome.rate_limited(retry_after=10.0), test_clock.now())
    before = _record(store, "a")

    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync()

    assert _record(store, "a") == before
    assert before.state == CredentialState.RATE_LIMITED
    assert store.list_active_leases() == ()


# --- concurrent reset / revoke / reserve ---------------------------------------------------


def _expected_reserve_result(order: Sequence[str]) -> LeaseReservation:
    """Serial semantics: eligible unless revoked and not reset since."""
    revoked = False
    for step in order:
        if step == "revoke":
            revoked = True
        elif step == "reset":
            revoked = False
        else:
            return LeaseReservation.INELIGIBLE if revoked else LeaseReservation.RESERVED
    raise AssertionError("no reserve step")  # pragma: no cover


@pytest.mark.parametrize("order", list(itertools.permutations(["revoke", "reset", "reserve"])))
def test_every_serialization_of_reset_revoke_reserve_is_consistent(
    test_clock: TestClock, order: tuple[str, ...]
) -> None:
    store = MemoryStateStore(clock=test_clock)
    store.initialize_record("c1")
    now = test_clock.now()
    result: LeaseReservation | None = None

    for step in order:
        if step == "revoke":
            store.update_state("c1", CredentialState.REVOKED)
        elif step == "reset":
            store.reset("c1")
        else:
            result = store.reserve_lease("c1", "l1", now)

    assert result is _expected_reserve_result(order)
    assert_lease_accounting(store)


class _AuditedStore(MemoryStateStore):
    """Logs every state change and reservation, atomically with the operation itself."""

    def __init__(self, clock: TestClock) -> None:
        super().__init__(clock=clock)
        self.log: list[tuple[str, CredentialState | LeaseReservation]] = []

    def update_state(
        self,
        credential_id: str,
        state: CredentialState,
        *,
        cooldown_until: datetime | None = None,
    ) -> None:
        with self._lock:
            super().update_state(credential_id, state, cooldown_until=cooldown_until)
            self.log.append(("state", state))

    def reset(self, credential_id: str) -> None:
        with self._lock:
            super().reset(credential_id)
            self.log.append(("state", CredentialState.AVAILABLE))

    def reserve_lease(
        self,
        credential_id: str,
        lease_id: str,
        timestamp: datetime,
        *,
        max_concurrency: int | None = None,
        expires_at: datetime | None = None,
    ) -> LeaseReservation:
        with self._lock:
            result = super().reserve_lease(
                credential_id,
                lease_id,
                timestamp,
                max_concurrency=max_concurrency,
                expires_at=expires_at,
            )
            self.log.append(("reserve", result))
            return result


def test_concurrent_reset_revoke_reserve_is_linearizable(test_clock: TestClock) -> None:
    """Replaying the atomic log serially must explain every reservation result."""
    store = _AuditedStore(test_clock)
    store.initialize_record("c1")
    now = test_clock.now()
    workers = 12
    per_worker = 400
    barrier = threading.Barrier(workers)
    counter = itertools.count()
    reserved_ids: list[str] = []
    ids_lock = threading.Lock()

    def worker(seed: int) -> None:
        rng = random.Random(seed)
        barrier.wait()
        for _ in range(per_worker):
            action = rng.choice(("revoke", "reset", "reserve", "reserve"))
            if action == "revoke":
                store.update_state("c1", CredentialState.REVOKED)
            elif action == "reset":
                store.reset("c1")
            else:
                lease_id = f"l{next(counter)}"
                if store.reserve_lease("c1", lease_id, now):
                    with ids_lock:
                        reserved_ids.append(lease_id)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        for future in [executor.submit(worker, seed) for seed in range(workers)]:
            future.result()

    state = CredentialState.AVAILABLE
    reservations = 0
    for kind, value in store.log:
        if kind == "state":
            assert isinstance(value, CredentialState)
            state = value
        else:
            reservations += 1
            expected = (
                LeaseReservation.RESERVED
                if state == CredentialState.AVAILABLE
                else LeaseReservation.INELIGIBLE
            )
            assert value is expected
    assert reservations > 0
    assert len(reserved_ids) == len(store.list_active_leases())
    assert _record(store, "c1").total_leases == len(reserved_ids)
    assert_lease_accounting(store)

    # Once finally revoked nothing can be reserved any more.
    store.update_state("c1", CredentialState.REVOKED)
    assert store.reserve_lease("c1", "after", now) is LeaseReservation.INELIGIBLE


# --- multiple pools sharing one store ------------------------------------------------------


def test_report_through_another_pool_rejects_the_first_pools_reservation(
    test_clock: TestClock,
) -> None:
    store = MemoryStateStore(clock=test_clock)
    creds = [_cred("a"), _cred("b")]
    pool_b = CredentialPool(
        credentials=creds,
        strategy=FailoverStrategy(["a", "b"]),
        store=store,
        clock=test_clock,
    )
    running = pool_b.acquire_sync()
    assert running.credential_id == "a"
    fired: list[bool] = []

    def revoke_via_other_pool(chosen: CredentialCandidate) -> None:
        if chosen.credential_id == "a" and not fired:
            fired.append(True)
            pool_b.report_sync(running, Outcome.auth_failed())

    strategy = _HookedStrategy(FailoverStrategy(["a", "b"]), revoke_via_other_pool)
    pool_a = CredentialPool(credentials=creds, strategy=strategy, store=store, clock=test_clock)

    lease = pool_a.acquire_sync()

    assert strategy.selections == ["a", "b"]
    assert lease.credential_id == "b"
    assert _record(store, "a").state == CredentialState.REVOKED
    assert _record(store, "a").in_flight_leases == 0
    assert pool_a.in_flight_leases == pool_b.in_flight_leases == 1
    assert_lease_accounting(store)


def test_pools_sharing_a_store_never_exceed_capacity_under_contention() -> None:
    store = MemoryStateStore()
    creds = [_cred("a"), _cred("b"), _cred("c")]
    pools = [
        CredentialPool(credentials=creds, store=store, max_concurrency_per_credential=2)
        for _ in range(4)
    ]
    barrier = threading.Barrier(len(pools) * 2)

    def drain(pool: CredentialPool) -> list[Lease]:
        barrier.wait()
        leases: list[Lease] = []
        while True:
            try:
                leases.append(pool.acquire_sync())
            except NoCredentialsAvailableError:
                return leases

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(pools) * 2) as executor:
        futures = [executor.submit(drain, pool) for pool in pools for _ in range(2)]
        leases = [lease for future in futures for lease in future.result()]

    assert len(leases) == 6  # 3 credentials x cap 2, exactly: none lost, none over
    assert len({lease.lease_id for lease in leases}) == 6
    for cid in ("a", "b", "c"):
        assert _record(store, cid).in_flight_leases == 2
    assert_lease_accounting(store)


def test_pools_sharing_a_store_survive_an_acquire_report_reset_storm() -> None:
    store = MemoryStateStore(default_cooldown=0.001)
    creds = [_cred("a"), _cred("b"), _cred("c")]
    pools = [
        CredentialPool(credentials=creds, store=store, max_concurrency_per_credential=3)
        for _ in range(3)
    ]
    stop = threading.Event()
    outcomes = [
        Outcome.success(),
        Outcome.rate_limited(retry_after=0.001),
        Outcome.transient_error(retry_after=0.001),
        Outcome.auth_failed(),
    ]
    reserved = 0
    reserved_lock = threading.Lock()

    def worker(pool: CredentialPool, seed: int) -> None:
        nonlocal reserved
        rng = random.Random(seed)
        for _ in range(300):
            try:
                lease = pool.acquire_sync()
            except NoCredentialsAvailableError:
                continue
            with reserved_lock:
                reserved += 1
            settlement = store.settle_lease(
                lease.lease_id, lease.credential_id, rng.choice(outcomes), pool.clock.now()
            )
            assert settlement is LeaseSettlement.SETTLED

    def resetter() -> None:
        while not stop.is_set():
            for cred in creds:
                pools[0].reset_credential(cred.id)
            stop.wait(0.0005)

    reset_thread = threading.Thread(target=resetter)
    reset_thread.start()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(worker, pools[i % len(pools)], i) for i in range(6)]
            for future in futures:
                future.result()
    finally:
        stop.set()
        reset_thread.join()

    assert store.list_active_leases() == ()
    assert sum(r.in_flight_leases for r in store.list_records()) == 0
    assert sum(r.total_leases for r in store.list_records()) == reserved
    assert_lease_accounting(store)


# --- registry count always equals summed in_flight_leases ---------------------------------


def test_registry_count_equals_summed_in_flight_after_every_operation(
    test_clock: TestClock,
) -> None:
    rng = random.Random(20240607)
    store = MemoryStateStore(clock=test_clock)
    credentials = ["a", "b", "c"]
    states = [
        CredentialState.AVAILABLE,
        CredentialState.REVOKED,
        CredentialState.DISABLED,
        CredentialState.UNHEALTHY,
        CredentialState.RATE_LIMITED,
        CredentialState.COOLDOWN,
    ]
    outcomes = [
        Outcome.success(),
        Outcome.auth_failed(),
        Outcome.rate_limited(retry_after=5.0),
        Outcome.transient_error(retry_after=5.0),
        Outcome.permanent_failure(),
    ]
    live: list[tuple[str, str]] = []
    ids = itertools.count()

    for _ in range(3000):
        action = rng.choice(
            ("reserve", "reserve", "reserve", "settle", "settle", "reclaim", "state", "reset")
        )
        now = test_clock.now()
        cid = rng.choice(credentials)
        if action == "reserve":
            lease_id = f"l{next(ids)}"
            ttl = timedelta(seconds=rng.choice((3, 30)))
            result = store.reserve_lease(
                cid,
                lease_id,
                now,
                max_concurrency=rng.choice((None, 1, 2, 4)),
                expires_at=now + ttl if rng.random() < 0.5 else None,
            )
            if result is LeaseReservation.RESERVED:
                live.append((lease_id, cid))
        elif action == "settle" and live:
            lease_id, owner = live.pop(rng.randrange(len(live)))
            store.settle_lease(lease_id, owner, rng.choice(outcomes), now)
        elif action == "reclaim":
            store.reclaim_expired_leases(now)
            registered = {lease.lease_id for lease in store.list_active_leases()}
            live = [pair for pair in live if pair[0] in registered]
        elif action == "state":
            cooldown = now + timedelta(seconds=rng.choice((1, 10))) if rng.random() < 0.5 else None
            store.update_state(cid, rng.choice(states), cooldown_until=cooldown)
        elif action == "reset":
            store.reset(cid)
        if rng.random() < 0.3:
            test_clock.advance(rng.choice((0.5, 2.0, 8.0)))

        assert_lease_accounting(store)
