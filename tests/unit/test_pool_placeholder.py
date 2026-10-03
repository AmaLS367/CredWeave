"""Unit tests for the CredentialPool placeholder in v0.1.0."""

from datetime import datetime, timezone

import pytest

from credweave.application.services.pool import CredentialPool
from credweave.domain.errors import ConfigurationError
from credweave.domain.models import Credential, Lease
from credweave.domain.outcomes import Outcome
from credweave.infrastructure.clocks.system import SystemClock


def test_credential_pool_initialization(sample_credential: Credential) -> None:
    """Test initializing CredentialPool with credentials and optional ports."""
    clock = SystemClock()
    pool = CredentialPool(credentials=[sample_credential], clock=clock)

    assert pool.initial_credentials == (sample_credential,)
    assert pool.clock is clock
    assert pool.source is None
    assert pool.strategy is None
    assert pool.store is None


def test_credential_pool_empty_initialization_error() -> None:
    """Test that initializing with neither credentials nor source raises ConfigurationError."""
    with pytest.raises(ConfigurationError):
        CredentialPool()


@pytest.mark.asyncio
async def test_credential_pool_not_implemented_async(sample_credential: Credential) -> None:
    """Test that acquire() and report() raise explicit NotImplementedError in v0.1.0."""
    pool = CredentialPool(credentials=[sample_credential])
    lease = Lease(
        credential=sample_credential,
        lease_id="test-lease-id",
        acquired_at=datetime.now(timezone.utc),
    )
    outcome = Outcome.success()

    with pytest.raises(NotImplementedError) as exc_acquire:
        await pool.acquire()
    assert "not yet implemented" in str(exc_acquire.value)

    with pytest.raises(NotImplementedError) as exc_report:
        await pool.report(lease, outcome)
    assert "not yet implemented" in str(exc_report.value)


def test_credential_pool_not_implemented_sync(sample_credential: Credential) -> None:
    """Test that acquire_sync() and report_sync() raise explicit NotImplementedError in v0.1.0."""
    pool = CredentialPool(credentials=[sample_credential])
    lease = Lease(
        credential=sample_credential,
        lease_id="test-lease-id",
        acquired_at=datetime.now(timezone.utc),
    )
    outcome = Outcome.success()

    with pytest.raises(NotImplementedError) as exc_acquire:
        pool.acquire_sync()
    assert "not yet implemented" in str(exc_acquire.value)

    with pytest.raises(NotImplementedError) as exc_report:
        pool.report_sync(lease, outcome)
    assert "not yet implemented" in str(exc_report.value)
