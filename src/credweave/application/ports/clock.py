"""Clock port for deterministic timekeeping, cooldown calculation, and delays."""

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Protocol for timekeeping and sleeps.

    Injecting a clock abstraction allows deterministic testing of cooldowns,
    backoff strategies, and lease expiration without depending on real wall-clock time.
    """

    def now(self) -> datetime:
        """Return the current timezone-aware UTC datetime."""
        ...

    def monotonic(self) -> float:
        """Return a monotonic clock reading in fractional seconds for measuring intervals."""
        ...

    def sleep(self, seconds: float) -> None:
        """Suspend execution synchronously for the given duration in seconds."""
        ...

    async def sleep_async(self, seconds: float) -> None:
        """Suspend execution asynchronously for the given duration in seconds."""
        ...
