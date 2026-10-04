"""Unit tests for per-credential concurrency caps and lease reclamation in CredentialPool."""

from collections.abc import Callable, Sequence
from datetime import timedelta

import pytest

from credweave.application.ports.state_store import LeaseRecord, LeaseSettlement
from credweave.application.ports.strategy import (
    CredentialCandidate,
    SelectionContext,
    SelectionStrategy,
)
from credweave.application.services.pool import CredentialPool
from credweave.domain.enums import CredentialState
from credweave.domain.errors import (
    ConfigurationError,
    InvalidLeaseError,
    LeaseExpiredError,
    NoCredentialsAvailableError,
    StateStoreError,
)
from credweave.domain.models import Credential, Lease
from credweave.domain.outcomes import Outcome
from credweave.infrastructure.sources.static import StaticSource
from credweave.infrastructure.stores.memory import MemoryStateStore
from credweave.strategies.failover import FailoverStrategy
from credweave.strategies.round_robin import RoundRobinStrategy
from tests.conftest import TestClock


def _cred(cid: str, **metadata: object) -> Credential:
    return Credential(id=cid, secrets={"k": f"secret-{cid}"}, metadata=metadata)


def _in_flight(pool: CredentialPool, cid: str) -> int:
    record = pool.get_record(cid)
    assert record is not None
    return record.in_flight_leases


# --- configuration -------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, True, False, "3", 1.5, [2]])
def test_pool_level_cap_rejects_invalid_values(bad: object) -> None:
    with pytest.raises(ConfigurationError):
        CredentialPool(
            credentials=[_cred("a")],
            max_concurrency_per_credential=bad,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("bad", [0, -2, True, "3", 2.0])
def test_credential_metadata_cap_rejected_at_construction(bad: object) -> None:
    with pytest.raises(ConfigurationError, match="max_concurrency"):
        CredentialPool(credentials=[_cred("a", max_concurrency=bad)])


@pytest.mark.parametrize("bad", [0, -2, True, "3"])
def test_invalid_metadata_cap_from_source_rejected_at_acquire_sync(bad: object) -> None:
    pool = CredentialPool(source=StaticSource([_cred("a", max_concurrency=bad)]))
    with pytest.raises(ConfigurationError):
        pool.acquire_sync()


@pytest.mark.asyncio
async def test_invalid_metadata_cap_from_source_rejected_at_acquire_async() -> None:
    pool = CredentialPool(source=StaticSource([_cred("a", max_concurrency=0)]))
    with pytest.raises(ConfigurationError):
        await pool.acquire()


@pytest.mark.parametrize("bad", [0, -1.0, True, "5", float("nan"), float("inf"), 1e30])
def test_lease_timeout_rejects_invalid_values(bad: object) -> None:
    with pytest.raises(ConfigurationError):
        CredentialPool(credentials=[_cred("a")], lease_timeout=bad)  # type: ignore[arg-type]


# --- enforcement ---------------------------------------------------------------------------


def test_default_is_unlimited() -> None:
    pool = CredentialPool(credentials=[_cred("a")])
    leases = [pool.acquire_sync() for _ in range(50)]
    assert len(leases) == 50
    assert _in_flight(pool, "a") == 50


def test_cap_one_blocks_second_lease_until_report() -> None:
    pool = CredentialPool(credentials=[_cred("a")], max_concurrency_per_credential=1)
    lease = pool.acquire_sync()

    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync()

    pool.report_sync(lease, Outcome.success())
    assert pool.acquire_sync().credential_id == "a"


def test_cap_greater_than_one() -> None:
    pool = CredentialPool(credentials=[_cred("a")], max_concurrency_per_credential=3)
    leases = [pool.acquire_sync() for _ in range(3)]
    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync()
    pool.report_sync(leases[0], Outcome.success())
    pool.acquire_sync()
    assert _in_flight(pool, "a") == 3


def test_metadata_override_beats_pool_default() -> None:
    pool = CredentialPool(
        credentials=[_cred("tight", max_concurrency=1), _cred("loose", max_concurrency=3)],
        max_concurrency_per_credential=2,
        strategy=FailoverStrategy(["tight", "loose"]),
    )
    got = [pool.acquire_sync().credential_id for _ in range(4)]
    assert got == ["tight", "loose", "loose", "loose"]
    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync()


def test_metadata_none_overrides_pool_default_to_unlimited() -> None:
    pool = CredentialPool(
        credentials=[_cred("free", max_concurrency=None)],
        max_concurrency_per_credential=1,
    )
    for _ in range(10):
        pool.acquire_sync()
    assert _in_flight(pool, "free") == 10


def test_pool_default_applies_to_credentials_without_override() -> None:
    pool = CredentialPool(
        credentials=[_cred("plain"), _cred("big", max_concurrency=4)],
        max_concurrency_per_credential=2,
        strategy=FailoverStrategy(["plain", "big"]),
    )
    got = [pool.acquire_sync().credential_id for _ in range(6)]
    assert got == ["plain", "plain", "big", "big", "big", "big"]


def test_different_caps_per_credential_with_unlimited_default() -> None:
    pool = CredentialPool(
        credentials=[_cred("a", max_concurrency=1), _cred("b", max_concurrency=2), _cred("c")],
        strategy=FailoverStrategy(["a", "b", "c"]),
    )
    got = [pool.acquire_sync().credential_id for _ in range(6)]
    assert got == ["a", "b", "b", "c", "c", "c"]


def test_round_robin_skips_saturated_credential() -> None:
    pool = CredentialPool(
        credentials=[_cred("a", max_concurrency=1), _cred("b"), _cred("c")],
        strategy=RoundRobinStrategy(),
    )
    got = [pool.acquire_sync().credential_id for _ in range(7)]
    assert got.count("a") == 1
    assert set(got) == {"a", "b", "c"}
    assert got[:3] == ["a", "b", "c"]
    assert got[3:] == ["b", "c", "b", "c"]


def test_failover_falls_through_when_primary_saturated_then_returns() -> None:
    pool = CredentialPool(
        credentials=[_cred("primary", max_concurrency=1), _cred("backup")],
        strategy=FailoverStrategy(["primary", "backup"]),
    )
    first = pool.acquire_sync()
    second = pool.acquire_sync()
    assert (first.credential_id, second.credential_id) == ("primary", "backup")

    pool.report_sync(first, Outcome.success())
    assert pool.acquire_sync().credential_id == "primary"


def test_saturation_does_not_change_health_state() -> None:
    pool = CredentialPool(credentials=[_cred("a")], max_concurrency_per_credential=1)
    pool.acquire_sync()
    for _ in range(5):
        with pytest.raises(NoCredentialsAvailableError):
            pool.acquire_sync()

    record = pool.get_record("a")
    assert record is not None
    assert record.state == CredentialState.AVAILABLE
    assert record.consecutive_failures == 0
    assert record.cooldown_until is None
    assert record.total_leases == 1


def test_required_tags_still_apply_with_caps() -> None:
    pool = CredentialPool(
        credentials=[_cred("a", tags=("x",), max_concurrency=1), _cred("b", tags=("y",))],
    )
    pool.acquire_sync(SelectionContext(required_tags=frozenset({"x"})))
    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync(SelectionContext(required_tags=frozenset({"x"})))
    assert pool.acquire_sync(SelectionContext(required_tags=frozenset({"y"}))).credential_id == "b"


@pytest.mark.asyncio
async def test_async_cap_enforced_and_released() -> None:
    pool = CredentialPool(
        credentials=[_cred("a", max_concurrency=2), _cred("b")],
        strategy=FailoverStrategy(["a", "b"]),
    )
    leases = [await pool.acquire() for _ in range(3)]
    assert [lease.credential_id for lease in leases] == ["a", "a", "b"]
    await pool.report(leases[0], Outcome.success())
    assert (await pool.acquire()).credential_id == "a"


# --- reservation races ---------------------------------------------------------------------


class _StealingStrategy(SelectionStrategy):
    """Delegates to an inner strategy but lets a hook run between selection and reservation."""

    def __init__(
        self, inner: SelectionStrategy, hook: Callable[[CredentialCandidate], None]
    ) -> None:
        self._inner = inner
        self._hook = hook
        self.selections: list[str] = []

    @property
    def name(self) -> str:
        return "stealing"

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


def test_lost_reservation_race_selects_another_credential(test_clock: TestClock) -> None:
    store = MemoryStateStore(clock=test_clock)
    stolen: list[str] = []

    def steal_once(chosen: CredentialCandidate) -> None:
        if not stolen:
            stolen.append(chosen.credential_id)
            assert store.reserve_lease(chosen.credential_id, "thief", test_clock.now())

    strategy = _StealingStrategy(FailoverStrategy(["a", "b"]), steal_once)
    pool = CredentialPool(
        credentials=[_cred("a", max_concurrency=1), _cred("b")],
        strategy=strategy,
        store=store,
        clock=test_clock,
    )

    lease = pool.acquire_sync()

    assert stolen == ["a"]
    assert strategy.selections == ["a", "b"]
    assert lease.credential_id == "b"
    assert _in_flight(pool, "a") == 1  # only the thief; cap never exceeded


def test_lost_reservation_race_with_no_alternative_raises(test_clock: TestClock) -> None:
    store = MemoryStateStore(clock=test_clock)

    def steal(chosen: CredentialCandidate) -> None:
        store.reserve_lease(
            chosen.credential_id, f"thief-{len(store.list_active_leases())}", test_clock.now()
        )

    pool = CredentialPool(
        credentials=[_cred("a", max_concurrency=1), _cred("b", max_concurrency=1)],
        strategy=_StealingStrategy(FailoverStrategy(["a", "b"]), steal),
        store=store,
        clock=test_clock,
    )

    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync()
    assert _in_flight(pool, "a") == 1
    assert _in_flight(pool, "b") == 1


@pytest.mark.asyncio
async def test_lost_reservation_race_async(test_clock: TestClock) -> None:
    store = MemoryStateStore(clock=test_clock)
    stolen: list[str] = []

    def steal_once(chosen: CredentialCandidate) -> None:
        if not stolen:
            stolen.append(chosen.credential_id)
            store.reserve_lease(chosen.credential_id, "thief", test_clock.now())

    pool = CredentialPool(
        credentials=[_cred("a", max_concurrency=1), _cred("b")],
        strategy=_StealingStrategy(FailoverStrategy(["a", "b"]), steal_once),
        store=store,
        clock=test_clock,
    )

    assert (await pool.acquire()).credential_id == "b"
    assert _in_flight(pool, "a") == 1


# --- lease timeout: automatic reclamation --------------------------------------------------


def test_timeout_automatically_frees_capacity_on_acquire(test_clock: TestClock) -> None:
    pool = CredentialPool(
        credentials=[_cred("a")],
        clock=test_clock,
        lease_timeout=10.0,
        max_concurrency_per_credential=1,
    )
    orphan = pool.acquire_sync()
    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync()

    test_clock.advance(11.0)
    fresh = pool.acquire_sync()  # orphan was never reported

    assert fresh.lease_id != orphan.lease_id
    assert _in_flight(pool, "a") == 1
    assert [lease.lease_id for lease in pool.active_leases] == [fresh.lease_id]


@pytest.mark.asyncio
async def test_timeout_automatically_frees_capacity_on_acquire_async(
    test_clock: TestClock,
) -> None:
    pool = CredentialPool(
        credentials=[_cred("a")],
        clock=test_clock,
        lease_timeout=10.0,
        max_concurrency_per_credential=1,
    )
    await pool.acquire()
    test_clock.advance(11.0)
    await pool.acquire()
    assert _in_flight(pool, "a") == 1


def test_lease_not_reclaimed_before_deadline(test_clock: TestClock) -> None:
    pool = CredentialPool(
        credentials=[_cred("a")],
        clock=test_clock,
        lease_timeout=10.0,
        max_concurrency_per_credential=1,
    )
    pool.acquire_sync()
    test_clock.advance(10.0)  # exactly at the deadline: still valid
    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync()


def test_report_after_automatic_reclamation_raises_expired_and_releases_nothing(
    test_clock: TestClock,
) -> None:
    pool = CredentialPool(
        credentials=[_cred("a")],
        clock=test_clock,
        lease_timeout=10.0,
        max_concurrency_per_credential=1,
    )
    orphan = pool.acquire_sync()
    test_clock.advance(11.0)
    fresh = pool.acquire_sync()  # reclaims the orphan

    with pytest.raises(LeaseExpiredError):
        pool.report_sync(orphan, Outcome.auth_failed(reason="late"))

    # Neither double release nor a fake outcome: the fresh lease still holds its slot.
    record = pool.get_record("a")
    assert record is not None
    assert record.in_flight_leases == 1
    assert record.state == CredentialState.AVAILABLE
    pool.report_sync(fresh, Outcome.success())
    assert _in_flight(pool, "a") == 0


def test_report_expired_lease_twice_never_double_releases(test_clock: TestClock) -> None:
    pool = CredentialPool(credentials=[_cred("a")], clock=test_clock, lease_timeout=5.0)
    keep = pool.acquire_sync()
    late = pool.acquire_sync()
    test_clock.advance(6.0)
    for _ in range(3):
        with pytest.raises(LeaseExpiredError):
            pool.report_sync(late, Outcome.success())
    assert _in_flight(pool, "a") == 0
    with pytest.raises(LeaseExpiredError):
        pool.report_sync(keep, Outcome.success())
    assert _in_flight(pool, "a") == 0


def test_report_path_reclaims_other_expired_leases(test_clock: TestClock) -> None:
    pool = CredentialPool(
        credentials=[_cred("a", max_concurrency=1), _cred("b", max_concurrency=1)],
        strategy=FailoverStrategy(["a", "b"]),
        clock=test_clock,
        lease_timeout=10.0,
    )
    orphan_a = pool.acquire_sync()
    test_clock.advance(8.0)
    lease_b = pool.acquire_sync()
    test_clock.advance(3.0)  # orphan_a expired (11s), lease_b still fresh (3s)

    pool.report_sync(lease_b, Outcome.success())

    assert _in_flight(pool, "a") == 0  # freed as a side effect of the report
    with pytest.raises(LeaseExpiredError):
        pool.report_sync(orphan_a, Outcome.success())


@pytest.mark.asyncio
async def test_async_report_path_reclaims_other_expired_leases(test_clock: TestClock) -> None:
    pool = CredentialPool(
        credentials=[_cred("a", max_concurrency=1), _cred("b", max_concurrency=1)],
        strategy=FailoverStrategy(["a", "b"]),
        clock=test_clock,
        lease_timeout=10.0,
    )
    await pool.acquire()
    test_clock.advance(8.0)
    lease_b = await pool.acquire()
    test_clock.advance(3.0)
    await pool.report(lease_b, Outcome.success())
    assert _in_flight(pool, "a") == 0


def test_reclaim_preserves_credential_health(test_clock: TestClock) -> None:
    pool = CredentialPool(credentials=[_cred("a")], clock=test_clock, lease_timeout=10.0)
    orphan = pool.acquire_sync()
    failing = pool.acquire_sync()
    pool.report_sync(failing, Outcome.transient_error(retry_after=100.0))
    before = pool.get_record("a")
    assert before is not None
    assert before.state == CredentialState.COOLDOWN

    test_clock.advance(11.0)
    reclaimed = pool.reclaim_expired_leases()

    assert [lease.lease_id for lease in reclaimed] == [orphan.lease_id]
    after = pool.get_record("a")
    assert after is not None
    assert after.in_flight_leases == 0
    assert (after.state, after.consecutive_failures, after.cooldown_until) == (
        before.state,
        before.consecutive_failures,
        before.cooldown_until,
    )


# --- explicit reclamation ------------------------------------------------------------------


def test_explicit_reclaim_returns_lease_records_and_is_idempotent(
    test_clock: TestClock,
) -> None:
    pool = CredentialPool(credentials=[_cred("a"), _cred("b")], clock=test_clock, lease_timeout=5.0)
    l1 = pool.acquire_sync()
    l2 = pool.acquire_sync()
    test_clock.advance(6.0)
    survivor = pool.acquire_sync()  # auto-reclaims l1/l2 as well

    assert pool.in_flight_leases == 1

    # Nothing left to reclaim now.
    assert pool.reclaim_expired_leases() == ()
    assert survivor.lease_id in {lease.lease_id for lease in pool.active_leases}
    assert l1.lease_id not in {lease.lease_id for lease in pool.active_leases}
    assert l2.lease_id not in {lease.lease_id for lease in pool.active_leases}


def test_explicit_reclaim_without_acquire_reports_details(test_clock: TestClock) -> None:
    pool = CredentialPool(credentials=[_cred("a"), _cred("b")], clock=test_clock, lease_timeout=5.0)
    l1 = pool.acquire_sync()
    l2 = pool.acquire_sync()
    test_clock.advance(5.5)

    reclaimed = pool.reclaim_expired_leases()

    assert isinstance(reclaimed, tuple)
    assert all(isinstance(r, LeaseRecord) for r in reclaimed)
    assert {r.lease_id for r in reclaimed} == {l1.lease_id, l2.lease_id}
    assert {r.credential_id for r in reclaimed} == {"a", "b"}
    assert pool.in_flight_leases == 0
    assert pool.reclaim_expired_leases() == ()
    assert _in_flight(pool, "a") == 0
    assert _in_flight(pool, "b") == 0


@pytest.mark.asyncio
async def test_explicit_reclaim_async(test_clock: TestClock) -> None:
    pool = CredentialPool(credentials=[_cred("a")], clock=test_clock, lease_timeout=5.0)
    lease = await pool.acquire()
    assert await pool.reclaim_expired_leases_async() == ()
    test_clock.advance(6.0)

    reclaimed = await pool.reclaim_expired_leases_async()

    assert [r.lease_id for r in reclaimed] == [lease.lease_id]
    assert await pool.reclaim_expired_leases_async() == ()
    with pytest.raises(LeaseExpiredError):
        await pool.report(lease, Outcome.success())
    assert _in_flight(pool, "a") == 0


def test_reclaim_without_timeout_never_reclaims(test_clock: TestClock) -> None:
    pool = CredentialPool(credentials=[_cred("a")], clock=test_clock)
    pool.acquire_sync()
    test_clock.advance(10**7)
    assert pool.reclaim_expired_leases() == ()
    assert pool.in_flight_leases == 1


def test_reclaimed_lease_is_invalid_for_report_but_fresh_leases_unaffected(
    test_clock: TestClock,
) -> None:
    pool = CredentialPool(credentials=[_cred("a")], clock=test_clock, lease_timeout=5.0)
    old = pool.acquire_sync()
    test_clock.advance(6.0)
    pool.reclaim_expired_leases()
    fresh = pool.acquire_sync()

    with pytest.raises(LeaseExpiredError):
        pool.report_sync(old, Outcome.success())
    pool.report_sync(fresh, Outcome.success())
    assert _in_flight(pool, "a") == 0


# --- two pools, one store ------------------------------------------------------------------


def test_pools_sharing_a_store_share_caps(test_clock: TestClock) -> None:
    store = MemoryStateStore(clock=test_clock)
    creds = [_cred("a", max_concurrency=1)]
    pool_a = CredentialPool(credentials=creds, store=store, clock=test_clock)
    pool_b = CredentialPool(credentials=creds, store=store, clock=test_clock)

    held = pool_a.acquire_sync()
    with pytest.raises(NoCredentialsAvailableError):
        pool_b.acquire_sync()

    pool_a.report_sync(held, Outcome.success())
    assert pool_b.acquire_sync().credential_id == "a"


def test_other_pool_reclaims_expired_lease_and_late_report_is_rejected(
    test_clock: TestClock,
) -> None:
    store = MemoryStateStore(clock=test_clock)
    creds = [_cred("a", max_concurrency=1)]
    pool_a = CredentialPool(credentials=creds, store=store, clock=test_clock, lease_timeout=10.0)
    pool_b = CredentialPool(credentials=creds, store=store, clock=test_clock, lease_timeout=10.0)
    orphan = pool_a.acquire_sync()
    test_clock.advance(11.0)

    taken = pool_b.acquire_sync()  # B reclaims A's orphan

    with pytest.raises(LeaseExpiredError):
        pool_a.report_sync(orphan, Outcome.success())
    assert _in_flight(pool_b, "a") == 1
    pool_b.report_sync(taken, Outcome.success())
    assert _in_flight(pool_a, "a") == 0


def test_pool_without_timeout_honours_deadlines_of_a_shared_store(
    test_clock: TestClock,
) -> None:
    store = MemoryStateStore(clock=test_clock)
    creds = [_cred("a", max_concurrency=1)]
    pool_a = CredentialPool(credentials=creds, store=store, clock=test_clock, lease_timeout=10.0)
    pool_b = CredentialPool(credentials=creds, store=store, clock=test_clock)
    pool_a.acquire_sync()
    test_clock.advance(11.0)
    assert pool_b.acquire_sync().credential_id == "a"


# --- failure safety ------------------------------------------------------------------------


def test_settle_failure_keeps_lease_active_and_retry_releases_once(
    test_clock: TestClock,
) -> None:
    store = MemoryStateStore(clock=test_clock)
    pool = CredentialPool(
        credentials=[_cred("a")],
        store=store,
        clock=test_clock,
        max_concurrency_per_credential=1,
    )
    lease = pool.acquire_sync()
    real_settle = store.settle_lease
    calls = 0

    def flaky(*args: object, **kwargs: object) -> LeaseSettlement:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise StateStoreError("transient")
        return real_settle(*args, **kwargs)  # type: ignore[arg-type]

    store.settle_lease = flaky  # type: ignore[method-assign]

    with pytest.raises(StateStoreError):
        pool.report_sync(lease, Outcome.success())
    assert pool.in_flight_leases == 1
    assert _in_flight(pool, "a") == 1
    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync()  # slot is still held: not leaked, not released early

    pool.report_sync(lease, Outcome.success())
    assert _in_flight(pool, "a") == 0
    with pytest.raises(InvalidLeaseError):
        pool.report_sync(lease, Outcome.success())  # no double release
    assert _in_flight(pool, "a") == 0


@pytest.mark.asyncio
async def test_settle_failure_async_keeps_lease_active_and_retry_releases_once(
    test_clock: TestClock,
) -> None:
    store = MemoryStateStore(clock=test_clock)
    pool = CredentialPool(
        credentials=[_cred("a")],
        store=store,
        clock=test_clock,
        max_concurrency_per_credential=1,
    )
    lease = await pool.acquire()
    real_settle = store.settle_lease_async
    calls = 0

    async def flaky(*args: object, **kwargs: object) -> LeaseSettlement:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise StateStoreError("transient")
        return await real_settle(*args, **kwargs)  # type: ignore[arg-type]

    store.settle_lease_async = flaky  # type: ignore[method-assign]

    with pytest.raises(StateStoreError):
        await pool.report(lease, Outcome.success())
    assert _in_flight(pool, "a") == 1

    await pool.report(lease, Outcome.success())
    assert _in_flight(pool, "a") == 0
    with pytest.raises(InvalidLeaseError):
        await pool.report(lease, Outcome.success())


def test_expired_report_settle_failure_does_not_release_slot_twice(
    test_clock: TestClock,
) -> None:
    store = MemoryStateStore(clock=test_clock)
    pool = CredentialPool(
        credentials=[_cred("a")],
        store=store,
        clock=test_clock,
        lease_timeout=5.0,
        max_concurrency_per_credential=1,
    )
    lease = pool.acquire_sync()
    test_clock.advance(10.0)

    real_reclaim = store.reclaim_expired_leases
    fail = {"on": True}

    def flaky_reclaim(now: object) -> Sequence[LeaseRecord]:
        if fail["on"]:
            raise StateStoreError("reclaim failed")
        return real_reclaim(now)  # type: ignore[arg-type]

    store.reclaim_expired_leases = flaky_reclaim  # type: ignore[method-assign]

    with pytest.raises(StateStoreError):
        pool.report_sync(lease, Outcome.success())
    assert _in_flight(pool, "a") == 1  # still held; nothing released or leaked

    fail["on"] = False
    with pytest.raises(LeaseExpiredError):
        pool.report_sync(lease, Outcome.success())
    assert _in_flight(pool, "a") == 0


def test_failed_reservation_consumes_no_slot(test_clock: TestClock) -> None:
    store = MemoryStateStore(clock=test_clock)
    pool = CredentialPool(
        credentials=[_cred("a")],
        store=store,
        clock=test_clock,
        max_concurrency_per_credential=1,
    )

    def broken(*args: object, **kwargs: object) -> bool:
        raise StateStoreError("down")

    real = store.reserve_lease
    store.reserve_lease = broken  # type: ignore[method-assign]
    with pytest.raises(StateStoreError):
        pool.acquire_sync()
    store.reserve_lease = real  # type: ignore[method-assign]

    assert _in_flight(pool, "a") == 0
    assert pool.acquire_sync().credential_id == "a"


# --- lease views ---------------------------------------------------------------------------


def test_active_leases_view_matches_store_registry(test_clock: TestClock) -> None:
    pool = CredentialPool(credentials=[_cred("a"), _cred("b")], clock=test_clock)
    l1 = pool.acquire_sync()
    l2 = pool.acquire_sync()

    assert sorted(lease.lease_id for lease in pool.active_leases) == sorted(
        [l1.lease_id, l2.lease_id]
    )
    assert isinstance(pool.active_leases[0], Lease)
    pool.report_sync(l1, Outcome.success())
    assert pool.active_leases == (l2,)
    assert pool.in_flight_leases == 1


def test_lease_timeout_none_and_expiry_deadline_recorded(test_clock: TestClock) -> None:
    pool = CredentialPool(credentials=[_cred("a")], clock=test_clock, lease_timeout=30.0)
    pool.acquire_sync()
    (record,) = pool.store.list_active_leases()
    assert record.expires_at == test_clock.now() + timedelta(seconds=30)

    no_timeout = CredentialPool(credentials=[_cred("a")], clock=test_clock)
    no_timeout.acquire_sync()
    (record2,) = no_timeout.store.list_active_leases()
    assert record2.expires_at is None


def test_active_lease_view_survives_credential_missing_from_source(
    test_clock: TestClock,
) -> None:
    pool = CredentialPool(credentials=[_cred("a")], clock=test_clock)
    lease = pool.acquire_sync()
    other = CredentialPool(source=StaticSource([]), store=pool.store, clock=test_clock)

    (view,) = other.active_leases

    assert view.lease_id == lease.lease_id
    assert view.credential_id == "a"
    assert view.credential.secret_keys == frozenset()  # no secrets are ever reconstructed
