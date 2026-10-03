"""Application ports defining interfaces for sources, strategies, stores, and clocks."""

from credweave.application.ports.clock import Clock
from credweave.application.ports.credential_source import CredentialSource
from credweave.application.ports.state_store import CredentialRecord, StateStore
from credweave.application.ports.strategy import (
    CredentialCandidate,
    SelectionContext,
    SelectionStrategy,
)

__all__ = [
    "Clock",
    "CredentialCandidate",
    "CredentialRecord",
    "CredentialSource",
    "SelectionContext",
    "SelectionStrategy",
    "StateStore",
]
