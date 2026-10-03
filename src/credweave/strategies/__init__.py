"""Credential selection and scheduling strategies.

Provided strategies:
    - RoundRobinStrategy: Cyclic rotation among eligible credentials
"""

from credweave.strategies.base import (
    CredentialCandidate,
    SelectionContext,
    SelectionStrategy,
)
from credweave.strategies.round_robin import RoundRobinStrategy

__all__ = [
    "CredentialCandidate",
    "RoundRobinStrategy",
    "SelectionContext",
    "SelectionStrategy",
]
