"""A pool must work across successive and concurrent event loops, not just one.

``CredentialPool`` serialises asynchronous acquires with an ``asyncio.Lock``. That lock binds to
the event loop it first waits on, so a single cached lock used from a second loop (for example a
second ``asyncio.run`` call, or another loop in another thread) raised ``RuntimeError``.
"""

import asyncio
import json
import threading
from pathlib import Path

from credweave.application.services.pool import CredentialPool
from credweave.domain.outcomes import Outcome
from credweave.infrastructure.sources.json_source import JsonSource
from credweave.infrastructure.stores.memory import MemoryStateStore
from tests.conftest import TestClock

_CREDENTIAL_COUNT = 4
_TASKS = 8


def _write_credentials(path: Path, generation: int = 0) -> None:
    document = {
        "credentials": [
            {"id": f"k{i}", "secrets": {"api_key": f"secret-{i}-gen{generation}"}}
            for i in range(_CREDENTIAL_COUNT)
        ]
    }
    path.write_text(json.dumps(document), encoding="utf-8")


def _make_pool(tmp_path: Path) -> CredentialPool:
    path = tmp_path / "credentials.json"
    _write_credentials(path)
    return CredentialPool(source=JsonSource(path), store=MemoryStateStore(clock=TestClock()))


async def _contended_burst(pool: CredentialPool) -> None:
    """Run more concurrent acquire/report pairs than there are credentials, so tasks wait."""

    async def one() -> None:
        lease = await pool.acquire()
        await asyncio.sleep(0)
        await pool.report(lease, Outcome.success())

    await asyncio.gather(*(one() for _ in range(_TASKS)))


def test_pool_survives_successive_asyncio_run_calls(tmp_path: Path) -> None:
    pool = _make_pool(tmp_path)

    for _ in range(3):
        asyncio.run(_contended_burst(pool))

    assert pool.in_flight_leases == 0


def test_pool_serves_event_loops_running_in_parallel_threads(tmp_path: Path) -> None:
    pool = _make_pool(tmp_path)
    errors: list[BaseException] = []
    barrier = threading.Barrier(3)

    def run_loop() -> None:
        try:
            barrier.wait()
            for _ in range(5):
                asyncio.run(_contended_burst(pool))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run_loop) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert pool.in_flight_leases == 0
