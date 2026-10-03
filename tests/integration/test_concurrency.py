"""Integration tests testing concurrency, thread-safety, and asyncio-safety."""

import asyncio
import concurrent.futures
import threading

import pytest

from credweave import (
    Credential,
    CredentialPool,
    CredentialState,
    InvalidLeaseError,
    Outcome,
)


def test_multithreaded_acquire_report_stress(sample_credentials: list[Credential]) -> None:
    """Stress test: 15 threads concurrently acquiring leases and reporting outcomes."""
    pool = CredentialPool(credentials=sample_credentials)
    operations_per_thread = 20

    def worker(worker_id: int) -> None:
        for _ in range(operations_per_thread):
            lease = pool.acquire_sync()
            assert lease is not None
            pool.report_sync(lease, Outcome.success())

    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
        futures = [executor.submit(worker, i) for i in range(15)]
        for f in futures:
            f.result()

    assert pool.in_flight_leases == 0
    records = pool.list_records()
    total_completed = sum(r.total_leases for r in records)
    assert total_completed == 15 * operations_per_thread


@pytest.mark.asyncio
async def test_asyncio_acquire_report_concurrency(
    sample_credentials: list[Credential],
) -> None:
    """Stress test: 50 concurrent asyncio tasks acquiring and reporting."""
    pool = CredentialPool(credentials=sample_credentials)

    async def task_worker(task_id: int) -> None:
        lease = await pool.acquire()
        assert lease is not None
        await asyncio.sleep(0.005)
        await pool.report(lease, Outcome.success())

    tasks = [task_worker(i) for i in range(50)]
    await asyncio.gather(*tasks)

    assert pool.in_flight_leases == 0
    records = await pool.list_records_async()
    total = sum(r.total_leases for r in records)
    assert total == 50


def test_concurrent_double_report_race_condition(sample_credential: Credential) -> None:
    """Verify that when multiple threads race to report the same lease, exactly one succeeds."""
    pool = CredentialPool(credentials=[sample_credential])

    lease = pool.acquire_sync()

    success_count = 0
    failure_count = 0
    count_lock = threading.Lock()

    def try_report() -> None:
        nonlocal success_count, failure_count
        try:
            pool.report_sync(lease, Outcome.success())
            with count_lock:
                success_count += 1
        except InvalidLeaseError:
            with count_lock:
                failure_count += 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(try_report) for _ in range(10)]
        for f in futures:
            f.result()

    assert success_count == 1
    assert failure_count == 9
    assert pool.in_flight_leases == 0


def test_concurrent_mixed_outcomes_late_success_never_resurrects_revoked(
    sample_credential: Credential,
) -> None:
    """Verify that late SUCCESS outcomes from in-flight leases never resurrect a REVOKED key."""
    pool = CredentialPool(credentials=[sample_credential])

    # Acquire 10 in-flight leases
    leases = [pool.acquire_sync() for _ in range(10)]
    assert pool.in_flight_leases == 10

    barrier = threading.Barrier(10)

    def reporter(idx: int) -> None:
        barrier.wait()
        if idx == 0:
            # First thread reports auth failure -> REVOKED
            pool.report_sync(leases[idx], Outcome.auth_failed(reason="Revoked"))
        else:
            # Remaining threads report success
            pool.report_sync(leases[idx], Outcome.success())

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(reporter, i) for i in range(10)]
        for f in futures:
            f.result()

    assert pool.in_flight_leases == 0
    record = pool.get_record(sample_credential.id)
    assert record is not None
    assert record.state == CredentialState.REVOKED


def test_concurrent_mixed_outcomes_late_success_never_resurrects_unhealthy(
    sample_credential: Credential,
) -> None:
    """Verify that late SUCCESS outcomes from in-flight leases never resurrect an UNHEALTHY key."""
    pool = CredentialPool(credentials=[sample_credential])

    leases = [pool.acquire_sync() for _ in range(10)]
    assert pool.in_flight_leases == 10

    barrier = threading.Barrier(10)

    def reporter(idx: int) -> None:
        barrier.wait()
        if idx == 0:
            pool.report_sync(leases[idx], Outcome.permanent_failure(reason="Fatal"))
        else:
            pool.report_sync(leases[idx], Outcome.success())

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(reporter, i) for i in range(10)]
        for f in futures:
            f.result()

    assert pool.in_flight_leases == 0
    record = pool.get_record(sample_credential.id)
    assert record is not None
    assert record.state == CredentialState.UNHEALTHY


def test_concurrent_mixed_outcomes_late_success_never_clears_active_rate_limit(
    sample_credential: Credential,
) -> None:
    """Verify that late SUCCESS outcomes never clear an active RATE_LIMITED cooldown."""
    pool = CredentialPool(credentials=[sample_credential])

    leases = [pool.acquire_sync() for _ in range(10)]
    assert pool.in_flight_leases == 10

    barrier = threading.Barrier(10)

    def reporter(idx: int) -> None:
        barrier.wait()
        if idx == 0:
            pool.report_sync(leases[idx], Outcome.rate_limited(retry_after=60.0))
        else:
            pool.report_sync(leases[idx], Outcome.success())

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(reporter, i) for i in range(10)]
        for f in futures:
            f.result()

    assert pool.in_flight_leases == 0
    record = pool.get_record(sample_credential.id)
    assert record is not None
    assert record.state == CredentialState.RATE_LIMITED
    assert record.cooldown_until is not None


@pytest.mark.asyncio
async def test_asyncio_concurrent_mixed_outcomes_late_success_precedence(
    sample_credential: Credential,
) -> None:
    """Verify asyncio concurrent tasks reporting mixed outcomes respect state precedence."""
    pool = CredentialPool(credentials=[sample_credential])

    leases = [await pool.acquire() for _ in range(10)]
    assert pool.in_flight_leases == 10

    async def async_reporter(idx: int) -> None:
        if idx == 0:
            await pool.report(leases[idx], Outcome.auth_failed(reason="Async revoked"))
        else:
            await pool.report(leases[idx], Outcome.success())

    await asyncio.gather(*[async_reporter(i) for i in range(10)])

    assert pool.in_flight_leases == 0
    record = await pool.get_record_async(sample_credential.id)
    assert record is not None
    assert record.state == CredentialState.REVOKED
