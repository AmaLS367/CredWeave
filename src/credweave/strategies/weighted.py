"""Deterministic smooth weighted round-robin selection strategy."""

import threading
from collections.abc import Sequence

from credweave.application.ports.strategy import (
    CredentialCandidate,
    SelectionContext,
    SelectionStrategy,
)
from credweave.strategies._eligibility import (
    candidate_weight,
    select_eligible,
    validate_metadata_key,
    validate_weight,
)


class WeightedStrategy(SelectionStrategy):
    """Allocates traffic proportionally to credential weights, deterministically.

    Uses smooth weighted round-robin: over any full cycle each credential is chosen in
    proportion to its weight and picks are interleaved rather than bursty. Weights are
    read from credential metadata under ``weight_key`` (default ``"weight"``); missing
    weights use ``default_weight``. Weights must be finite numbers greater than 0,
    otherwise :class:`~credweave.domain.errors.ConfigurationError` is raised.

    Ties are broken by candidate order. Thread-safe.
    """

    def __init__(self, weight_key: str = "weight", default_weight: float = 1.0) -> None:
        self._weight_key = validate_metadata_key(weight_key, "weight_key")
        self._default_weight = validate_weight(default_weight, "default_weight")
        self._lock = threading.Lock()
        self._current: dict[str, float] = {}

    @property
    def name(self) -> str:
        """Human-readable identifier of the strategy."""
        return "weighted"

    def select(
        self,
        candidates: Sequence[CredentialCandidate],
        context: SelectionContext | None = None,
    ) -> CredentialCandidate | None:
        """Select the next candidate according to smooth weighted scheduling."""
        eligible = select_eligible(candidates, context)
        if not eligible:
            return None

        weights = [candidate_weight(c, self._weight_key, self._default_weight) for c in eligible]
        total = sum(weights)

        with self._lock:
            # Forget scheduling state of credentials that left the pool entirely.
            known = {c.credential_id for c in candidates}
            for stale in [cid for cid in self._current if cid not in known]:
                del self._current[stale]

            best_index = 0
            best_value = float("-inf")
            for index, (cand, weight) in enumerate(zip(eligible, weights, strict=True)):
                value = self._current.get(cand.credential_id, 0.0) + weight
                self._current[cand.credential_id] = value
                if value > best_value:
                    best_index, best_value = index, value

            chosen = eligible[best_index]
            self._current[chosen.credential_id] -= total
            return chosen
