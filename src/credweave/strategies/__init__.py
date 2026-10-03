"""Credential selection and scheduling strategies.

Future strategies will include:
    - RoundRobinStrategy
    - RandomStrategy
    - WeightedStrategy
    - LeastUsedStrategy
    - LeastRecentlyUsedStrategy
    - FailoverStrategy
    - QuotaAwareStrategy
"""

from credweave.strategies.base import (
    CredentialCandidate,
    SelectionContext,
    SelectionStrategy,
)

__all__ = [
    "CredentialCandidate",
    "SelectionContext",
    "SelectionStrategy",
]
