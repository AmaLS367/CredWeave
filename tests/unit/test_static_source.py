"""Unit tests for the StaticSource adapter."""

import pytest

from credweave.application.ports.credential_source import CredentialSource
from credweave.domain.errors import ConfigurationError, CredentialAlreadyExistsError
from credweave.domain.models import Credential
from credweave.infrastructure.sources.static import StaticSource


def test_static_source_protocol_conformance(sample_credential: Credential) -> None:
    """Verify StaticSource satisfies CredentialSource Protocol."""
    source = StaticSource([sample_credential])
    assert isinstance(source, CredentialSource)
    assert source.supports_hot_reload is False


def test_static_source_sync_and_async(sample_credentials: list[Credential]) -> None:
    """Verify synchronous and asynchronous retrieval return identical tuples."""
    source = StaticSource(sample_credentials)

    sync_creds = source.get_credentials()
    assert sync_creds == tuple(sample_credentials)

    # Empty source
    empty = StaticSource()
    assert empty.get_credentials() == ()


@pytest.mark.asyncio
async def test_static_source_async_retrieval(sample_credentials: list[Credential]) -> None:
    """Verify asynchronous retrieval."""
    source = StaticSource(sample_credentials)
    async_creds = await source.get_credentials_async()
    assert async_creds == tuple(sample_credentials)


def test_static_source_validation_errors(sample_credential: Credential) -> None:
    """Verify non-credential items and duplicate IDs raise errors."""
    with pytest.raises(ConfigurationError):
        StaticSource(["invalid"])  # type: ignore[list-item]

    duplicate = Credential(id=sample_credential.id, secrets={"key": "val"})
    with pytest.raises(CredentialAlreadyExistsError):
        StaticSource([sample_credential, duplicate])
