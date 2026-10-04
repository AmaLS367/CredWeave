"""Round-robin credential selection strategy."""

import threading
from collections.abc import Sequence

from credweave.application.ports.strategy import (
    CredentialCandidate,
    SelectionContext,
    SelectionStrategy,
)
from credweave.strategies._eligibility import select_eligible


class RoundRobinStrategy(SelectionStrategy):
    """Selects eligible credentials in cyclic round-robin order.

    Thread-safe and deterministic. Candidates are filtered through the shared eligibility
    rules (AVAILABLE state, required tags, preferred metadata). If the previously selected
    credential has left the candidate list, rotation restarts from the first eligible one.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_selected_id: str | None = None

    @property
    def name(self) -> str:
        """Human-readable identifier of the strategy."""
        return "round_robin"

    def select(
        self,
        candidates: Sequence[CredentialCandidate],
        context: SelectionContext | None = None,
    ) -> CredentialCandidate | None:
        """Select the next eligible candidate according to round-robin order."""
        eligible = select_eligible(candidates, context)
        if not eligible:
            return None

        eligible_by_id = {c.credential_id: c for c in eligible}
        candidate_ids = [c.credential_id for c in candidates]

        with self._lock:
            chosen = eligible[0]
            # Resume the cycle right after the last pick, wrapping around the full candidate
            # list so ineligible or removed credentials never disturb the rotation order.
            if self._last_selected_id in candidate_ids:
                start = candidate_ids.index(self._last_selected_id) + 1
                for offset in range(len(candidate_ids)):
                    found = eligible_by_id.get(candidate_ids[(start + offset) % len(candidate_ids)])
                    if found is not None:
                        chosen = found
                        break
            self._last_selected_id = chosen.credential_id
            return chosen
