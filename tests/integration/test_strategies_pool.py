"""Pool-level integration tests for the built-in selection strategies."""

import concurrent.futures
from collections import Counter
from datetime import timedelta

import pytest

from credweave import (
    ConfigurationError,
    Credential,
    CredentialPool,
    FailoverStrategy,
    LeastRecentlyUsedStrategy,
    LeastUsedStrategy,
    NoCredentialsAvailableError,
    Outcome,
    RandomStrategy,
    SelectionContext,
    SelectionStrategy,
    WeightedStrategy,
)
from tests.conftest import TestClock


def make_creds(*specs: tuple[str, dict[str, object]]) -> list[Credential]:
    return [Credential(id=cid, secrets={"k": cid}, metadata=meta) for cid, meta in specs]


def acquire_release(pool: CredentialPool, ctx: SelectionContext | None = None) -> str:
    lease = pool.acquire_sync(ctx)
    pool.report_sync(lease, Outcome.success())
    return lease.credential_id


def test_candidates_carry_total_leases_and_last_used(test_clock: TestClock) -> None:
    seen: list[tuple[str, int, object]] = []

    class Spy:
        name = "spy"

        def select(self, candidates, context=None):  # type: ignore[no-untyped-def]
            seen.extend((c.credential_id, c.total_leases, c.last_used_at) for c in candidates)
            return candidates[0]

    pool = CredentialPool(
        credentials=make_creds(("a", {}), ("b", {})), clock=test_clock, strategy=Spy()
    )
    acquire_release(pool)
    started = test_clock.now()
    test_clock.advance(10)
    acquire_release(pool)
    assert seen[:2] == [("a", 0, None), ("b", 0, None)]
    assert seen[2] == ("a", 1, started)
    assert seen[3] == ("b", 0, None)


@pytest.mark.asyncio
async def test_async_acquire_populates_usage_fields(test_clock: TestClock) -> None:
    pool = CredentialPool(
        credentials=make_creds(("a", {}), ("b", {})),
        clock=test_clock,
        strategy=LeastUsedStrategy(),
    )
    picks = []
    for _ in range(4):
        lease = await pool.acquire()
        await pool.report(lease, Outcome.success())
        picks.append(lease.credential_id)
    assert picks == ["a", "b", "a", "b"]


def test_least_used_balances_through_pool(test_clock: TestClock) -> None:
    pool = CredentialPool(
        credentials=make_creds(("a", {}), ("b", {}), ("c", {})),
        clock=test_clock,
        strategy=LeastUsedStrategy(),
    )
    picks = [acquire_release(pool) for _ in range(9)]
    assert Counter(picks) == {"a": 3, "b": 3, "c": 3}
    assert picks[:3] == ["a", "b", "c"]


def test_least_used_catches_up_recovered_credential(test_clock: TestClock) -> None:
    pool = CredentialPool(
        credentials=make_creds(("a", {}), ("b", {})),
        clock=test_clock,
        strategy=LeastUsedStrategy(),
    )
    lease = pool.acquire_sync()  # a
    pool.report_sync(lease, Outcome.rate_limited(retry_after=30.0))
    for _ in range(3):
        assert acquire_release(pool) == "b"
    test_clock.advance(31)
    # a recovered with 1 lease vs b's 4 -> a is chosen until it catches up.
    assert [acquire_release(pool) for _ in range(3)] == ["a", "a", "a"]
    assert acquire_release(pool) in {"a", "b"}


def test_lru_prefers_never_used_then_oldest(test_clock: TestClock) -> None:
    pool = CredentialPool(
        credentials=make_creds(("a", {}), ("b", {}), ("c", {})),
        clock=test_clock,
        strategy=LeastRecentlyUsedStrategy(),
    )
    order = []
    for _ in range(3):
        order.append(acquire_release(pool))
        test_clock.advance(5)
    assert order == ["a", "b", "c"]
    assert [acquire_release(pool) for _ in range(3)] == ["a", "b", "c"]


def test_lru_respects_cooldown_and_recovery(test_clock: TestClock) -> None:
    pool = CredentialPool(
        credentials=make_creds(("a", {}), ("b", {})),
        clock=test_clock,
        strategy=LeastRecentlyUsedStrategy(),
    )
    lease = pool.acquire_sync()
    assert lease.credential_id == "a"
    pool.report_sync(lease, Outcome.rate_limited(retry_after=60.0))
    test_clock.advance(1)
    assert acquire_release(pool) == "b"
    test_clock.advance(1)
    assert acquire_release(pool) == "b"
    test_clock.advance(120)
    assert acquire_release(pool) == "a"  # a is now the least recently used


def test_failover_cascades_on_cooldown_and_revocation(test_clock: TestClock) -> None:
    pool = CredentialPool(
        credentials=make_creds(
            ("primary", {"priority": 0}),
            ("secondary", {"priority": 1}),
            ("tertiary", {"priority": 2}),
        ),
        clock=test_clock,
        strategy=FailoverStrategy(),
    )
    assert acquire_release(pool) == "primary"

    lease = pool.acquire_sync()
    pool.report_sync(lease, Outcome.rate_limited(retry_after=30.0))
    assert acquire_release(pool) == "secondary"

    lease = pool.acquire_sync()
    assert lease.credential_id == "secondary"
    pool.report_sync(lease, Outcome.auth_failed(reason="revoked"))
    assert acquire_release(pool) == "tertiary"

    test_clock.advance(31)  # primary recovers and takes over again
    assert acquire_release(pool) == "primary"

    # Revoked credential never comes back, even after a long time.
    test_clock.advance(10_000)
    assert acquire_release(pool) == "primary"


def test_failover_all_blocked_raises(test_clock: TestClock) -> None:
    pool = CredentialPool(
        credentials=make_creds(("a", {"priority": 0}), ("b", {"priority": 1})),
        clock=test_clock,
        strategy=FailoverStrategy(),
    )
    for _ in range(2):
        lease = pool.acquire_sync()
        pool.report_sync(lease, Outcome.auth_failed(reason="revoked"))
    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync()


def test_failover_explicit_order_via_pool(test_clock: TestClock) -> None:
    pool = CredentialPool(
        credentials=make_creds(("a", {}), ("b", {}), ("c", {})),
        clock=test_clock,
        strategy=FailoverStrategy(priorities=["c", "b", "a"]),
    )
    assert acquire_release(pool) == "c"


def test_weighted_distribution_through_pool(test_clock: TestClock) -> None:
    pool = CredentialPool(
        credentials=make_creds(("heavy", {"weight": 3}), ("light", {"weight": 1})),
        clock=test_clock,
        strategy=WeightedStrategy(),
    )
    picks = [acquire_release(pool) for _ in range(40)]
    assert Counter(picks) == {"heavy": 30, "light": 10}


def test_weighted_skips_cooling_credential_then_rebalances(test_clock: TestClock) -> None:
    pool = CredentialPool(
        credentials=make_creds(("a", {"weight": 1}), ("b", {"weight": 1})),
        clock=test_clock,
        strategy=WeightedStrategy(),
    )
    lease = pool.acquire_sync()
    pool.report_sync(lease, Outcome.rate_limited(retry_after=30.0))
    first = lease.credential_id
    other = "b" if first == "a" else "a"
    assert {acquire_release(pool) for _ in range(5)} == {other}
    test_clock.advance(31)
    counts = Counter(acquire_release(pool) for _ in range(20))
    assert counts["a"] == counts["b"] == 10


def test_invalid_weight_surfaces_as_configuration_error(test_clock: TestClock) -> None:
    pool = CredentialPool(
        credentials=make_creds(("a", {"weight": -5}), ("b", {})),
        clock=test_clock,
        strategy=WeightedStrategy(),
    )
    with pytest.raises(ConfigurationError):
        pool.acquire_sync()
    assert pool.in_flight_leases == 0


@pytest.mark.parametrize(
    "strategy_factory",
    [
        LeastUsedStrategy,
        LeastRecentlyUsedStrategy,
        FailoverStrategy,
        WeightedStrategy,
        lambda: RandomStrategy(seed=1),
    ],
)
def test_tags_and_preferred_metadata_through_pool(
    test_clock: TestClock, strategy_factory: type[SelectionStrategy]
) -> None:
    pool = CredentialPool(
        credentials=make_creds(
            ("a", {"tags": ("fast",), "region": "us"}),
            ("b", {"tags": ("fast", "eu"), "region": "eu"}),
            ("c", {"tags": ("slow",), "region": "eu"}),
        ),
        clock=test_clock,
        strategy=strategy_factory(),
    )
    only_fast = SelectionContext(required_tags=frozenset({"fast"}))
    assert {acquire_release(pool, only_fast) for _ in range(10)} <= {"a", "b"}
    fast_eu = SelectionContext(
        required_tags=frozenset({"fast"}), preferred_metadata={"region": "eu"}
    )
    assert {acquire_release(pool, fast_eu) for _ in range(10)} == {"b"}
    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync(SelectionContext(required_tags=frozenset({"gold"})))


def test_seeded_random_is_reproducible_through_pool(test_clock: TestClock) -> None:
    def run() -> list[str]:
        pool = CredentialPool(
            credentials=make_creds(("a", {}), ("b", {}), ("c", {})),
            clock=TestClock(),
            strategy=RandomStrategy(seed=1234),
        )
        return [acquire_release(pool) for _ in range(30)]

    assert run() == run()


def test_dynamic_credential_removal_through_source(test_clock: TestClock) -> None:
    class MutableSource:
        def __init__(self, creds: list[Credential]) -> None:
            self.creds = creds

        def get_credentials(self) -> list[Credential]:
            return list(self.creds)

        async def get_credentials_async(self) -> list[Credential]:
            return self.get_credentials()

    creds = make_creds(("a", {}), ("b", {}), ("c", {}))
    source = MutableSource(creds)
    pool = CredentialPool(source=source, clock=test_clock, strategy=WeightedStrategy())  # type: ignore[arg-type]
    assert Counter(acquire_release(pool) for _ in range(6)) == {"a": 2, "b": 2, "c": 2}
    source.creds = [creds[0], creds[2]]
    assert Counter(acquire_release(pool) for _ in range(6)) == {"a": 3, "c": 3}
    source.creds = creds
    assert Counter(acquire_release(pool) for _ in range(6)).keys() == {"a", "b", "c"}


def test_concurrent_pool_usage_least_used_stays_balanced(test_clock: TestClock) -> None:
    pool = CredentialPool(
        credentials=make_creds(("a", {}), ("b", {}), ("c", {})),
        clock=test_clock,
        strategy=LeastUsedStrategy(),
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        picks = list(ex.map(lambda _: acquire_release(pool), range(90)))
    assert Counter(picks) == {"a": 30, "b": 30, "c": 30}


def test_concurrent_pool_usage_weighted(test_clock: TestClock) -> None:
    pool = CredentialPool(
        credentials=make_creds(("a", {"weight": 2}), ("b", {"weight": 1})),
        clock=test_clock,
        strategy=WeightedStrategy(),
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        picks = list(ex.map(lambda _: acquire_release(pool), range(90)))
    assert Counter(picks) == {"a": 60, "b": 30}


def test_failover_unaffected_by_time_gap(test_clock: TestClock) -> None:
    pool = CredentialPool(
        credentials=make_creds(("a", {"priority": 0}), ("b", {"priority": 1})),
        clock=test_clock,
        strategy=FailoverStrategy(),
    )
    lease = pool.acquire_sync()
    pool.report_sync(lease, Outcome.rate_limited(retry_after=10.0))
    assert acquire_release(pool) == "b"
    test_clock.advance(timedelta(seconds=11).total_seconds())
    assert acquire_release(pool) == "a"
