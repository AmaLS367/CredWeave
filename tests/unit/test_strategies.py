"""Unit tests for Weighted, LeastUsed, LRU, Failover and Random strategies + shared filtering."""

import concurrent.futures
import random
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

import pytest

from credweave import (
    ConfigurationError,
    FailoverStrategy,
    LeastRecentlyUsedStrategy,
    LeastUsedStrategy,
    RandomStrategy,
    RoundRobinStrategy,
    WeightedStrategy,
)
from credweave.application.ports.strategy import (
    CredentialCandidate,
    SelectionContext,
    SelectionStrategy,
)
from credweave.domain.enums import CredentialState
from credweave.domain.models import Credential

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
AVAILABLE = CredentialState.AVAILABLE


def cand(
    cid: str,
    *,
    state: CredentialState = AVAILABLE,
    total: int = 0,
    last_used: datetime | None = None,
    **metadata: Any,
) -> CredentialCandidate:
    return CredentialCandidate(
        credential=Credential(id=cid, secrets={"k": "v"}, metadata=metadata),
        state=state,
        total_leases=total,
        last_used_at=last_used,
    )


def ids(items: Sequence[CredentialCandidate | None]) -> list[str]:
    return [i.credential_id for i in items if i is not None]


StrategyFactory = Callable[[], SelectionStrategy]
ALL_STRATEGIES: list[tuple[str, StrategyFactory]] = [
    ("round_robin", RoundRobinStrategy),
    ("weighted", WeightedStrategy),
    ("least_used", LeastUsedStrategy),
    ("least_recently_used", LeastRecentlyUsedStrategy),
    ("failover", FailoverStrategy),
    ("random", lambda: RandomStrategy(seed=1)),
]


# --------------------------------------------------------------------------- shared behavior
@pytest.mark.parametrize(("name", "factory"), ALL_STRATEGIES)
class TestSharedEligibility:
    def test_protocol_and_name(self, name: str, factory: StrategyFactory) -> None:
        strat = factory()
        assert isinstance(strat, SelectionStrategy)
        assert strat.name == name

    def test_empty_returns_none(self, name: str, factory: StrategyFactory) -> None:
        assert factory().select([]) is None

    def test_unavailable_skipped(self, name: str, factory: StrategyFactory) -> None:
        strat = factory()
        blocked = [cand(f"c-{s.value}", state=s) for s in CredentialState if s is not AVAILABLE]
        assert strat.select(blocked) is None
        ok = cand("ok", total=99, last_used=T0)
        for _ in range(5):
            chosen = strat.select([*blocked, ok])
            assert chosen is ok

    def test_required_tags_hard_filter(self, name: str, factory: StrategyFactory) -> None:
        strat = factory()
        pool = [
            cand("a", tags=("fast",)),
            cand("b", tags=["fast", "eu"]),
            cand("c", tags="eu"),
            cand("d"),
            cand("e", tags=42),
        ]
        ctx = SelectionContext(required_tags=frozenset({"fast", "eu"}))
        for _ in range(10):
            chosen = strat.select(pool, ctx)
            assert chosen is not None
            assert chosen.credential_id == "b"
        assert strat.select(pool, SelectionContext(required_tags=frozenset({"nope"}))) is None

    def test_preferred_metadata_soft_filter(self, name: str, factory: StrategyFactory) -> None:
        strat = factory()
        pool = [cand("a", region="us"), cand("b", region="eu"), cand("c", region="eu")]
        ctx = SelectionContext(preferred_metadata={"region": "eu"})
        for _ in range(10):
            chosen = strat.select(pool, ctx)
            assert chosen is not None
            assert chosen.credential_id in {"b", "c"}
        # Nobody matches -> falls back to any eligible candidate.
        fallback = strat.select(pool, SelectionContext(preferred_metadata={"region": "ap"}))
        assert fallback is not None

    def test_preferred_metadata_never_overrides_availability(
        self, name: str, factory: StrategyFactory
    ) -> None:
        strat = factory()
        pool = [cand("a", region="eu", state=CredentialState.COOLDOWN), cand("b", region="us")]
        chosen = strat.select(pool, SelectionContext(preferred_metadata={"region": "eu"}))
        assert chosen is not None
        assert chosen.credential_id == "b"

    def test_preferred_metadata_requires_all_keys(
        self, name: str, factory: StrategyFactory
    ) -> None:
        strat = factory()
        pool = [cand("a", region="eu"), cand("b", region="eu", tier="gold")]
        ctx = SelectionContext(preferred_metadata={"region": "eu", "tier": "gold"})
        chosen = strat.select(pool, ctx)
        assert chosen is not None
        assert chosen.credential_id == "b"

    def test_preferred_metadata_checks_runtime_metadata(
        self, name: str, factory: StrategyFactory
    ) -> None:
        strat = factory()
        runtime = CredentialCandidate(
            credential=Credential(id="rt"), state=AVAILABLE, metadata={"zone": "z1"}
        )
        pool = [cand("plain"), runtime]
        chosen = strat.select(pool, SelectionContext(preferred_metadata={"zone": "z1"}))
        assert chosen is runtime

    def test_dynamic_removal_and_recovery(self, name: str, factory: StrategyFactory) -> None:
        strat = factory()
        a, b, c = cand("a"), cand("b"), cand("c")
        assert strat.select([a, b, c]) is not None
        # b leaves the pool entirely, a goes into cooldown, then a recovers.
        only_c = strat.select([cand("a", state=CredentialState.COOLDOWN), c])
        assert only_c is c
        assert strat.select([c]) is c
        assert strat.select([a, c]) is not None
        assert strat.select([a, b, c]) is not None


# --------------------------------------------------------------------------- round robin
class TestRoundRobinHardening:
    def test_preferred_metadata_rotates_within_matches(self) -> None:
        strat = RoundRobinStrategy()
        pool = [cand("a", r="x"), cand("b", r="y"), cand("c", r="x"), cand("d", r="y")]
        ctx = SelectionContext(preferred_metadata={"r": "x"})
        assert ids([strat.select(pool, ctx) for _ in range(4)]) == ["a", "c", "a", "c"]

    def test_rotation_continues_after_last_removed(self) -> None:
        strat = RoundRobinStrategy()
        a, b, c = cand("a"), cand("b"), cand("c")
        assert ids([strat.select([a, b, c]), strat.select([a, b, c])]) == ["a", "b"]
        # b (last selected) disappears; rotation restarts from first eligible.
        assert ids([strat.select([a, c])]) == ["a"]

    def test_recovered_credential_rejoins_rotation(self) -> None:
        strat = RoundRobinStrategy()
        a, b, c = cand("a"), cand("b"), cand("c")
        b_down = cand("b", state=CredentialState.COOLDOWN)
        picks = [strat.select([a, b_down, c]) for _ in range(3)]
        assert ids(picks) == ["a", "c", "a"]
        assert ids([strat.select([a, b, c])]) == ["b"]


# --------------------------------------------------------------------------- weighted
class TestWeighted:
    def test_distribution_matches_weights_exactly_per_cycle(self) -> None:
        strat = WeightedStrategy()
        pool = [cand("a", weight=5), cand("b", weight=1), cand("c", weight=1)]
        counts = Counter(ids([strat.select(pool) for _ in range(70)]))
        assert counts == {"a": 50, "b": 10, "c": 10}

    def test_smooth_interleaving(self) -> None:
        strat = WeightedStrategy()
        pool = [cand("a", weight=5), cand("b", weight=1), cand("c", weight=1)]
        seq = ids([strat.select(pool) for _ in range(7)])
        assert seq == ["a", "a", "b", "a", "c", "a", "a"]

    def test_deterministic_across_instances(self) -> None:
        pool = [cand("a", weight=3), cand("b", weight=2), cand("c", weight=1.5)]
        runs = [ids([WeightedStrategy().select(pool)]) for _ in range(3)]
        assert runs[0] == runs[1] == runs[2]
        s1, s2 = WeightedStrategy(), WeightedStrategy()
        assert ids([s1.select(pool) for _ in range(40)]) == ids(
            [s2.select(pool) for _ in range(40)]
        )

    def test_default_weight_and_custom_key(self) -> None:
        strat = WeightedStrategy(weight_key="share", default_weight=2)
        pool = [cand("a", share=4), cand("b"), cand("c", weight=100)]  # c's "weight" ignored
        counts = Counter(ids([strat.select(pool) for _ in range(80)]))
        assert counts == {"a": 40, "b": 20, "c": 20}

    def test_ties_follow_candidate_order(self) -> None:
        strat = WeightedStrategy()
        pool = [cand("x"), cand("y"), cand("z")]
        assert ids([strat.select(pool) for _ in range(6)]) == ["x", "y", "z", "x", "y", "z"]

    def test_unavailable_candidates_excluded_from_distribution(self) -> None:
        strat = WeightedStrategy()
        pool = [cand("a", weight=1), cand("b", weight=100, state=CredentialState.REVOKED)]
        assert set(ids([strat.select(pool) for _ in range(10)])) == {"a"}

    def test_recovery_does_not_starve_or_flood(self) -> None:
        strat = WeightedStrategy()
        a, b = cand("a", weight=1), cand("b", weight=1)
        b_down = cand("b", weight=1, state=CredentialState.COOLDOWN)
        for _ in range(20):
            assert ids([strat.select([a, b_down])]) == ["a"]
        counts = Counter(ids([strat.select([a, b]) for _ in range(20)]))
        assert counts["a"] == counts["b"] == 10

    def test_removed_credentials_are_forgotten(self) -> None:
        strat = WeightedStrategy()
        a, b = cand("a"), cand("b")
        strat.select([a, b])
        strat.select([a])
        assert set(strat._current) == {"a"}

    @pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), "3", None, True, [1], 0.0])
    def test_invalid_candidate_weight_raises(self, bad: object) -> None:
        with pytest.raises(ConfigurationError, match="weight"):
            WeightedStrategy().select([cand("a", weight=bad)])

    @pytest.mark.parametrize("bad", [0, -2.5, float("nan"), "1", True])
    def test_invalid_default_weight_raises(self, bad: Any) -> None:
        with pytest.raises(ConfigurationError, match="default_weight"):
            WeightedStrategy(default_weight=bad)

    @pytest.mark.parametrize("bad", ["", "  ", None, 3])
    def test_invalid_weight_key_raises(self, bad: Any) -> None:
        with pytest.raises(ConfigurationError, match="weight_key"):
            WeightedStrategy(weight_key=bad)

    def test_invalid_weight_does_not_corrupt_state(self) -> None:
        strat = WeightedStrategy()
        good = [cand("a", weight=2), cand("b", weight=1)]
        before = ids([strat.select(good) for _ in range(3)])
        with pytest.raises(ConfigurationError):
            strat.select([cand("a", weight=-1), cand("b")])
        after = ids([strat.select(good) for _ in range(3)])
        assert Counter(before + after) == {"a": 4, "b": 2}

    def test_thread_safe_exact_distribution(self) -> None:
        strat = WeightedStrategy()
        pool = [cand("a", weight=3), cand("b", weight=1)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(lambda _: strat.select(pool), range(400)))
        assert Counter(ids(results)) == {"a": 300, "b": 100}


# --------------------------------------------------------------------------- least used
class TestLeastUsed:
    def test_picks_lowest_total_leases(self) -> None:
        pool = [cand("a", total=5), cand("b", total=2), cand("c", total=9)]
        chosen = LeastUsedStrategy().select(pool)
        assert chosen is not None
        assert chosen.credential_id == "b"

    def test_ties_resolved_by_candidate_order(self) -> None:
        pool = [cand("a", total=3), cand("b", total=1), cand("c", total=1)]
        chosen = LeastUsedStrategy().select(pool)
        assert chosen is not None
        assert chosen.credential_id == "b"
        assert LeastUsedStrategy().select(list(reversed(pool))).credential_id == "c"  # type: ignore[union-attr]

    def test_ignores_unavailable_even_if_least_used(self) -> None:
        pool = [cand("a", total=0, state=CredentialState.RATE_LIMITED), cand("b", total=50)]
        chosen = LeastUsedStrategy().select(pool)
        assert chosen is not None
        assert chosen.credential_id == "b"

    def test_new_credential_with_zero_leases_wins(self) -> None:
        pool = [cand("a", total=10), cand("b", total=10), cand("new")]
        chosen = LeastUsedStrategy().select(pool)
        assert chosen is not None
        assert chosen.credential_id == "new"

    def test_thread_safe(self) -> None:
        strat = LeastUsedStrategy()
        pool = [cand("a", total=4), cand("b", total=1)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(lambda _: strat.select(pool), range(100)))
        assert set(ids(results)) == {"b"}


# --------------------------------------------------------------------------- LRU
class TestLeastRecentlyUsed:
    def test_never_used_preferred(self) -> None:
        pool = [cand("a", last_used=T0), cand("b"), cand("c", last_used=T0 - timedelta(days=9))]
        chosen = LeastRecentlyUsedStrategy().select(pool)
        assert chosen is not None
        assert chosen.credential_id == "b"

    def test_first_never_used_wins_ties(self) -> None:
        pool = [cand("a", last_used=T0), cand("b"), cand("c")]
        chosen = LeastRecentlyUsedStrategy().select(pool)
        assert chosen is not None
        assert chosen.credential_id == "b"

    def test_oldest_last_used_wins(self) -> None:
        pool = [
            cand("a", last_used=T0),
            cand("b", last_used=T0 - timedelta(hours=1)),
            cand("c", last_used=T0 + timedelta(hours=1)),
        ]
        chosen = LeastRecentlyUsedStrategy().select(pool)
        assert chosen is not None
        assert chosen.credential_id == "b"

    def test_equal_timestamps_resolved_by_candidate_order(self) -> None:
        pool = [cand("a", last_used=T0), cand("b", last_used=T0)]
        chosen = LeastRecentlyUsedStrategy().select(pool)
        assert chosen is not None
        assert chosen.credential_id == "a"

    def test_unused_but_unavailable_is_skipped(self) -> None:
        pool = [cand("a", state=CredentialState.REVOKED), cand("b", last_used=T0)]
        chosen = LeastRecentlyUsedStrategy().select(pool)
        assert chosen is not None
        assert chosen.credential_id == "b"

    def test_thread_safe(self) -> None:
        strat = LeastRecentlyUsedStrategy()
        pool = [cand("a", last_used=T0), cand("b", last_used=T0 - timedelta(1))]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(lambda _: strat.select(pool), range(100)))
        assert set(ids(results)) == {"b"}


# --------------------------------------------------------------------------- failover
class TestFailover:
    def test_metadata_priority_lower_first(self) -> None:
        pool = [cand("a", priority=2), cand("b", priority=0), cand("c", priority=1)]
        assert ids([FailoverStrategy().select(pool)]) == ["b"]

    def test_falls_through_priorities_as_states_change(self) -> None:
        strat = FailoverStrategy()

        def pool(sa: CredentialState, sb: CredentialState) -> list[CredentialCandidate]:
            return [
                cand("a", priority=0, state=sa),
                cand("b", priority=1, state=sb),
                cand("c", priority=2),
            ]

        assert ids([strat.select(pool(AVAILABLE, AVAILABLE))]) == ["a"]
        assert ids([strat.select(pool(CredentialState.COOLDOWN, AVAILABLE))]) == ["b"]
        assert ids([strat.select(pool(CredentialState.REVOKED, CredentialState.RATE_LIMITED))]) == [
            "c"
        ]
        # Higher priority recovers and takes over again.
        assert ids([strat.select(pool(AVAILABLE, AVAILABLE))]) == ["a"]

    def test_explicit_mapping_overrides_metadata(self) -> None:
        strat = FailoverStrategy(priorities={"a": 5, "b": 1})
        pool = [cand("a", priority=0), cand("b", priority=9)]
        assert ids([strat.select(pool)]) == ["b"]

    def test_explicit_sequence_order(self) -> None:
        strat = FailoverStrategy(priorities=["c", "a", "b"])
        pool = [cand("a"), cand("b"), cand("c")]
        assert ids([strat.select(pool)]) == ["c"]
        pool_c_down = [cand("a"), cand("b"), cand("c", state=CredentialState.COOLDOWN)]
        assert ids([strat.select(pool_c_down)]) == ["a"]

    def test_unlisted_ranks_after_listed_in_candidate_order(self) -> None:
        strat = FailoverStrategy(priorities=["z"])
        pool = [cand("x"), cand("y"), cand("z", state=CredentialState.COOLDOWN)]
        assert ids([strat.select(pool)]) == ["x"]
        assert ids([strat.select([*pool[:2], cand("z")])]) == ["z"]

    def test_unprioritized_rank_last(self) -> None:
        pool = [cand("a"), cand("b", priority=100)]
        assert ids([FailoverStrategy().select(pool)]) == ["b"]

    def test_default_priority(self) -> None:
        strat = FailoverStrategy(default_priority=5)
        pool = [cand("a"), cand("b", priority=7), cand("c", priority=3)]
        assert ids([strat.select(pool)]) == ["c"]
        assert ids([strat.select(pool[:2])]) == ["a"]

    def test_negative_priorities_allowed(self) -> None:
        pool = [cand("a", priority=0), cand("b", priority=-1)]
        assert ids([FailoverStrategy().select(pool)]) == ["b"]

    def test_ties_resolved_by_candidate_order(self) -> None:
        pool = [cand("a", priority=1), cand("b", priority=1)]
        assert ids([FailoverStrategy().select(pool)]) == ["a"]
        assert ids([FailoverStrategy().select(pool[::-1])]) == ["b"]

    def test_custom_priority_key(self) -> None:
        strat = FailoverStrategy(priority_key="rank")
        pool = [cand("a", priority=0, rank=2), cand("b", priority=9, rank=1)]
        assert ids([strat.select(pool)]) == ["b"]

    @pytest.mark.parametrize("bad", [1.5, "1", None, True, [1], float("nan")])
    def test_invalid_metadata_priority_raises(self, bad: object) -> None:
        with pytest.raises(ConfigurationError, match="riority"):
            FailoverStrategy().select([cand("a", priority=bad), cand("b", priority=1)])

    @pytest.mark.parametrize("bad", [1.5, "1", None, True])
    def test_invalid_mapping_priority_raises(self, bad: Any) -> None:
        with pytest.raises(ConfigurationError, match="riority"):
            FailoverStrategy(priorities={"a": bad})

    @pytest.mark.parametrize("bad", [1.5, "1", True])
    def test_invalid_default_priority_raises(self, bad: Any) -> None:
        with pytest.raises(ConfigurationError, match="default_priority"):
            FailoverStrategy(default_priority=bad)

    def test_invalid_sequence_configuration_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="Duplicate"):
            FailoverStrategy(priorities=["a", "a"])
        with pytest.raises(ConfigurationError, match="priorities entry"):
            FailoverStrategy(priorities=["a", ""])
        with pytest.raises(ConfigurationError, match="priorities key"):
            FailoverStrategy(priorities={"": 1})
        with pytest.raises(ConfigurationError, match="mapping or a sequence"):
            FailoverStrategy(priorities="abc")

    def test_invalid_priority_key_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="priority_key"):
            FailoverStrategy(priority_key=" ")

    def test_thread_safe(self) -> None:
        strat = FailoverStrategy()
        pool = [cand("a", priority=2), cand("b", priority=1)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(lambda _: strat.select(pool), range(100)))
        assert set(ids(results)) == {"b"}


# --------------------------------------------------------------------------- random
class TestRandom:
    POOL: ClassVar[list[CredentialCandidate]] = [cand("a"), cand("b"), cand("c"), cand("d")]

    def test_same_seed_same_sequence(self) -> None:
        s1, s2 = RandomStrategy(seed=42), RandomStrategy(seed=42)
        seq1 = ids([s1.select(self.POOL) for _ in range(50)])
        seq2 = ids([s2.select(self.POOL) for _ in range(50)])
        assert seq1 == seq2
        assert len(set(seq1)) > 1

    def test_different_seeds_diverge(self) -> None:
        s1, s2 = RandomStrategy(seed=1), RandomStrategy(seed=2)
        a = ids([s1.select(self.POOL) for _ in range(50)])
        b = ids([s2.select(self.POOL) for _ in range(50)])
        assert a != b

    def test_injected_rng_is_used(self) -> None:
        rng = random.Random(7)
        expected_rng = random.Random(7)
        strat = RandomStrategy(rng=rng)
        got = ids([strat.select(self.POOL) for _ in range(10)])
        assert got == [expected_rng.choice(self.POOL).credential_id for _ in range(10)]

    def test_no_global_random_state_used(self) -> None:
        random.seed(0)
        state = random.getstate()
        RandomStrategy().select(self.POOL)
        RandomStrategy(seed=3).select(self.POOL)
        assert random.getstate() == state

    def test_seed_and_rng_conflict(self) -> None:
        with pytest.raises(ConfigurationError, match="either"):
            RandomStrategy(seed=1, rng=random.Random(1))

    def test_rng_type_validated(self) -> None:
        with pytest.raises(ConfigurationError, match=r"random\.Random"):
            RandomStrategy(rng=object())  # type: ignore[arg-type]

    def test_unweighted_ignores_weight_metadata(self) -> None:
        strat = RandomStrategy(seed=5)
        pool = [cand("a", weight=1000), cand("b", weight=1)]
        counts = Counter(ids([strat.select(pool) for _ in range(2000)]))
        assert 800 < counts["b"] < 1200

    def test_weighted_distribution(self) -> None:
        strat = RandomStrategy(seed=11, weighted=True)
        pool = [cand("a", weight=8), cand("b", weight=1), cand("c", weight=1)]
        counts = Counter(ids([strat.select(pool) for _ in range(5000)]))
        assert 3800 < counts["a"] < 4200
        assert 400 < counts["b"] < 600
        assert 400 < counts["c"] < 600

    def test_weighted_is_seed_deterministic(self) -> None:
        pool = [cand("a", weight=2), cand("b", weight=1)]
        s1 = RandomStrategy(seed=9, weighted=True)
        s2 = RandomStrategy(seed=9, weighted=True)
        assert ids([s1.select(pool) for _ in range(30)]) == ids(
            [s2.select(pool) for _ in range(30)]
        )

    def test_weighted_default_weight_and_key(self) -> None:
        strat = RandomStrategy(seed=2, weighted=True, weight_key="w", default_weight=1)
        pool = [cand("a", w=9), cand("b")]
        counts = Counter(ids([strat.select(pool) for _ in range(2000)]))
        assert counts["a"] > 1600

    @pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), "2", None, True])
    def test_weighted_invalid_weight_raises(self, bad: object) -> None:
        with pytest.raises(ConfigurationError, match="weight"):
            RandomStrategy(seed=1, weighted=True).select([cand("a", weight=bad)])

    def test_unweighted_does_not_validate_weights(self) -> None:
        assert RandomStrategy(seed=1).select([cand("a", weight="junk")]) is not None

    def test_invalid_config_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="default_weight"):
            RandomStrategy(default_weight=0)
        with pytest.raises(ConfigurationError, match="weight_key"):
            RandomStrategy(weight_key="")

    def test_only_eligible_ever_selected(self) -> None:
        strat = RandomStrategy(seed=3)
        pool = [cand("a", state=CredentialState.COOLDOWN), cand("b"), cand("c", tags=("x",))]
        ctx = SelectionContext(required_tags=frozenset({"x"}))
        picked = ids([strat.select(pool, ctx) for _ in range(20)])
        assert set(picked) == {"c"}

    def test_thread_safe(self) -> None:
        strat = RandomStrategy(seed=1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(lambda _: strat.select(self.POOL), range(400)))
        assert len(results) == 400
        assert set(ids(results)) == {"a", "b", "c", "d"}
