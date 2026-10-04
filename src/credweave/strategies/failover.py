"""Strict priority-based failover selection strategy."""

from collections.abc import Mapping, Sequence

from credweave.application.ports.strategy import (
    CredentialCandidate,
    SelectionContext,
    SelectionStrategy,
)
from credweave.domain.errors import ConfigurationError
from credweave.strategies._eligibility import (
    lookup_metadata,
    select_eligible,
    validate_metadata_key,
    validate_priority,
)


class FailoverStrategy(SelectionStrategy):
    """Always selects the highest-priority eligible credential.

    Lower priority numbers rank first. A credential's priority is resolved as:

    1. ``priorities`` - an explicit credential-id -> priority mapping, or a sequence of
       credential ids where the position is the priority (first = highest).
    2. The ``priority_key`` metadata value (default ``"priority"``).
    3. ``default_priority`` if set; otherwise the credential ranks after every
       credential that has a priority.

    Credentials with equal priority are ordered by candidate order, so fallback is fully
    deterministic. When a higher-priority credential is not AVAILABLE (cooldown, revoked,
    ...) the next one takes over, and the higher one is picked again once it recovers.
    Priorities must be integers, otherwise
    :class:`~credweave.domain.errors.ConfigurationError` is raised. Stateless and
    thread-safe.
    """

    def __init__(
        self,
        priorities: Mapping[str, int] | Sequence[str] | None = None,
        priority_key: str = "priority",
        default_priority: int | None = None,
    ) -> None:
        self._priority_key = validate_metadata_key(priority_key, "priority_key")
        self._default_priority = (
            None
            if default_priority is None
            else validate_priority(default_priority, "default_priority")
        )
        self._explicit: dict[str, int] = {}
        if isinstance(priorities, Mapping):
            for credential_id, priority in priorities.items():
                self._explicit[validate_metadata_key(credential_id, "priorities key")] = (
                    validate_priority(priority, f"Priority for credential {credential_id!r}")
                )
        elif priorities is not None:
            if isinstance(priorities, str):
                raise ConfigurationError("priorities must be a mapping or a sequence of ids.")
            for position, credential_id in enumerate(priorities):
                validate_metadata_key(credential_id, "priorities entry")
                if credential_id in self._explicit:
                    raise ConfigurationError(
                        f"Duplicate credential id in priorities: {credential_id!r}."
                    )
                self._explicit[credential_id] = position

    @property
    def name(self) -> str:
        """Human-readable identifier of the strategy."""
        return "failover"

    def _priority(self, candidate: CredentialCandidate) -> float:
        explicit = self._explicit.get(candidate.credential_id)
        if explicit is not None:
            return explicit
        found, raw = lookup_metadata(candidate, self._priority_key)
        if found:
            return validate_priority(
                raw,
                f"Priority metadata {self._priority_key!r} on credential "
                f"{candidate.credential_id!r}",
            )
        if self._default_priority is not None:
            return self._default_priority
        return float("inf")

    def select(
        self,
        candidates: Sequence[CredentialCandidate],
        context: SelectionContext | None = None,
    ) -> CredentialCandidate | None:
        """Select the highest-priority eligible candidate."""
        eligible = select_eligible(candidates, context)
        if not eligible:
            return None
        return min(eligible, key=self._priority)
