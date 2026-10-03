"""Round-robin credential selection strategy."""

import threading
from collections.abc import Sequence

from credweave.application.ports.strategy import (
    CredentialCandidate,
    SelectionContext,
    SelectionStrategy,
)


class RoundRobinStrategy(SelectionStrategy):
    """Selects eligible credentials in cyclic round-robin order.

    Thread-safe and deterministic. Automatically skips candidates that are not
    in the AVAILABLE state or do not satisfy selection context requirements.
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
        if not candidates:
            return None

        # Filter candidates by availability and selection context requirements
        eligible: list[CredentialCandidate] = []
        for candidate in candidates:
            if not candidate.is_available:
                continue

            if context is not None and context.required_tags:
                tags = candidate.credential.get_metadata("tags")
                if tags is None:
                    continue
                if isinstance(tags, str):
                    candidate_tags = {tags}
                elif isinstance(tags, (set, frozenset, list, tuple)):
                    candidate_tags = set(tags)
                else:
                    continue

                if not context.required_tags.issubset(candidate_tags):
                    continue

            eligible.append(candidate)

        if not eligible:
            return None

        with self._lock:
            # If no previous selection or single candidate, select the first eligible
            if self._last_selected_id is None or len(eligible) == 1:
                chosen = eligible[0]
                self._last_selected_id = chosen.credential_id
                return chosen

            # Find the position of the last selected credential in the candidates list
            candidate_ids = [c.credential_id for c in candidates]
            if self._last_selected_id in candidate_ids:
                start_index = (candidate_ids.index(self._last_selected_id) + 1) % len(candidates)
                eligible_map = {c.credential_id: c for c in eligible}
                for i in range(len(candidates)):
                    curr_id = candidate_ids[(start_index + i) % len(candidates)]
                    if curr_id in eligible_map:
                        chosen = eligible_map[curr_id]
                        self._last_selected_id = chosen.credential_id
                        return chosen

            # Fallback if last selected credential is no longer in the candidates list
            chosen = eligible[0]
            self._last_selected_id = chosen.credential_id
            return chosen
