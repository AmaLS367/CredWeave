"""Unit tests for the Lease domain model."""

from datetime import datetime, timezone

import pytest

from credweave.domain.errors import ConfigurationError
from credweave.domain.models import Credential, Lease


def test_lease_creation_and_properties(sample_credential: Credential) -> None:
    """Test valid lease initialization and properties."""
    now = datetime.now(timezone.utc)
    lease = Lease(
        credential=sample_credential,
        lease_id="lease-uuid-12345",
        acquired_at=now,
        metadata={"worker_id": "worker-1"},
    )

    assert lease.credential == sample_credential
    assert lease.credential_id == sample_credential.id
    assert lease.lease_id == "lease-uuid-12345"
    assert lease.acquired_at == now
    assert lease.metadata["worker_id"] == "worker-1"


def test_lease_validation(sample_credential: Credential) -> None:
    """Test validation errors for invalid lease attributes."""
    now = datetime.now(timezone.utc)

    with pytest.raises(ConfigurationError):
        Lease(
            credential="not-a-credential",  # type: ignore[arg-type]
            lease_id="lease-1",
            acquired_at=now,
        )

    with pytest.raises(ConfigurationError):
        Lease(
            credential=sample_credential,
            lease_id="",
            acquired_at=now,
        )

    with pytest.raises(ConfigurationError):
        Lease(
            credential=sample_credential,
            lease_id="   ",
            acquired_at=now,
        )


def test_lease_secret_redaction(sample_credential: Credential) -> None:
    """Test that secret values do not appear in Lease repr."""
    lease = Lease(
        credential=sample_credential,
        lease_id="lease-100",
        acquired_at=datetime.now(timezone.utc),
    )

    repr_str = repr(lease)
    assert "sk-mock-secret-key-12345" not in repr_str
    assert "cs-mock-super-secret-67890" not in repr_str
    assert "lease-100" in repr_str
