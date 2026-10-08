"""Internal security helpers for secret masking, redaction, and safe mappings.

Security Model:
    CredWeave strictly redacts known secret values registered with credentials.
    Pattern-based masking of diagnostic strings (bearer tokens, prefixed API keys,
    key-value pairs) provides best-effort defense-in-depth against accidental
    leakage in logs and representations. Universal redaction of arbitrary freeform
    strings cannot be guaranteed without knowing the secrets, so callers should
    never intentionally interpolate raw secrets into unredacted diagnostic messages.

Clean Architecture:
    Belongs to the domain layer. Pure Python without third-party runtime dependencies.
"""

import hashlib
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
_BEARER_PATTERN = re.compile(r"""(?i)\b(bearer|basic|token)\s+['"]?([a-zA-Z0-9_\-\.~+/]+=*)['"]?""")
_PREFIXED_KEY_PATTERN = re.compile(
    r"\b("
    r"sk-[a-zA-Z0-9_\-]{6,}|"
    r"cs-[a-zA-Z0-9_\-]{6,}|"
    r"gh[pousr]-[a-zA-Z0-9]{8,}|"
    r"gh[pousr]_[a-zA-Z0-9]{8,}|"
    r"github_pat_[a-zA-Z0-9_]{16,}|"
    r"glpat-[a-zA-Z0-9_\-]{8,}|"
    r"glptt-[a-zA-Z0-9_\-]{8,}|"
    r"gloas-[a-zA-Z0-9_\-]{8,}|"
    r"xox[baprs]-[a-zA-Z0-9_\-]{8,}|"
    r"xoxe-[a-zA-Z0-9_\-]{8,}|"
    r"sk-(?:ant|proj|admin)-[a-zA-Z0-9_\-]{10,}|"
    r"(?:sk|rk|pk)_(?:live|test)_[a-zA-Z0-9]{14,}|"
    r"(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}|"
    r"AIza[0-9A-Za-z\-_]{35}|"
    r"hf_[a-zA-Z0-9]{20,}|"
    r"pypi-AgEIcHlwaS5vcmc[a-zA-Z0-9\-_]{20,}|"
    r"eyJ[a-zA-Z0-9_\-]{8,}\.eyJ[a-zA-Z0-9_\-]{8,}\.[a-zA-Z0-9_\-]{8,}"
    r")\b"
)
_KEY_VALUE_PATTERN = re.compile(
    r"""(?i)\b(api[_-]?key|secret|token|password|passwd|pwd)\s*([:=])\s*(?:['"]([^'"]+)['"]|([^\s,;&'"\(\)]+))"""
)


def compute_secrets_fingerprint(raw_secrets: Mapping[str, Any] | None) -> str:
    """Compute a deterministic hash fingerprint of raw secrets without exposing content."""
    if not raw_secrets:
        return "empty"
    hasher = hashlib.sha256()
    for k in sorted(raw_secrets.keys()):
        hasher.update(str(k).encode("utf-8", errors="replace"))
        hasher.update(b"\x00")
        val = raw_secrets[k]
        if isinstance(val, (str, bytes)):
            b_val = val if isinstance(val, bytes) else val.encode("utf-8", errors="replace")
        else:
            b_val = repr(val).encode("utf-8", errors="replace")
        hasher.update(b_val)
        hasher.update(b"\x00")
    return hasher.hexdigest()


def is_sensitive_key(key: object) -> bool:
    """Return True if the key name implies sensitive/secret contents."""
    if not isinstance(key, str):
        return False
    normalized = key.strip().lower().replace("-", "_").replace(".", "_")
    if normalized in {"id", "credential_id", "cred_id"}:
        return False
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

    # 1. Exact raw secret values if provided (longer secrets first to avoid prefix collisions)
    valid_secrets = [s for s in raw_secrets if isinstance(s, str) and len(s) >= 3]
    for secret in sorted(valid_secrets, key=len, reverse=True):
        sanitized = sanitized.replace(secret, "***")

    # 2. Bearer / Basic / Token authorization headers
    sanitized = _BEARER_PATTERN.sub(r"\1 ***", sanitized)

    # 3. Known prefixed token formats (sk-..., github_pat_..., AIza..., etc.)
    sanitized = _PREFIXED_KEY_PATTERN.sub("***", sanitized)

    # 4. Explicit key-value pairs (key="...", token: ...)
    def _replace_kv(m: re.Match[str]) -> str:
        sep = m.group(2)
        return f"{m.group(1)}{sep}***"

    return _KEY_VALUE_PATTERN.sub(_replace_kv, sanitized)


def deep_freeze(
    value: Any,
    raw_secrets: Iterable[str] = (),
) -> Any:
    """Recursively freeze mutable mappings, lists, and sets into immutable, secret-safe types."""
    if isinstance(value, SecretSafeMapping):
        return SecretSafeMapping(value, raw_secrets=raw_secrets)
    if isinstance(value, Mapping):
        return SecretSafeMapping(value, raw_secrets=raw_secrets)
    if isinstance(value, list):
        return tuple(deep_freeze(item, raw_secrets=raw_secrets) for item in value)
    if isinstance(value, tuple):
        return tuple(deep_freeze(item, raw_secrets=raw_secrets) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(deep_freeze(item, raw_secrets=raw_secrets) for item in value)
    return value


def mask_metadata(
    metadata: Mapping[str, Any] | Any,
    raw_secrets: Iterable[str] = (),
) -> Any:
    """Recursively mask sensitive keys and secret values in metadata structures."""
    if isinstance(metadata, Mapping):
        masked: dict[str, Any] = {}
        for k, v in metadata.items():
            if is_sensitive_key(k):
                masked[str(k)] = "***"
            elif isinstance(v, Mapping):
                masked[str(k)] = mask_metadata(v, raw_secrets)
            elif isinstance(v, (list, tuple)):
                masked[str(k)] = [mask_metadata(item, raw_secrets) for item in v]
            elif isinstance(v, (set, frozenset)):
                masked[str(k)] = {mask_metadata(item, raw_secrets) for item in v}
            elif isinstance(v, str):
                masked[str(k)] = mask_secret_text(v, raw_secrets)
            else:
                masked[str(k)] = v
        return masked
    if isinstance(metadata, (list, tuple)):
        return [mask_metadata(item, raw_secrets) for item in metadata]
    if isinstance(metadata, (set, frozenset)):
        return {mask_metadata(item, raw_secrets) for item in metadata}
    if isinstance(metadata, str):
        return mask_secret_text(metadata, raw_secrets)
    return metadata


class SecretSafeMapping(Mapping[str, Any]):
    """Immutable mapping proxy that deep-freezes data and masks secrets in representations."""

    __slots__ = ("_data", "_raw_secrets")

    def __init__(
        self,
        mapping: Mapping[str, Any] | None = None,
        *,
        raw_secrets: Iterable[str] = (),
    ) -> None:
        valid_secrets = {s for s in raw_secrets if isinstance(s, str) and len(s) >= 3}
        if isinstance(mapping, SecretSafeMapping):
            combined = set(mapping._raw_secrets) | valid_secrets
            self._raw_secrets: tuple[str, ...] = tuple(sorted(combined, key=len, reverse=True))
            frozen = {
                k: deep_freeze(v, raw_secrets=self._raw_secrets) for k, v in mapping._data.items()
            }
            self._data: Mapping[str, Any] = MappingProxyType(frozen)
        else:
            self._raw_secrets = tuple(sorted(valid_secrets, key=len, reverse=True))
            frozen = {
                k: deep_freeze(v, raw_secrets=self._raw_secrets) for k, v in (mapping or {}).items()
            }
            self._data = MappingProxyType(frozen)

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
