"""Integration tests: CredentialPool driven by dynamic sources (JsonSource, EnvSource)."""

import asyncio
import contextlib
import json
import os
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from credweave import (
    CredentialPool,
    CredentialSourceError,
    CredentialState,
    EnvCredential,
    EnvSource,
    JsonSource,
    Lease,
    MemoryStateStore,
    NoCredentialsAvailableError,
    Outcome,
    RoundRobinStrategy,
)
from credweave.application.ports.strategy import CredentialCandidate, SelectionContext
from tests.conftest import TestClock
from tests.lease_helpers import assert_lease_accounting


def entry(cred_id: str, version: int, **metadata: Any) -> dict[str, Any]:
    return {
        "id": cred_id,
        "secrets": {"api_key": f"{cred_id}-secret-{version}"},
        "metadata": {"v": version, **metadata},
    }


def write_creds(path: Path, entries: Sequence[dict[str, Any]], *, atomic: bool = True) -> None:
    payload = json.dumps({"credentials": list(entries)}).encode()
    if not atomic:
        path.write_bytes(payload)
        return
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(payload)
    for attempt in range(200):  # Windows refuses to replace a file a reader has open
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 199:
                raise
            time.sleep(0.001)


@pytest.fixture
def creds_path(tmp_path: Path) -> Path:
    path = tmp_path / "creds.json"
    write_creds(path, [entry("a", 1), entry("b", 1)])
    return path


def make_pool(path: Path, clock: TestClock, **kwargs: Any) -> tuple[CredentialPool, JsonSource]:
    source = JsonSource(path)
    pool = CredentialPool(source=source, clock=clock, **kwargs)
    return pool, source


def acquire_ids(pool: CredentialPool, count: int) -> list[str]:
    ids = []
    for _ in range(count):
        lease = pool.acquire_sync()
        ids.append(lease.credential_id)
        pool.report_sync(lease, Outcome.success())
    return ids


# --------------------------------------------------------------------------- rotation


def test_rotation_returns_new_object_and_keeps_history_and_cooldown(
    creds_path: Path, test_clock: TestClock
) -> None:
    pool, _ = make_pool(creds_path, test_clock)
    lease_a = next(
        lease for lease in (pool.acquire_sync() for _ in range(2)) if lease.credential_id == "a"
    )
    old_object = lease_a.credential
    assert old_object.require_secret("api_key") == "a-secret-1"
    pool.report_sync(lease_a, Outcome.rate_limited(retry_after=60.0))
    before = pool.get_record("a")
    assert before is not None
    assert before.state is CredentialState.RATE_LIMITED

    write_creds(creds_path, [entry("a", 2), entry("b", 1)])

    rotated = pool.get_credential("a")
    assert rotated is not None
    assert rotated is not old_object
    assert rotated.require_secret("api_key") == "a-secret-2"
    after = pool.get_record("a")
    assert after == before  # usage and cooldown preserved under the stable id

    # Still cooling down: only b is leased, and b's lease was never reported so far.
    other = pool.acquire_sync()
    assert other.credential_id == "b"
    pool.report_sync(other, Outcome.success())
    assert all(i == "b" for i in acquire_ids(pool, 3))

    test_clock.advance(61.0)
    lease = None
    for _ in range(2):
        candidate = pool.acquire_sync()
        pool.report_sync(candidate, Outcome.success())
        if candidate.credential_id == "a":
            lease = candidate
    assert lease is not None
    assert lease.credential.require_secret("api_key") == "a-secret-2"
    record = pool.get_record("a")
    assert record is not None
    assert record.total_leases == before.total_leases + 1
    assert_lease_accounting(pool.store)


async def test_rotation_async(creds_path: Path, test_clock: TestClock) -> None:
    pool, _ = make_pool(creds_path, test_clock)
    first = await pool.acquire()
    await pool.report(first, Outcome.success())
    write_creds(creds_path, [entry("a", 2), entry("b", 2)])
    seen = set()
    for _ in range(4):
        lease = await pool.acquire()
        seen.add(lease.credential.require_secret("api_key"))
        await pool.report(lease, Outcome.success())
    assert seen == {"a-secret-2", "b-secret-2"}
    assert_lease_accounting(pool.store)


def test_reloaded_concurrency_cap_applies_to_new_leases(
    creds_path: Path, test_clock: TestClock
) -> None:
    pool, _ = make_pool(creds_path, test_clock)
    write_creds(creds_path, [entry("a", 1, max_concurrency=1), entry("b", 1)])
    held = [pool.acquire_sync() for _ in range(6)]
    assert sum(lease.credential_id == "a" for lease in held) == 1
    for lease in held:
        pool.report_sync(lease, Outcome.success())
    assert_lease_accounting(pool.store)


# --------------------------------------------------------------------------- removal


def test_removed_credential_gets_no_new_leases_but_active_lease_stays_reportable(
    creds_path: Path, test_clock: TestClock
) -> None:
    pool, _ = make_pool(creds_path, test_clock)
    lease_b = next(
        lease for lease in (pool.acquire_sync() for _ in range(2)) if lease.credential_id == "b"
    )
    write_creds(creds_path, [entry("a", 2)])

    assert set(acquire_ids(pool, 5)) == {"a"}
    assert pool.get_credential("b") is None

    # The active lease still reports its original Credential object, secrets intact.
    active = {lease.lease_id: lease for lease in pool.active_leases}
    assert lease_b.lease_id in active
    assert active[lease_b.lease_id].credential is lease_b.credential
    assert active[lease_b.lease_id].credential.require_secret("api_key") == "b-secret-1"

    pool.report_sync(lease_b, Outcome.success())
    assert pool.in_flight_leases == len(pool.active_leases)
    assert_lease_accounting(pool.store)
    record = pool.get_record("b")
    assert record is not None
    assert record.in_flight_leases == 0


def test_active_lease_shows_leased_object_after_rotation(
    creds_path: Path, test_clock: TestClock
) -> None:
    pool, _ = make_pool(creds_path, test_clock)
    lease = pool.acquire_sync()
    leased_secret = lease.credential.require_secret("api_key")
    write_creds(creds_path, [entry("a", 9), entry("b", 9)])
    (active,) = pool.active_leases
    assert active.credential is lease.credential
    assert active.credential.require_secret("api_key") == leased_secret
    pool.report_sync(lease, Outcome.success())
    assert pool.active_leases == ()


def test_foreign_lease_falls_back_to_source_credential(
    creds_path: Path, test_clock: TestClock
) -> None:
    clock = test_clock
    store = MemoryStateStore(clock=clock)
    pool_one = CredentialPool(source=JsonSource(creds_path), clock=clock, store=store)
    pool_two = CredentialPool(source=JsonSource(creds_path), clock=clock, store=store)
    lease = pool_one.acquire_sync()
    (seen,) = pool_two.active_leases
    assert seen.lease_id == lease.lease_id
    assert seen.credential.id == lease.credential_id
    assert seen.credential.has_secret("api_key")  # taken from the source, not a hollow placeholder
    pool_one.report_sync(lease, Outcome.success())


def test_removing_everything_raises_no_credentials(creds_path: Path, test_clock: TestClock) -> None:
    pool, _ = make_pool(creds_path, test_clock)
    write_creds(creds_path, [])
    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync()


# --------------------------------------------------------------------------- addition


def test_added_credential_is_immediately_eligible_with_lazy_record(
    creds_path: Path, test_clock: TestClock
) -> None:
    pool, _ = make_pool(creds_path, test_clock)
    acquire_ids(pool, 2)
    assert pool.get_record("c") is None
    write_creds(creds_path, [entry("a", 1), entry("b", 1), entry("c", 1)])
    assert "c" in acquire_ids(pool, 3)
    record = pool.get_record("c")
    assert record is not None
    assert record.state is CredentialState.AVAILABLE
    assert record.total_leases == 1
    assert_lease_accounting(pool.store)


async def test_added_credential_async(creds_path: Path, test_clock: TestClock) -> None:
    pool, _ = make_pool(creds_path, test_clock)
    write_creds(creds_path, [entry("a", 1), entry("b", 1), entry("c", 1)])
    ids = []
    for _ in range(3):
        lease = await pool.acquire()
        ids.append(lease.credential_id)
        await pool.report(lease, Outcome.success())
    assert set(ids) == {"a", "b", "c"}


def test_removed_then_readded_credential_regains_its_history(
    creds_path: Path, test_clock: TestClock
) -> None:
    pool, _ = make_pool(creds_path, test_clock)
    lease_a = next(
        lease for lease in (pool.acquire_sync() for _ in range(2)) if lease.credential_id == "a"
    )
    pool.report_sync(lease_a, Outcome.rate_limited(retry_after=300.0))
    stamped = pool.get_record("a")
    write_creds(creds_path, [entry("b", 1)])
    acquire_ids(pool, 2)
    write_creds(creds_path, [entry("a", 5), entry("b", 1)])
    assert pool.get_record("a") == stamped
    assert set(acquire_ids(pool, 3)) == {"b"}  # still cooling down
    test_clock.advance(301.0)
    assert "a" in acquire_ids(pool, 2)


# --------------------------------------------------------------------------- bad reloads


def test_malformed_intermediate_content_keeps_serving_last_good(
    creds_path: Path, test_clock: TestClock
) -> None:
    pool, source = make_pool(creds_path, test_clock)
    acquire_ids(pool, 2)
    creds_path.write_bytes(b'{"credentials": [{"id": "a", "secrets": {"api_key": "partial')
    assert set(acquire_ids(pool, 4)) == {"a", "b"}
    assert not source.reload_status.ok
    write_creds(creds_path, [entry("a", 3), entry("b", 3)])
    assert source.refresh().ok
    secrets = {pool.acquire_sync().credential.require_secret("api_key") for _ in range(2)}
    assert secrets == {"a-secret-3", "b-secret-3"}


def test_invalid_max_concurrency_on_reload_cannot_break_acquire(
    creds_path: Path, test_clock: TestClock
) -> None:
    pool, source = make_pool(creds_path, test_clock)
    write_creds(creds_path, [entry("a", 2, max_concurrency=0), entry("b", 2)])
    assert set(acquire_ids(pool, 4)) == {"a", "b"}  # rejected reload; old snapshot still served
    assert "max_concurrency" in (source.reload_status.last_error or "")
    assert pool.get_credential("a") is not None
    assert pool.get_credential("a").get_metadata("v") == 1  # type: ignore[union-attr]


def test_initial_malformed_file_fails_normally(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    path.write_text("{ nope")
    with pytest.raises(CredentialSourceError):
        JsonSource(path)


# --------------------------------------------------------------------------- races


class AfterSelectStrategy(RoundRobinStrategy):
    """Runs a hook between candidate selection and lease reservation."""

    def __init__(self, hook: Callable[[str], None]) -> None:
        super().__init__()
        self.hook = hook
        self.armed = False

    def select(
        self,
        candidates: Sequence[CredentialCandidate],
        context: SelectionContext | None = None,
    ) -> CredentialCandidate | None:
        chosen = super().select(candidates, context)
        if self.armed and chosen is not None:
            self.armed = False
            self.hook(chosen.credential_id)
        return chosen


@pytest.mark.parametrize("change", ["rotate", "remove"])
def test_source_changes_between_selection_and_reservation(
    creds_path: Path, test_clock: TestClock, change: str
) -> None:
    def hook(chosen_id: str) -> None:
        entries = [entry("a", 2), entry("b", 2)]
        if change == "remove":
            entries = [e for e in entries if e["id"] != chosen_id]
        write_creds(creds_path, entries)

    strategy = AfterSelectStrategy(hook)
    source = JsonSource(creds_path)
    pool = CredentialPool(source=source, strategy=strategy, clock=test_clock)

    strategy.armed = True
    lease = pool.acquire_sync()
    # Linearised at the snapshot: the lease holds the snapshot's object and is reportable.
    assert lease.credential.require_secret("api_key") == f"{lease.credential_id}-secret-1"
    assert_lease_accounting(pool.store)
    (active,) = pool.active_leases
    assert active.credential is lease.credential
    pool.report_sync(lease, Outcome.success())
    assert_lease_accounting(pool.store)

    later = {acquire_ids(pool, 1)[0] for _ in range(4)}
    if change == "remove":
        assert lease.credential_id not in later
    else:
        fresh = pool.get_credential(lease.credential_id)
        assert fresh is not None
        assert fresh.require_secret("api_key").endswith("-2")


async def test_source_changes_between_selection_and_reservation_async(
    creds_path: Path, test_clock: TestClock
) -> None:
    strategy = AfterSelectStrategy(lambda _id: write_creds(creds_path, [entry("a", 2)]))
    pool = CredentialPool(source=JsonSource(creds_path), strategy=strategy, clock=test_clock)
    strategy.armed = True
    lease = await pool.acquire()
    await pool.report(lease, Outcome.success())
    for _ in range(3):
        again = await pool.acquire()
        assert again.credential_id == "a"
        assert again.credential.require_secret("api_key") == "a-secret-2"
        await pool.report(again, Outcome.success())
    assert_lease_accounting(pool.store)


# --------------------------------------------------------------------------- concurrency


def check_lease(lease: Lease, last_seen: dict[str, int]) -> None:
    credential = lease.credential
    version = credential.get_metadata("v")
    # Secret and metadata must come from the same file version (no torn snapshots).
    assert credential.require_secret("api_key") == f"{credential.id}-secret-{version}"
    assert version >= last_seen.get(credential.id, 0)
    last_seen[credential.id] = version


def run_writer(path: Path, stop: threading.Event, errors: list[BaseException]) -> threading.Thread:
    def work() -> None:
        try:
            version = 1
            while not stop.is_set():
                version += 1
                write_creds(path, [entry("a", version), entry("b", version)])
                if version % 5 == 0:  # a torn, in-place intermediate state
                    with contextlib.suppress(PermissionError):
                        path.write_bytes(b'{"credentials": [{"id": "a", "secrets": {"api_key": "x')
                time.sleep(0.001)
        except BaseException as exc:  # pragma: no cover - surfaced by the test
            errors.append(exc)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread


def test_concurrent_threads_while_file_is_rewritten(
    creds_path: Path, test_clock: TestClock
) -> None:
    pool, source = make_pool(creds_path, test_clock)
    stop = threading.Event()
    errors: list[BaseException] = []
    writer = run_writer(creds_path, stop, errors)

    def reader() -> None:
        try:
            seen: dict[str, int] = {}
            for _ in range(150):
                lease = pool.acquire_sync()
                check_lease(lease, seen)
                pool.report_sync(lease, Outcome.success())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    stop.set()
    writer.join(timeout=10)

    assert not errors, errors
    assert not any(t.is_alive() for t in threads)
    assert pool.in_flight_leases == 0
    assert_lease_accounting(pool.store)
    assert source.reload_status.generation > 1


async def test_concurrent_asyncio_callers_while_file_is_rewritten(
    creds_path: Path, test_clock: TestClock
) -> None:
    pool, source = make_pool(creds_path, test_clock)
    stop = threading.Event()
    errors: list[BaseException] = []
    writer = run_writer(creds_path, stop, errors)

    async def worker() -> None:
        seen: dict[str, int] = {}
        for _ in range(40):
            lease = await pool.acquire()
            check_lease(lease, seen)
            await asyncio.sleep(0)
            await pool.report(lease, Outcome.success())

    try:
        await asyncio.wait_for(asyncio.gather(*(worker() for _ in range(8))), timeout=60)
    finally:
        stop.set()
        writer.join(timeout=10)

    assert not errors, errors
    assert pool.in_flight_leases == 0
    assert_lease_accounting(pool.store)
    assert source.reload_status.generation > 1


def test_concurrent_get_credentials_returns_consistent_monotonic_snapshots(
    creds_path: Path,
) -> None:
    source = JsonSource(creds_path)
    stop = threading.Event()
    errors: list[BaseException] = []
    writer = run_writer(creds_path, stop, errors)

    def reader() -> None:
        try:
            last = 0
            for _ in range(300):
                creds = source.get_credentials()
                assert [c.id for c in creds] == ["a", "b"]
                versions = {c.get_metadata("v") for c in creds}
                assert len(versions) == 1  # both credentials come from one file version
                (version,) = versions
                assert version >= last
                last = version
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    stop.set()
    writer.join(timeout=10)
    assert not errors, errors


async def test_concurrent_async_reads_share_snapshots(creds_path: Path) -> None:
    source = JsonSource(creds_path)
    results = await asyncio.gather(*(source.get_credentials_async() for _ in range(50)))
    assert all(r is results[0] for r in results)
    write_creds(creds_path, [entry("a", 2), entry("b", 2)])
    results = await asyncio.gather(*(source.get_credentials_async() for _ in range(50)))
    assert all(r is results[0] for r in results)
    assert results[0][0].get_metadata("v") == 2


# --------------------------------------------------------------------------- EnvSource


def test_env_source_rotation_preserves_state_under_stable_id(test_clock: TestClock) -> None:
    env = {"KEY_A": "env-a-1", "KEY_B": "env-b-1"}
    source = EnvSource(
        [
            EnvCredential(id="a", secrets={"api_key": "KEY_A"}),
            EnvCredential(id="b", secrets={"api_key": "KEY_B"}),
        ],
        environ=env,
    )
    pool = CredentialPool(source=source, clock=test_clock)
    lease = next(x for x in (pool.acquire_sync() for _ in range(2)) if x.credential_id == "a")
    pool.report_sync(lease, Outcome.rate_limited(retry_after=60.0))
    before = pool.get_record("a")

    env["KEY_A"] = "env-a-2"
    assert pool.get_credential("a").require_secret("api_key") == "env-a-2"  # type: ignore[union-attr]
    assert pool.get_record("a") == before
    assert set(acquire_ids(pool, 3)) == {"b"}
    test_clock.advance(61.0)
    secrets = {pool.acquire_sync().credential.require_secret("api_key") for _ in range(2)}
    assert secrets == {"env-a-2", "env-b-1"}


async def test_env_source_async_pool(test_clock: TestClock) -> None:
    env = {"KEY_A": "env-a-1"}
    pool = CredentialPool(
        source=EnvSource([EnvCredential(id="a", secrets={"api_key": "KEY_A"})], environ=env),
        clock=test_clock,
    )
    lease = await pool.acquire()
    assert lease.credential.require_secret("api_key") == "env-a-1"
    await pool.report(lease, Outcome.success())
    env["KEY_A"] = "env-a-2"
    lease = await pool.acquire()
    assert lease.credential.require_secret("api_key") == "env-a-2"
    await pool.report(lease, Outcome.success())
    del env["KEY_A"]
    with pytest.raises(CredentialSourceError):
        await pool.acquire()
    assert_lease_accounting(pool.store)
