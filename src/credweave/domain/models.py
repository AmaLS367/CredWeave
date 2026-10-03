"""Domain models representing credentials and active credential leases.

Security Guarantee:
    Secret values stored in :class:`Credential` are never rendered in
    :meth:`Credential.__repr__` or :meth:`Credential.__str__`. All secret
    values are replaced with masked placeholders (``'***'``).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any

from credweave.domain.errors import ConfigurationError, SecretAccessError


class Credential:
    """Generic, immutable representation of an authenticated credential or identity.

    A Credential holds a unique identifier, arbitrary secret values (such as API keys,
    OAuth tokens, or service account keypairs), and arbitrary non-secret metadata
    (such as provider name, account tier, region, or tags).

    Security:
        Secret values are strictly redacted in :meth:`__repr__` and :meth:`__str__`.
        Internal mappings are immutable proxies to prevent accidental in-place
        mutation.

    Equality & Hashing:
        Equality and hashing are strictly based on the credential's unique :attr:`id`.
        In CredWeave, a Credential is an entity whose identity is uniquely determined
        by its identifier within a pool. Two credential instances with identical IDs
        are considered equivalent for hashing, set operations, and dictionary keys.
    """

    __slots__ = ("_id", "_metadata", "_raw_secrets", "_secrets")

    def __init__(
        self,
        id: str,  # noqa: A002
        secrets: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(id, str) or not id.strip():
            raise ConfigurationError("Credential id must be a non-empty string.")

        self._id: str = id.strip()
        # Shallow copy to guard against caller modifying the dict later.
        # Raw secrets are kept in a private read-only proxy. Public secrets property
        # exposes a masked mapping to prevent accidental leakage in logs and debuggers.
        raw_secrets = dict(secrets or {})
        self._raw_secrets: Mapping[str, Any] = MappingProxyType(raw_secrets)
        self._secrets: Mapping[str, str] = MappingProxyType(dict.fromkeys(raw_secrets, "***"))
        self._metadata: Mapping[str, Any] = MappingProxyType(dict(metadata or {}))

    @property
    def id(self) -> str:
        """The stable unique identifier of this credential."""
        return self._id

    @property
    def secrets(self) -> Mapping[str, str]:
        """Read-only mapping of secret keys to masked values ('***').

        All secret values are automatically masked to prevent accidental leakage
        in logs, debuggers, or representations. Use :meth:`get_secret` or
        :meth:`require_secret` to explicitly retrieve raw secret values for execution.
        """
        return self._secrets

    @property
    def secret_keys(self) -> frozenset[str]:
        """Set of available secret key names without revealing their secret values."""
        return frozenset(self._raw_secrets.keys())

    @property
    def metadata(self) -> Mapping[str, Any]:
        """Read-only mapping of non-secret metadata attributes."""
        return self._metadata

    def get_secret(self, key: str, default: Any = None) -> Any:
        """Safely retrieve a secret value by key, returning default if not present."""
        return self._raw_secrets.get(key, default)

    def require_secret(self, key: str) -> Any:
        """Retrieve a secret value by key, raising :exc:`SecretAccessError` if missing."""
        if key not in self._raw_secrets:
            raise SecretAccessError(self._id, key)
        return self._raw_secrets[key]

    def has_secret(self, key: str) -> bool:
        """Return True if the secret key is defined on this credential."""
        return key in self._raw_secrets

    def get_metadata(self, key: str, default: Any = None) -> Any:
        """Retrieve a non-secret metadata value by key, returning default if not present."""
        return self._metadata.get(key, default)

    def has_metadata(self, key: str) -> bool:
        """Return True if the metadata key is defined on this credential."""
        return key in self._metadata

    def __repr__(self) -> str:
        """Return secret-safe string representation with masked secret values."""
        return (
            f"{self.__class__.__name__}("
            f"id={self._id!r}, "
            f"secrets={dict(self._secrets)!r}, "
            f"metadata={dict(self._metadata)!r}"
            f")"
        )

    def __str__(self) -> str:
        """Return secret-safe string representation with masked secret values."""
        return self.__repr__()

    def __eq__(self, other: object) -> bool:
        """Equality based on unique credential identifier."""
        if not isinstance(other, Credential):
            return NotImplemented
        return self._id == other._id

    def __hash__(self) -> int:
        """Hash based on unique credential identifier."""
        return hash((Credential, self._id))


@dataclass(frozen=True)
class Lease:
    """Represents a temporary lease of a credential for a workload execution.

    A lease links an active credential to an execution context. Once the execution
    completes (or fails), the caller reports an :class:`Outcome` along with this
    lease to update pool state and cooldown timers.
    """

    credential: Credential
    lease_id: str
    acquired_at: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.credential, Credential):
            raise ConfigurationError("Lease credential must be an instance of Credential.")
        if not isinstance(self.lease_id, str) or not self.lease_id.strip():
            raise ConfigurationError("Lease lease_id must be a non-empty string.")
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(
                self,
                "metadata",
                MappingProxyType(dict(self.metadata)),
            )

    @property
    def credential_id(self) -> str:
        """Convenience property to access the underlying credential's identifier."""
        return self.credential.id
