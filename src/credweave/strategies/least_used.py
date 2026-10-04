"""Least-used credential selection strategy."""

from collections.abc import Sequence

from credweave.application.ports.strategy import (
    CredentialCandidate,
    SelectionContext,
    SelectionStrategy,
)
from credweave.strategies._eligibility import select_eligible


class LeastUsedStrategy(SelectionStrategy):
    """Selects the eligible credential with the lowest ``total_leases``.

    Ties are broken by candidate order. Stateless and therefore thread-safe.
    """

    @property
    def name(self) -> str:
        """Human-readable identifier of the strategy."""
        return "least_used"

    def select(
        self,
        candidates: Sequence[CredentialCandidate],
        context: SelectionContext | None = None,
    ) -> CredentialCandidate | None:
        """Select the least-used eligible candidate."""
        eligible = select_eligible(candidates, context)
        if not eligible:
            return None
        return min(eligible, key=lambda c: c.total_leases)
