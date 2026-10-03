"""Integration tests testing concurrency, thread-safety, and asyncio-safety."""

import asyncio
import concurrent.futures
import threading

import pytest

from credweave import (
    Credential,
    CredentialPool,
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
