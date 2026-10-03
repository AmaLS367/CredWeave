"""Credential source port for loading and hot-reloading credentials."""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from credweave.domain.models import Credential


@runtime_checkable
class CredentialSource(Protocol):
    """Protocol for sources capable of supplying credentials to a pool.

    Implementations can supply credentials from static configurations,
    environment variables, JSON/YAML files, or cloud secrets managers
    (e.g., AWS Secrets Manager, GCP Secret Manager, HashiCorp Vault).
    """

    def get_credentials(self) -> Sequence[Credential]:
        """Load and return all credentials synchronously from this source."""
        ...

    async def get_credentials_async(self) -> Sequence[Credential]:
        """Load and return all credentials asynchronously from this source."""
        ...

    @property
    def supports_hot_reload(self) -> bool:
        """Whether this source supports dynamic credential reloading or file watching."""
        ...
