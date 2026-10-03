"""Unit tests for the Clock port and SystemClock implementation."""

from datetime import timezone

import pytest

from credweave.application.ports.clock import Clock
from credweave.infrastructure.clocks.system import SystemClock


def test_system_clock_protocol_conformance() -> None:
    """Verify SystemClock adheres to the Clock runtime protocol."""
    clock = SystemClock()
    assert isinstance(clock, Clock)


def test_system_clock_now_and_monotonic() -> None:
    """Verify now() returns a UTC datetime and monotonic() advances."""
    clock = SystemClock()
    t1 = clock.now()
    m1 = clock.monotonic()

    assert t1.tzinfo == timezone.utc
    assert isinstance(m1, float)

    clock.sleep(0.01)

    t2 = clock.now()
    m2 = clock.monotonic()

    assert t2 >= t1
    assert m2 >= m1


@pytest.mark.asyncio
async def test_system_clock_sleep_async() -> None:
    """Verify sleep_async suspends execution without error."""
    clock = SystemClock()
    m1 = clock.monotonic()
    await clock.sleep_async(0.01)
    m2 = clock.monotonic()
    assert m2 >= m1
