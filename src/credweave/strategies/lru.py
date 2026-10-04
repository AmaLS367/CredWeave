"""Least-recently-used credential selection strategy."""

from collections.abc import Sequence

from credweave.application.ports.strategy import (
    CredentialCandidate,
    SelectionContext,
    SelectionStrategy,
)
from credweave.strategies._eligibility import select_eligible


class LeastRecentlyUsedStrategy(SelectionStrategy):
    """Selects the eligible credential that has been idle the longest.

    Credentials that have never been used (``last_used_at is None``) are preferred over
    any used credential. Ties are broken by candidate order. Stateless and thread-safe.
    """

    @property
    def name(self) -> str:
        """Human-readable identifier of the strategy."""
        return "least_recently_used"

    def select(
        self,
        candidates: Sequence[CredentialCandidate],
        context: SelectionContext | None = None,
    ) -> CredentialCandidate | None:
        """Select the least recently used eligible candidate."""
        best: CredentialCandidate | None = None
        for candidate in select_eligible(candidates, context):
            if candidate.last_used_at is None:
                return candidate
            if (
                best is None
                or best.last_used_at is None
                or candidate.last_used_at < best.last_used_at
            ):
                best = candidate
        return best
