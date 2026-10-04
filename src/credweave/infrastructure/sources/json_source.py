"""JSON file credential source with dynamic reload (standard library only).

Canonical schema::

    {
      "credentials": [
        {
          "id": "primary",
          "secrets": {"api_key": "<your-api-key>"},
          "metadata": {"tier": "primary", "max_concurrency": 2}
        }
      ]
    }

Rules (anything else is rejected):

* The document is one JSON object whose only field is ``credentials``, a list (possibly empty).
* Each credential is an object with ``id`` (non-empty string; surrounding whitespace is
  stripped and ids must be unique after that), ``secrets`` (required, non-empty object of
  string names to non-empty string values) and ``metadata`` (optional object of string names
  to any JSON value; lists become tuples, objects become read-only mappings). A
  ``max_concurrency`` metadata value must be a positive integer or ``null``.
* Unknown fields, duplicate keys inside any JSON object, ``NaN``/``Infinity`` and non-UTF-8
  content are errors. The file must not exceed 1 MiB.

Reload behavior: every ``get_credentials()`` call ``stat``-s the file and re-reads it only when
it changed (see :mod:`credweave.infrastructure.sources.reloading`). If a reload fails the
previously loaded credentials keep being served and :attr:`JsonSource.reload_status` describes
the problem. Only the initial load raises.

Security: validation errors identify entries by position (``credentials[1]``) and, once
validated, by credential id. They never contain secret values or any other file content, and
are raised without exception chaining so the offending document is unreachable from them.
"""

import json
import os
from collections.abc import Sequence
from typing import Any, NoReturn

from credweave.application.ports.clock import Clock
from credweave.application.ports.credential_source import CredentialSource
from credweave.domain.errors import CredentialSourceError
from credweave.domain.models import Credential
from credweave.infrastructure.sources._credential_factory import (
    check_max_concurrency,
    freeze_metadata,
)
from credweave.infrastructure.sources.reloading import FileReloader, ReloadStatus

_TOP_LEVEL_FIELDS = ("credentials",)
_CREDENTIAL_FIELDS = ("id", "secrets", "metadata")


class _DuplicateKeyError(Exception):
    """Internal marker for a repeated key inside one JSON object (carries no data)."""


class _NonFiniteError(Exception):
    """Internal marker for NaN/Infinity constants (carries no data)."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _reject_constant(_name: str) -> NoReturn:
    raise _NonFiniteError


def _fail(message: str) -> NoReturn:
    raise CredentialSourceError(message)


def _parse_document(data: bytes) -> Any:
    # Errors are raised *after* the except blocks so the parser's exception, whose attributes
    # hold the document, is never attached as ``__context__`` of the error that escapes.
    problem: str
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        problem = "content is not valid UTF-8."
    else:
        try:
            return json.loads(
                text,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
            )
        except _DuplicateKeyError:
            problem = "invalid JSON: an object contains a duplicate key."
        except _NonFiniteError:
            problem = "invalid JSON: NaN and Infinity are not allowed."
        except json.JSONDecodeError as exc:
            # Only the position is reported; the parser's message and document are dropped.
            problem = f"invalid JSON at line {exc.lineno}, column {exc.colno}."
        except RecursionError:
            problem = "invalid JSON: nesting is too deep."
    raise CredentialSourceError(problem)


def _parse_credentials(data: bytes) -> tuple[Credential, ...]:
    """Parse and strictly validate a credentials document into immutable credentials."""
    document = _parse_document(data)
    if not isinstance(document, dict):
        _fail("top level must be a JSON object with a 'credentials' list.")
    if any(key not in _TOP_LEVEL_FIELDS for key in document):
        _fail("top level has unsupported fields; the only allowed field is 'credentials'.")
    entries = document.get("credentials")
    if not isinstance(entries, list):
        _fail("'credentials' must be a list.")

    credentials: list[Credential] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        credential = _parse_entry(index, entry)
        if credential.id in seen:
            _fail(f"credentials[{index}]: duplicate credential id {credential.id!r}.")
        seen.add(credential.id)
        credentials.append(credential)
    return tuple(credentials)


def _parse_entry(index: int, entry: Any) -> Credential:
    where = f"credentials[{index}]"
    if not isinstance(entry, dict):
        _fail(f"{where}: must be an object.")
    if any(key not in _CREDENTIAL_FIELDS for key in entry):
        _fail(f"{where}: has unsupported fields; allowed fields are 'id', 'secrets', 'metadata'.")

    raw_id = entry.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        _fail(f"{where}: 'id' must be a non-empty string.")
    credential_id = raw_id.strip()
    where = f"credentials[{index}] ({credential_id!r})"

    secrets = entry.get("secrets")
    if (
        not isinstance(secrets, dict)
        or not secrets
        or not all(isinstance(k, str) and k for k in secrets)
        or not all(isinstance(v, str) and v for v in secrets.values())
    ):
        _fail(
            f"{where}: 'secrets' must be a non-empty object of string names to "
            "non-empty string values."
        )

    metadata = entry.get("metadata", {})
    if not isinstance(metadata, dict) or not all(isinstance(k, str) for k in metadata):
        _fail(f"{where}: 'metadata' must be an object with string names.")
    frozen_metadata = freeze_metadata(metadata)
    check_max_concurrency(frozen_metadata, credential_id)

    return Credential(id=credential_id, secrets=secrets, metadata=frozen_metadata)


class JsonSource(CredentialSource):
    """Loads credentials from a JSON file and picks up changes without restarting the pool.

    See the module documentation for the canonical schema. The file is loaded eagerly: a
    missing, unreadable or malformed file raises :class:`CredentialSourceError` from the
    constructor. Afterwards every :meth:`get_credentials` call checks the file and serves the
    newest valid contents; a bad edit or a half-written file never discards the last good
    credentials (inspect :attr:`reload_status` for the error).

    Safe for concurrent use from threads and asyncio tasks. Replace the file atomically
    (write a temporary file, then ``os.replace``) for the cleanest rotation; in-place edits are
    also safe because intermediate states that fail validation are simply skipped.

    Args:
        path: Path of the JSON file.
        clock: Optional clock for status timestamps and ``min_check_interval``.
        min_check_interval: Minimum seconds between file checks (``0`` = check on every call).
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        clock: Clock | None = None,
        min_check_interval: float = 0.0,
    ) -> None:
        self._reloader: FileReloader[tuple[Credential, ...]] = FileReloader(
            path,
            _parse_credentials,
            name="credentials file",
            clock=clock,
            min_check_interval=min_check_interval,
        )

    @property
    def path(self) -> str:
        """The watched file path."""
        return self._reloader.path

    @property
    def reload_status(self) -> ReloadStatus:
        """Generation and last reload error, without touching the file."""
        return self._reloader.status

    def get_credentials(self) -> Sequence[Credential]:
        """Return the newest valid credentials, reloading the file first if it changed."""
        return self._reloader.get()

    async def get_credentials_async(self) -> Sequence[Credential]:
        """Asynchronous :meth:`get_credentials`; file work runs off the event loop."""
        return await self._reloader.get_async()

    def refresh(self) -> ReloadStatus:
        """Check the file now and return the resulting status (never raises)."""
        return self._reloader.refresh()

    async def refresh_async(self) -> ReloadStatus:
        """Asynchronous :meth:`refresh`."""
        return await self._reloader.refresh_async()

    def reload(self) -> ReloadStatus:
        """Re-read the file now, skipping the change check, and return the status."""
        return self._reloader.refresh(force=True)

    @property
    def supports_hot_reload(self) -> bool:
        """JSON sources pick up file changes on every read."""
        return True

    def __repr__(self) -> str:
        status = self._reloader.status
        return (
            f"{self.__class__.__name__}(path={self._reloader.path!r}, "
            f"generation={status.generation}, ok={status.ok})"
        )

    __str__ = __repr__
