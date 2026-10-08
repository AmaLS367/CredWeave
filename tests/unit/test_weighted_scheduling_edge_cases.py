"""Deterministic unit tests for weighted scheduling edge cases and overflow prevention."""

from collections import Counter

from credweave.application.ports.strategy import CredentialCandidate
from credweave.domain.enums import CredentialState
from credweave.domain.models import Credential
from credweave.strategies.random_strategy import RandomStrategy
from credweave.strategies.weighted import WeightedStrategy


def _cand(
    cid: str,
    weight: float,
    state: CredentialState = CredentialState.AVAILABLE,
) -> CredentialCandidate:
    cred = Credential(id=cid, secrets={"k": "s"}, metadata={"weight": weight})
    return CredentialCandidate(credential=cred, state=state)


def test_huge_weights_do_not_overflow() -> None:
    """Weights near float maximum (1e308) do not overflow to infinity or produce NaN."""
    strat = WeightedStrategy()
    # sum(weights) would be 3e308 -> overflows IEEE 754 float without scaling
    pool = [_cand("a", 1e308), _cand("b", 1e308), _cand("c", 1e308)]

    picks = [strat.select(pool) for _ in range(30)]
    assert all(p is not None for p in picks)
    counts = Counter(p.credential_id for p in picks if p is not None)
    assert counts == {"a": 10, "b": 10, "c": 10}


def test_subnormal_tiny_weights_do_not_underflow() -> None:
    """Subnormal weights (e.g. 1e-300) are scheduled according to exact proportions."""
    strat = WeightedStrategy()
    pool = [_cand("a", 2e-300), _cand("b", 1e-300)]

    picks = [strat.select(pool) for _ in range(30)]
    assert all(p is not None for p in picks)
    counts = Counter(p.credential_id for p in picks if p is not None)
    assert counts == {"a": 20, "b": 10}


def test_extreme_weight_disparity() -> None:
    """Extreme weight ratios (1e15 vs 1.0) function deterministically."""
    strat = WeightedStrategy()
    pool = [_cand("dominant", 1e12), _cand("minor", 1.0)]

    # In 100 picks, dominant should be selected virtually every time
    picks = [strat.select(pool) for _ in range(100)]
    counts = Counter(p.credential_id for p in picks if p is not None)
    assert counts["dominant"] >= 99


def test_long_running_drift_invariance() -> None:
    """Smooth round-robin preserves exact proportions over 20,000 rounds without float drift."""
    strat = WeightedStrategy()
    pool = [_cand("a", 5.0), _cand("b", 3.0), _cand("c", 2.0)]

    picks = [strat.select(pool) for _ in range(20000)]
    counts = Counter(p.credential_id for p in picks if p is not None)
    # Total ratio: 5:3:2 over 20,000 rounds = 10,000 : 6,000 : 4,000
    assert counts == {"a": 10000, "b": 6000, "c": 4000}


def test_random_strategy_weighted_huge_weights() -> None:
    """RandomStrategy with weighted=True handles huge weights without OverflowError."""
    strat = RandomStrategy(seed=42, weighted=True)
    pool = [_cand("a", 1e308), _cand("b", 1e308)]

    picks = [strat.select(pool) for _ in range(100)]
    assert all(p is not None for p in picks)
    counts = Counter(p.credential_id for p in picks if p is not None)
    assert counts["a"] > 30
    assert counts["b"] > 30


def test_returning_candidate_after_long_absence() -> None:
    """Candidate that was down for thousands of selections resumes smoothly without starving."""
    strat = WeightedStrategy()
    a = _cand("a", 1.0)
    b_down = _cand("b", 1.0, state=CredentialState.COOLDOWN)
    b_active = _cand("b", 1.0, state=CredentialState.AVAILABLE)

    # 5,000 rounds where b is down
    for _ in range(5000):
        strat.select([a, b_down])

    # Now b recovers
    resumed_picks = [strat.select([a, b_active]) for _ in range(100)]
    counts = Counter(p.credential_id for p in resumed_picks if p is not None)
    # Both should get 50
    assert counts == {"a": 50, "b": 50}
