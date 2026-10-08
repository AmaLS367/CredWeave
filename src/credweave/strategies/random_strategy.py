import math
import random as _random
import threading
from collections.abc import Sequence

from credweave.application.ports.strategy import (
    CredentialCandidate,
    SelectionContext,
    SelectionStrategy,
)
from credweave.domain.errors import ConfigurationError
from credweave.strategies._eligibility import (
    candidate_weight,
    select_eligible,
    validate_metadata_key,
    validate_weight,
)


class RandomStrategy(SelectionStrategy):
    """Selects a random eligible credential, optionally weighted.

    Each instance owns its random generator (no global ``random`` state). Pass either
    ``seed`` or an ``rng`` (a :class:`random.Random`) for reproducible selection; passing
    both raises :class:`~credweave.domain.errors.ConfigurationError`.

    With ``weighted=True`` the selection probability is proportional to the weight read
    from credential metadata under ``weight_key`` (same validation rules as
    :class:`~credweave.strategies.weighted.WeightedStrategy`). Thread-safe.
    """

    def __init__(
        self,
        *,
        seed: int | None = None,
        rng: _random.Random | None = None,
        weighted: bool = False,
        weight_key: str = "weight",
        default_weight: float = 1.0,
    ) -> None:
        if rng is not None and seed is not None:
            raise ConfigurationError("Provide either 'seed' or 'rng', not both.")
        if rng is not None and not isinstance(rng, _random.Random):
            raise ConfigurationError("rng must be an instance of random.Random.")
        self._rng = rng if rng is not None else _random.Random(seed)
        self._weighted = weighted
        self._weight_key = validate_metadata_key(weight_key, "weight_key")
        self._default_weight = validate_weight(default_weight, "default_weight")
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        """Human-readable identifier of the strategy."""
        return "random"

    def select(
        self,
        candidates: Sequence[CredentialCandidate],
        context: SelectionContext | None = None,
    ) -> CredentialCandidate | None:
        """Select a random eligible candidate."""
        eligible = select_eligible(candidates, context)
        if not eligible:
            return None

        if not self._weighted:
            with self._lock:
                return self._rng.choice(eligible)

        raw_weights = [
            candidate_weight(c, self._weight_key, self._default_weight) for c in eligible
        ]
        max_weight = max(raw_weights)
        if not math.isfinite(sum(raw_weights)) or max_weight > 1e100 or max_weight < 1e-50:
            scale = max_weight if max_weight > 0 else 1.0
            weights = [w / scale for w in raw_weights]
        else:
            weights = raw_weights

        with self._lock:
            return self._rng.choices(eligible, weights=weights, k=1)[0]
