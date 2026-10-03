"""Static in-memory credential source adapter."""

from collections.abc import Sequence

from credweave.application.ports.credential_source import CredentialSource
from credweave.domain.errors import ConfigurationError, CredentialAlreadyExistsError
from credweave.domain.models import Credential


class StaticSource(CredentialSource):
    """Supplies a fixed in-memory sequence of credentials to a pool.

    Attributes:
        supports_hot_reload: Always False for static sources.
    """

    def __init__(self, credentials: Sequence[Credential] | None = None) -> None:
        creds = list(credentials or ())
        seen_ids: set[str] = set()
        for c in creds:
            if not isinstance(c, Credential):
                raise ConfigurationError(
                    f"StaticSource requires Credential instances, got {type(c).__name__}."
                )
            if c.id in seen_ids:
                raise CredentialAlreadyExistsError(c.id)
            seen_ids.add(c.id)

        self._credentials: tuple[Credential, ...] = tuple(creds)

    def get_credentials(self) -> Sequence[Credential]:
        """Load and return all credentials synchronously."""
        return self._credentials

    async def get_credentials_async(self) -> Sequence[Credential]:
        """Load and return all credentials asynchronously."""
        return self._credentials

    @property
    def supports_hot_reload(self) -> bool:
        """Static sources do not support dynamic reloading."""
        return False
