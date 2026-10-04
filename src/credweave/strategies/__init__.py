"""Credential selection and scheduling strategies.

Provided strategies:
    - RoundRobinStrategy: Cyclic rotation among eligible credentials
    - WeightedStrategy: Deterministic smooth weighted scheduling
    - LeastUsedStrategy: Lowest total lease count first
    - LeastRecentlyUsedStrategy: Longest-idle credential first
    - FailoverStrategy: Strict priority ordering with deterministic fallback
    - RandomStrategy: Random (optionally weighted, seedable) selection
"""

from credweave.strategies.base import (
    CredentialCandidate,
    SelectionContext,
    SelectionStrategy,
)
from credweave.strategies.failover import FailoverStrategy
from credweave.strategies.least_used import LeastUsedStrategy
from credweave.strategies.lru import LeastRecentlyUsedStrategy
from credweave.strategies.random_strategy import RandomStrategy
from credweave.strategies.round_robin import RoundRobinStrategy
from credweave.strategies.weighted import WeightedStrategy

__all__ = [
    "CredentialCandidate",
    "FailoverStrategy",
    "LeastRecentlyUsedStrategy",
    "LeastUsedStrategy",
    "RandomStrategy",
    "RoundRobinStrategy",
    "SelectionContext",
    "SelectionStrategy",
    "WeightedStrategy",
]
