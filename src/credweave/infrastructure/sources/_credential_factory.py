"""Private helpers shared by the dynamic credential sources.

Sources build :class:`~credweave.domain.models.Credential` objects from external data. The
helpers here validate and freeze that data and only ever raise secret-safe messages: they
mention credential ids, never secret values.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from credweave.domain.concurrency import MAX_CONCURRENCY_METADATA_KEY, validate_max_concurrency
from credweave.domain.errors import ConfigurationError, CredentialSourceError


def freeze_value(value: Any) -> Any:
    """Recursively freeze JSON-like containers: lists become tuples, dicts read-only proxies."""
    if isinstance(value, Mapping):
        return MappingProxyType({k: freeze_value(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_value(v) for v in value)
    return value


def freeze_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``metadata`` whose nested containers are immutable."""
    return {key: freeze_value(value) for key, value in metadata.items()}


def check_max_concurrency(metadata: Mapping[str, Any], credential_id: str) -> None:
    """Validate the ``max_concurrency`` metadata override at load time.

    The pool validates this key on every acquire and would raise if a reloaded credential
    carried a bad value, so a source must reject it before publishing the snapshot.

    Raises:
        CredentialSourceError: The override is not ``None`` or an integer of at least 1.
    """
    if MAX_CONCURRENCY_METADATA_KEY not in metadata:
        return
    try:
        validate_max_concurrency(metadata[MAX_CONCURRENCY_METADATA_KEY], "max_concurrency")
    except ConfigurationError:
        valid = False
    else:
        valid = True
    if not valid:
        raise CredentialSourceError(
            f"Credential {credential_id!r}: metadata {MAX_CONCURRENCY_METADATA_KEY!r} "
            "must be a positive integer or null."
        )
