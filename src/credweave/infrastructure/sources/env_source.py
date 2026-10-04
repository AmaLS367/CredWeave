"""Environment-variable credential source.

The source is configured with environment variable **names**; the secret values stay in the
environment and are read on demand::

    EnvSource([
        EnvCredential(
            id="openai-primary",
            secrets={"api_key": "OPENAI_API_KEY_PRIMARY"},
            optional_secrets={"org_id": "OPENAI_ORG_PRIMARY"},
            metadata={"tier": "primary"},
        ),
    ])

Every :meth:`EnvSource.get_credentials` call re-reads the environment, so a changed value is
picked up by a running pool. While no value changed, the very same ``Credential`` objects are
returned.

A required variable that is unset, empty or not a string raises
:class:`~credweave.domain.errors.CredentialSourceError`, on construction and on any later read:
an environment has no half-written intermediate state, so there is no last-known-good fallback.
Messages name the credential and the variable, never a value.
"""

import hashlib
import os
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from credweave.application.ports.credential_source import CredentialSource
from credweave.domain.concurrency import MAX_CONCURRENCY_METADATA_KEY, validate_max_concurrency
from credweave.domain.errors import (
    ConfigurationError,
    CredentialAlreadyExistsError,
    CredentialSourceError,
)
from credweave.domain.models import Credential
from credweave.infrastructure.sources._credential_factory import freeze_metadata


def _check_names(owner: str, label: str, mapping: Mapping[str, str]) -> dict[str, str]:
    checked: dict[str, str] = {}
    for secret_name, variable in mapping.items():
        if not isinstance(secret_name, str) or not secret_name:
            raise ConfigurationError(f"{owner}: {label} names must be non-empty strings.")
        if (
            not isinstance(variable, str)
            or not variable.strip()
            or "=" in variable
            or "\0" in variable
        ):
            raise ConfigurationError(
                f"{owner}: {label} {secret_name!r} must map to an environment variable "
                "name (a non-empty string without '=')."
            )
        checked[secret_name] = variable
    return checked


@dataclass(frozen=True)
class EnvCredential:
    """Describes one credential to assemble from environment variables.

    Only environment variable *names* are configured here, never secret values.

    Attributes:
        id: Stable credential identifier (non-empty after stripping).
        secrets: Required secrets, as ``{secret field name: environment variable name}``.
            At least one is required; all must be set.
        optional_secrets: Secrets omitted from the credential when their variable is unset.
        metadata: Static non-secret metadata (for example ``tier`` or ``max_concurrency``).
    """

    id: str
    secrets: Mapping[str, str]
    optional_secrets: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ConfigurationError("EnvCredential id must be a non-empty string.")
        owner = f"EnvCredential {self.id.strip()!r}"
        if not isinstance(self.secrets, Mapping) or not self.secrets:
            raise ConfigurationError(f"{owner}: 'secrets' must be a non-empty mapping.")
        if not isinstance(self.optional_secrets, Mapping):
            raise ConfigurationError(f"{owner}: 'optional_secrets' must be a mapping.")
        if not isinstance(self.metadata, Mapping):
            raise ConfigurationError(f"{owner}: 'metadata' must be a mapping.")

        required = _check_names(owner, "secret", self.secrets)
        optional = _check_names(owner, "optional secret", self.optional_secrets)
        if required.keys() & optional.keys():
            raise ConfigurationError(f"{owner}: a secret cannot be both required and optional.")
        metadata = freeze_metadata(self.metadata)
        if MAX_CONCURRENCY_METADATA_KEY in metadata:
            validate_max_concurrency(
                metadata[MAX_CONCURRENCY_METADATA_KEY],
                f"{owner}: metadata {MAX_CONCURRENCY_METADATA_KEY!r}",
            )

        object.__setattr__(self, "id", self.id.strip())
        object.__setattr__(self, "secrets", MappingProxyType(required))
        object.__setattr__(self, "optional_secrets", MappingProxyType(optional))
        object.__setattr__(self, "metadata", MappingProxyType(metadata))


class EnvSource(CredentialSource):
    """Supplies credentials assembled from environment variables, re-read on every call.

    Args:
        credentials: Descriptions of the credentials to build. Their order is the order of the
            returned credentials; secret fields keep the order of their mapping.
        environ: Mapping to read variables from. ``None`` (default) uses the live
            ``os.environ``; pass a ``dict`` to inject an environment, e.g. in tests. The mapping
            is referenced, not copied, so later changes to it are observed.

    Raises:
        ConfigurationError: A non-``EnvCredential`` item was given.
        CredentialAlreadyExistsError: Two descriptions share an id.
        CredentialSourceError: A required variable is unset, empty or not a string.
    """

    def __init__(
        self,
        credentials: Sequence[EnvCredential],
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        specs = tuple(credentials)
        seen: set[str] = set()
        for spec in specs:
            if not isinstance(spec, EnvCredential):
                raise ConfigurationError(
                    f"EnvSource requires EnvCredential instances, got {type(spec).__name__}."
                )
            if spec.id in seen:
                raise CredentialAlreadyExistsError(spec.id)
            seen.add(spec.id)

        self._specs: tuple[EnvCredential, ...] = specs
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        self._lock = threading.Lock()
        self._built: tuple[tuple[bytes, Credential], ...] = ()
        self._snapshot: tuple[Credential, ...] = ()
        self._read()

    def _read(self) -> tuple[Credential, ...]:
        with self._lock:
            built: list[tuple[bytes, Credential]] = []
            for index, spec in enumerate(self._specs):
                secrets = self._collect(spec)
                digest = self._digest_of(secrets)
                if index < len(self._built) and self._built[index][0] == digest:
                    built.append(self._built[index])  # unchanged: keep the same object
                else:
                    credential = Credential(id=spec.id, secrets=secrets, metadata=spec.metadata)
                    built.append((digest, credential))
            if len(self._built) != len(built) or any(
                new[1] is not old[1] for new, old in zip(built, self._built, strict=True)
            ):
                self._built = tuple(built)
                self._snapshot = tuple(credential for _, credential in built)
            return self._snapshot

    def _collect(self, spec: EnvCredential) -> dict[str, str]:
        secrets: dict[str, str] = {}
        for secret_name, variable in spec.secrets.items():
            value = self._environ.get(variable)
            if not isinstance(value, str):
                problem = "is not set" if value is None else "does not hold a string"
            elif not value.strip():
                problem = "is empty"
            else:
                secrets[secret_name] = value
                continue
            raise CredentialSourceError(
                f"Credential {spec.id!r}: environment variable {variable!r} "
                f"(secret {secret_name!r}) {problem}."
            )
        for secret_name, variable in spec.optional_secrets.items():
            value = self._environ.get(variable)
            if isinstance(value, str) and value.strip():
                secrets[secret_name] = value
        return secrets

    @staticmethod
    def _digest_of(secrets: Mapping[str, str]) -> bytes:
        """Fingerprint of one credential's values; no second plaintext copy is kept."""
        hasher = hashlib.sha256()
        for name, value in secrets.items():
            raw_name = name.encode("utf-8", errors="surrogatepass")
            raw_value = value.encode("utf-8", errors="surrogatepass")
            hasher.update(len(raw_name).to_bytes(4, "big") + raw_name)
            hasher.update(len(raw_value).to_bytes(8, "big") + raw_value)
        return hasher.digest()

    def get_credentials(self) -> Sequence[Credential]:
        """Re-read the environment and return the current credentials."""
        return self._read()

    async def get_credentials_async(self) -> Sequence[Credential]:
        """Asynchronous :meth:`get_credentials` (environment reads never block)."""
        return self._read()

    @property
    def supports_hot_reload(self) -> bool:
        """Environment changes are observed on every read."""
        return True

    def __repr__(self) -> str:
        ids = [spec.id for spec in self._specs]
        return f"{self.__class__.__name__}(credentials={ids!r})"

    __str__ = __repr__
