"""Stress and regression tests for per-credential concurrency caps and lease reclamation.

Every scenario asserts the same global invariants once the dust settles:

* a credential never holds more leases than its cap (checked live by a ``_Gauge``),
* no ``in_flight_leases`` counter is ever negative,
* the per-credential counters always add up to the lease registry (no leak, no double release).
"""

import asyncio
import concurrent.futures
import contextlib
import random
import threading
import time
from collections import Counter

import pytest

from credweave import (
    Credential,
    CredentialPool,
    FailoverStrategy,
    InvalidLeaseError,
    LeaseExpiredError,
    MemoryStateStore,
    NoCredentialsAvailableError,
    Outcome,
    RoundRobinStrategy,
)
from credweave.domain.models import Lease
from tests.conftest import TestClock


def _cred(cid: str, **metadata: object) -> Credential:
    return Credential(id=cid, secrets={"k": f"secret-{cid}"}, metadata=metadata)


class _Gauge:
    """Thread-safe live gauge of how many leases each credential holds right now."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current: Counter[str] = Counter()
        self.peak: Counter[str] = Counter()
        self.completed: Counter[str] = Counter()

    def enter(self, cid: str) -> None:
        with self._lock:
            self.current[cid] += 1
            self.peak[cid] = max(self.peak[cid], self.current[cid])

    def leave(self, cid: str) -> None:
        with self._lock:
            self.current[cid] -= 1
            self.completed[cid] += 1


def _assert_invariants(store: MemoryStateStore) -> None:
    records = store.list_records()
    assert all(r.in_flight_leases >= 0 for r in records)
    assert sum(r.in_flight_leases for r in records) == len(store.list_active_leases())


def _acquire_with_retry(pool: CredentialPool, deadline: float) -> Lease:
    while True:
        try:
            return pool.acquire_sync()
        except NoCredentialsAvailableError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0)


async def _acquire_with_retry_async(pool: CredentialPool, deadline: float) -> Lease:
    while True:
        try:
            return await pool.acquire()
        except NoCredentialsAvailableError:
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(0)


def _sync_worker(pool: CredentialPool, gauge: _Gauge, iterations: int) -> None:
    deadline = time.monotonic() + 60
    for _ in range(iterations):
        lease = _acquire_with_retry(pool, deadline)
        gauge.enter(lease.credential_id)
        time.sleep(0.0005)
        gauge.leave(lease.credential_id)
        pool.report_sync(lease, Outcome.success())


async def _async_worker(pool: CredentialPool, gauge: _Gauge, iterations: int) -> None:
    deadline = time.monotonic() + 60
    for _ in range(iterations):
        lease = await _acquire_with_retry_async(pool, deadline)
        gauge.enter(lease.credential_id)
        await asyncio.sleep(0.0005)
        gauge.leave(lease.credential_id)
        await pool.report(lease, Outcome.success())


# --- threads -------------------------------------------------------------------------------


@pytest.mark.parametrize("cap", [1, 2, 5])
def test_many_threads_never_exceed_cap(cap: int) -> None:
    store = MemoryStateStore()
    pool = CredentialPool(credentials=[_cred("a")], store=store, max_concurrency_per_credential=cap)
    gauge = _Gauge()

    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as executor:
        for f in [executor.submit(_sync_worker, pool, gauge, 25) for _ in range(24)]:
            f.result()

    assert gauge.peak["a"] <= cap
    assert gauge.completed["a"] == 24 * 25
    assert pool.in_flight_leases == 0
    _assert_invariants(store)


def test_many_threads_with_different_caps_per_credential() -> None:
    store = MemoryStateStore()
    caps = {"a": 1, "b": 2, "c": 4}
    pool = CredentialPool(
        credentials=[_cred(cid, max_concurrency=cap) for cid, cap in caps.items()],
        store=store,
        strategy=RoundRobinStrategy(),
    )
    gauge = _Gauge()

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        for f in [executor.submit(_sync_worker, pool, gauge, 30) for _ in range(20)]:
            f.result()

    for cid, cap in caps.items():
        assert gauge.peak[cid] <= cap, cid
    assert sum(gauge.completed.values()) == 20 * 30
    assert all(gauge.completed[cid] > 0 for cid in caps)
    _assert_invariants(store)


def test_threads_failover_spills_to_backup_only_when_primary_saturated() -> None:
    store = MemoryStateStore()
    pool = CredentialPool(
        credentials=[_cred("primary", max_concurrency=2), _cred("backup")],
        store=store,
        strategy=FailoverStrategy(["primary", "backup"]),
    )
    gauge = _Gauge()

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        for f in [executor.submit(_sync_worker, pool, gauge, 25) for _ in range(16)]:
            f.result()

    assert gauge.peak["primary"] <= 2
    assert gauge.completed["primary"] > 0
    assert gauge.completed["backup"] > 0
    _assert_invariants(store)


def test_unlimited_default_allows_full_parallelism() -> None:
    pool = CredentialPool(credentials=[_cred("a")])
    barrier = threading.Barrier(12)
    held: list[Lease] = []
    held_lock = threading.Lock()

    def worker() -> None:
        lease = pool.acquire_sync()
        with held_lock:
            held.append(lease)
        barrier.wait(timeout=10)  # every thread holds a lease at the same time

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        for f in [executor.submit(worker) for _ in range(12)]:
            f.result()

    assert pool.in_flight_leases == 12
    for lease in held:
        pool.report_sync(lease, Outcome.success())
    assert pool.in_flight_leases == 0


def test_simultaneous_acquire_of_last_slot_has_exactly_one_winner() -> None:
    store = MemoryStateStore()
    pool = CredentialPool(
        credentials=[_cred("a", max_concurrency=1), _cred("b", max_concurrency=1)],
        store=store,
    )
    barrier = threading.Barrier(16)

    def attempt() -> str | None:
        barrier.wait(timeout=10)
        try:
            return pool.acquire_sync().credential_id
        except NoCredentialsAvailableError:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        results = [f.result() for f in [executor.submit(attempt) for _ in range(16)]]

    winners = [r for r in results if r is not None]
    assert sorted(winners) == ["a", "b"]
    _assert_invariants(store)


# --- asyncio -------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [1, 3])
async def test_many_asyncio_tasks_never_exceed_cap(cap: int) -> None:
    store = MemoryStateStore()
    pool = CredentialPool(credentials=[_cred("a")], store=store, max_concurrency_per_credential=cap)
    gauge = _Gauge()

    await asyncio.gather(*[_async_worker(pool, gauge, 10) for _ in range(60)])

    assert gauge.peak["a"] <= cap
    assert gauge.completed["a"] == 600
    assert pool.in_flight_leases == 0
    _assert_invariants(store)


@pytest.mark.asyncio
async def test_asyncio_different_caps_per_credential() -> None:
    store = MemoryStateStore()
    caps = {"a": 1, "b": 3}
    pool = CredentialPool(
        credentials=[_cred(cid, max_concurrency=cap) for cid, cap in caps.items()],
        store=store,
    )
    gauge = _Gauge()

    await asyncio.gather(*[_async_worker(pool, gauge, 10) for _ in range(40)])

    assert gauge.peak["a"] <= 1
    assert gauge.peak["b"] <= 3
    assert sum(gauge.completed.values()) == 400
    _assert_invariants(store)


# --- mixed sync + async, and shared stores -------------------------------------------------


@pytest.mark.asyncio
async def test_mixed_sync_and_async_access_respects_cap() -> None:
    store = MemoryStateStore()
    pool = CredentialPool(
        credentials=[_cred("a", max_concurrency=2), _cred("b", max_concurrency=3)],
        store=store,
    )
    gauge = _Gauge()

    async def run_thread_workers() -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            loop = asyncio.get_running_loop()
            await asyncio.gather(
                *[loop.run_in_executor(executor, _sync_worker, pool, gauge, 10) for _ in range(6)]
            )

    await asyncio.gather(
        run_thread_workers(),
        *[_async_worker(pool, gauge, 6) for _ in range(15)],
    )

    assert gauge.peak["a"] <= 2
    assert gauge.peak["b"] <= 3
    assert sum(gauge.completed.values()) == 6 * 10 + 15 * 6
    assert pool.in_flight_leases == 0
    _assert_invariants(store)


@pytest.mark.asyncio
async def test_two_pools_sharing_a_store_share_one_cap() -> None:
    store = MemoryStateStore()
    creds = [_cred("a", max_concurrency=3)]
    pool_one = CredentialPool(credentials=creds, store=store)
    pool_two = CredentialPool(credentials=creds, store=store)
    gauge = _Gauge()

    async def run_thread_workers(pool: CredentialPool) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            loop = asyncio.get_running_loop()
            await asyncio.gather(
                *[loop.run_in_executor(executor, _sync_worker, pool, gauge, 10) for _ in range(4)]
            )

    await asyncio.gather(
        run_thread_workers(pool_one),
        run_thread_workers(pool_two),
        *[_async_worker(pool_one, gauge, 5) for _ in range(8)],
        *[_async_worker(pool_two, gauge, 5) for _ in range(8)],
    )

    assert gauge.peak["a"] <= 3
    assert gauge.completed["a"] == 2 * 4 * 10 + 2 * 8 * 5
    assert pool_one.in_flight_leases == pool_two.in_flight_leases == 0
    _assert_invariants(store)


def test_two_pools_threads_contend_for_last_slot() -> None:
    store = MemoryStateStore()
    creds = [_cred("a", max_concurrency=1)]
    pools = [CredentialPool(credentials=creds, store=store) for _ in range(2)]
    gauge = _Gauge()

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(_sync_worker, pools[i % 2], gauge, 20) for i in range(12)]
        for f in futures:
            f.result()

    assert gauge.peak["a"] == 1
    assert gauge.completed["a"] == 240
    _assert_invariants(store)


# --- timeouts and reclamation under load ---------------------------------------------------


def test_timeout_automatically_frees_capacity_held_by_orphans() -> None:
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    pool = CredentialPool(
        credentials=[_cred("a", max_concurrency=4)],
        store=store,
        clock=clock,
        lease_timeout=30.0,
    )
    orphans = [pool.acquire_sync() for _ in range(4)]  # callers crash: never reported
    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync()

    clock.advance(31.0)

    # Capacity returns with no explicit action, even under concurrent acquirers.
    results: list[Lease] = []
    lock = threading.Lock()

    def grab() -> None:
        lease = pool.acquire_sync()
        with lock:
            results.append(lease)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        for f in [executor.submit(grab) for _ in range(4)]:
            f.result()

    assert len(results) == 4
    assert pool.in_flight_leases == 4
    _assert_invariants(store)
    for orphan in orphans:
        with pytest.raises(LeaseExpiredError):
            pool.report_sync(orphan, Outcome.success())
    assert pool.in_flight_leases == 4  # late reports never released a slot twice
    _assert_invariants(store)


def test_reclaim_and_report_race_releases_each_lease_exactly_once() -> None:
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    pool = CredentialPool(
        credentials=[_cred("a"), _cred("b")],
        store=store,
        clock=clock,
        lease_timeout=10.0,
        strategy=RoundRobinStrategy(),
    )
    n = 240
    leases = [pool.acquire_sync() for _ in range(n)]
    clock.advance(11.0)  # every lease is now past its deadline

    outcomes: list[str] = []
    out_lock = threading.Lock()
    barrier = threading.Barrier(24)

    def reporter(batch: list[Lease]) -> None:
        barrier.wait(timeout=10)
        for lease in batch:
            try:
                pool.report_sync(lease, Outcome.auth_failed(reason="late"))
                verdict = "settled"
            except LeaseExpiredError:
                verdict = "expired"
            except InvalidLeaseError:
                verdict = "invalid"
            with out_lock:
                outcomes.append(verdict)

    def reclaimer() -> int:
        barrier.wait(timeout=10)
        return sum(len(pool.reclaim_expired_leases()) for _ in range(20))

    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as executor:
        chunks = [leases[i::16] for i in range(16)]
        report_futures = [executor.submit(reporter, chunk) for chunk in chunks]
        reclaim_futures = [executor.submit(reclaimer) for _ in range(8)]
        for f in report_futures:
            f.result()
        reclaimed_total = sum(f.result() for f in reclaim_futures)

    # Every lease was expired: no outcome may ever have been applied.
    assert outcomes.count("expired") == n
    assert reclaimed_total <= n
    assert pool.in_flight_leases == 0
    for cid in ("a", "b"):
        record = store.get_record(cid)
        assert record is not None
        assert record.in_flight_leases == 0
        assert record.state.value == "available"
        assert record.consecutive_failures == 0
    _assert_invariants(store)


def _race_report_against_reclaim() -> None:
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    pool = CredentialPool(credentials=[_cred("a")], store=store, clock=clock, lease_timeout=10.0)
    lease = pool.acquire_sync()
    clock.advance(10.5)
    barrier = threading.Barrier(2)
    verdict: list[str] = []

    def report() -> None:
        barrier.wait(timeout=10)
        try:
            pool.report_sync(lease, Outcome.success())
            verdict.append("settled")
        except LeaseExpiredError:
            verdict.append("expired")

    def reclaim() -> None:
        barrier.wait(timeout=10)
        pool.reclaim_expired_leases()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        for f in [executor.submit(report), executor.submit(reclaim)]:
            f.result()

    assert verdict == ["expired"]
    record = store.get_record("a")
    assert record is not None
    assert record.in_flight_leases == 0
    _assert_invariants(store)


def test_report_vs_reclaim_race_on_an_expired_lease() -> None:
    """An expired lease is released exactly once and never accepts its outcome."""
    for _ in range(25):
        _race_report_against_reclaim()


def test_randomized_workload_with_orphans_keeps_accounting_exact() -> None:
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    caps = {"a": 2, "b": 3}
    pool = CredentialPool(
        credentials=[_cred(cid, max_concurrency=cap) for cid, cap in caps.items()],
        store=store,
        clock=clock,
        lease_timeout=5.0,
    )
    rng = random.Random(1234)
    live: list[Lease] = []
    for step in range(3000):
        action = rng.random()
        if action < 0.45:
            with contextlib.suppress(NoCredentialsAvailableError):
                live.append(pool.acquire_sync())
        elif action < 0.8 and live:
            lease = live.pop(rng.randrange(len(live)))
            with contextlib.suppress(LeaseExpiredError):
                pool.report_sync(lease, Outcome.success())
        elif action < 0.9:
            clock.advance(rng.choice([0.5, 2.0, 6.0]))
        else:
            pool.reclaim_expired_leases()
        for cid, cap in caps.items():
            record = store.get_record(cid)
            assert record is not None
            assert 0 <= record.in_flight_leases <= cap, (step, cid)
        _assert_invariants(store)
