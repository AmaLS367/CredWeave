"""Internal security helpers for secret masking, redaction, and safe mappings.

Clean Architecture:
    Belongs to the domain layer. Pure Python without third-party runtime dependencies.
"""

import re
from collections.abc import Iterable, Iterator, Mapping
from types import MappingProxyType
from typing import Any

# Key fragments that indicate sensitive metadata attributes
_SENSITIVE_KEY_FRAGMENTS: frozenset[str] = frozenset(
    {
        "secret",
        "token",
        "password",
        "passwd",
        "pwd",
        "auth",
        "credential",
        "private",
        "bearer",
        "signature",
        "cert",
        "cookie",
        "session",
        "apikey",
    }
)

_SENSITIVE_KEY_EXACT: frozenset[str] = frozenset(
    {
        "key",
        "api_key",
        "secret_key",
        "private_key",
        "access_key",
        "client_secret",
        "client_key",
        "auth_key",
        "encryption_key",
        "signing_key",
        "ssh_key",
        "master_key",
    }
)

_SENSITIVE_KEY_PREFIXES: tuple[str, ...] = (
    "api_",
    "secret_",
    "private_",
    "access_",
    "auth_",
    "token_",
    "sign_",
    "signing_",
    "encrypt_",
    "encryption_",
    "license_",
    "ssh_",
    "master_",
)

# Common secret string patterns in diagnostic strings / reasons
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer|basic)\s+([a-zA-Z0-9_\-\.~+/]+=*)")
_PREFIXED_KEY_PATTERN = re.compile(
    r"\b(sk-[a-zA-Z0-9_\-]{6,}|cs-[a-zA-Z0-9_\-]{6,}|gh[pousr]-[a-zA-Z0-9]{8,}|"
    r"glpat-[a-zA-Z0-9_\-]{8,}|xox[baprs]-[a-zA-Z0-9_\-]{8,})\b"
)
_KEY_VALUE_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd)\s*([:=])\s*([^\s,;&'\"]+)"
)


def is_sensitive_key(key: object) -> bool:
    """Return True if the key name implies sensitive/secret contents."""
    if not isinstance(key, str):
        return False
    normalized = key.strip().lower().replace("-", "_").replace(".", "_")
    if normalized in _SENSITIVE_KEY_EXACT:
        return True
    if any(frag in normalized for frag in _SENSITIVE_KEY_FRAGMENTS):
        return True
    return normalized.endswith("_key") and any(
        normalized.startswith(prefix) for prefix in _SENSITIVE_KEY_PREFIXES
    )


def mask_secret_text(text: str | None, raw_secrets: Iterable[str] = ()) -> str | None:
    """Mask known secret values and identifiable secret patterns in text."""
    if text is None:
        return None

    sanitized = text

    # 1. Exact raw secret values if provided
    for secret in raw_secrets:
        if isinstance(secret, str) and len(secret) >= 3:
            sanitized = sanitized.replace(secret, "***")

    # 2. Bearer / Basic authorization headers
    sanitized = _BEARER_PATTERN.sub(r"\1 ***", sanitized)

    # 3. Known prefixed token formats (sk-..., cs-..., ghp-..., etc.)
    sanitized = _PREFIXED_KEY_PATTERN.sub("***", sanitized)

    # 4. Explicit key-value pairs (key=..., token: ...)
    return _KEY_VALUE_PATTERN.sub(r"\1\2***", sanitized)


def mask_metadata(
    metadata: Mapping[str, Any] | Any,
    raw_secrets: Iterable[str] = (),
) -> Any:
    """Recursively mask sensitive keys and secret values in metadata structures."""
    if isinstance(metadata, Mapping):
        masked: dict[str, Any] = {}
        for k, v in metadata.items():
            if is_sensitive_key(k):
                masked[k] = "***"
            elif isinstance(v, Mapping):
                masked[k] = mask_metadata(v, raw_secrets)
            elif isinstance(v, (list, tuple)):
                masked[k] = [mask_metadata(item, raw_secrets) for item in v]
            elif isinstance(v, (set, frozenset)):
                masked[k] = {mask_metadata(item, raw_secrets) for item in v}
            elif isinstance(v, str):
                masked[k] = mask_secret_text(v, raw_secrets)
            else:
                masked[k] = v
        return masked
    if isinstance(metadata, (list, tuple)):
        return [mask_metadata(item, raw_secrets) for item in metadata]
    if isinstance(metadata, (set, frozenset)):
        return {mask_metadata(item, raw_secrets) for item in metadata}
    if isinstance(metadata, str):
        return mask_secret_text(metadata, raw_secrets)
    return metadata


class SecretSafeMapping(Mapping[str, Any]):
    """Immutable mapping proxy that masks secrets and sensitive keys in representations."""

    __slots__ = ("_data", "_raw_secrets")

    def __init__(
        self,
        mapping: Mapping[str, Any] | None = None,
        *,
        raw_secrets: Iterable[str] = (),
    ) -> None:
        valid_secrets = {s for s in raw_secrets if isinstance(s, str) and len(s) >= 3}
        if isinstance(mapping, SecretSafeMapping):
            self._data: Mapping[str, Any] = mapping._data
            combined = set(mapping._raw_secrets) | valid_secrets
            self._raw_secrets: tuple[str, ...] = tuple(sorted(combined, key=len, reverse=True))
        else:
            self._data = MappingProxyType(dict(mapping or {}))
            self._raw_secrets = tuple(sorted(valid_secrets, key=len, reverse=True))

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return repr(mask_metadata(self._data, raw_secrets=self._raw_secrets))

    def __str__(self) -> str:
        return self.__repr__()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return dict(self._data) == dict(other)
        return False

    def get_masked(self) -> dict[str, Any]:
        """Return a dictionary copy with all sensitive fields and secrets masked."""
        return mask_metadata(self._data, raw_secrets=self._raw_secrets)  # type: ignore[no-any-return]
