"""Pytest configuration and shared fixtures for CredWeave tests."""

import pytest

from credweave.domain.models import Credential


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
        },
    )
