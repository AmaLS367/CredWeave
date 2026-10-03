"""Default system clock adapter using standard library time and datetime."""

import asyncio
import time
from datetime import datetime, timezone

from credweave.application.ports.clock import Clock


class SystemClock(Clock):
    """Standard system clock implementation relying on Python standard library."""

    def now(self) -> datetime:
        """Return the current timezone-aware UTC datetime."""
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        """Return a monotonic clock reading in fractional seconds."""
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        """Suspend execution synchronously for the given duration in seconds."""
        time.sleep(seconds)

    async def sleep_async(self, seconds: float) -> None:
        """Suspend execution asynchronously for the given duration in seconds."""
        await asyncio.sleep(seconds)
