"""Base interfaces and candidate definitions for selection strategies."""

from credweave.application.ports.strategy import (
    CredentialCandidate,
    SelectionContext,
    SelectionStrategy,
)

__all__ = [
    "CredentialCandidate",
    "SelectionContext",
    "SelectionStrategy",
]
