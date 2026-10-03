"""Pytest configuration and shared fixtures for CredWeave tests."""

from datetime import datetime, timedelta, timezone

import pytest

from credweave.application.ports.clock import Clock
from credweave.domain.models import Credential


class TestClock(Clock):
    """Deterministic in-memory Clock implementation for testing time-dependent logic."""

    __test__ = False

    def __init__(self, initial_time: datetime | None = None) -> None:
        self._now = initial_time or datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        self._monotonic = 1000.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    async def sleep_async(self, seconds: float) -> None:
        self.advance(seconds)


@pytest.fixture
def test_clock() -> TestClock:
    """Provide a deterministic TestClock."""
    return TestClock()


@pytest.fixture
def sample_credential() -> Credential:
    """Provide a standard Credential instance for testing."""
    return Credential(
        id="test-credential-1",
        secrets={
            "api_key": "sk-mock-secret-key-12345",
            "client_secret": "cs-mock-super-secret-67890",
        },
        metadata={
            "provider": "openai",
            "tier": "tier-3",
            "region": "us-east-1",
            "tags": ("primary", "prod"),
        },
    )


@pytest.fixture
def sample_credentials() -> list[Credential]:
    """Provide a list of 3 distinct sample credentials."""
    return [
        Credential(
            id="cred-alpha",
            secrets={"api_key": "sk-alpha-111"},
            metadata={"tier": "primary", "tags": ("primary", "fast")},
        ),
        Credential(
            id="cred-beta",
            secrets={"api_key": "sk-beta-222"},
            metadata={"tier": "secondary", "tags": ("secondary", "fast")},
        ),
        Credential(
            id="cred-gamma",
            secrets={"api_key": "sk-gamma-333"},
            metadata={"tier": "backup", "tags": ("backup", "slow")},
        ),
    ]
