"""Reusable pull-based reload engine for file-backed credential sources.

:class:`FileReloader` owns everything about *keeping a parsed snapshot of a file fresh* so a
concrete source (JSON today, YAML later) only has to supply a ``parser`` turning the file's
bytes into an immutable value.

How it works
    * **Pull-based.** There is no background thread. Every :meth:`FileReloader.refresh`
      ``stat``-s the file and only re-reads it when its fingerprint
      (``mtime_ns``, size, inode, device) changed. An atomic ``rename``/``os.replace`` swap
      changes the inode, so it is detected even when size and mtime happen to match.
    * **Racy timestamps.** A file rewritten with the same size inside one coarse mtime tick
      leaves an unchanged fingerprint. For a short window after each read the file is
      therefore re-read and compared by SHA-256 digest, so such a rewrite is still seen. Content
      that is byte-identical to the last read never produces a new snapshot.
    * **Last known good.** A failure to read or parse never replaces the published snapshot:
      readers keep getting the previous good value while :attr:`FileReloader.status` reports the
      error. Only the *initial* load raises. A file that failed to parse is not re-parsed until
      it changes again; a file that could not be read at all (e.g. missing in the middle of a
      non-atomic replace) is retried on the next call.
    * **Thread and asyncio safe.** Refreshes are serialised by one lock and the snapshot is an
      immutable value swapped atomically, so concurrent readers always observe a complete
      snapshot and the generation never goes backwards. The async API runs the blocking file
      work in a worker thread so the event loop is never stalled by I/O.

Security
    Parsers must raise :class:`~credweave.domain.errors.CredentialSourceError` with a message
    that never contains file content. Any other exception is reduced to its type name. Nothing
    from the file is ever logged.
"""

import asyncio
import hashlib
import logging
import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Generic, TypeVar

from credweave.application.ports.clock import Clock
from credweave.domain._security import mask_secret_text
from credweave.domain.errors import ConfigurationError, CredentialSourceError
from credweave.infrastructure.clocks.system import SystemClock

T = TypeVar("T")

DEFAULT_MAX_BYTES = 1024 * 1024
"""Default upper bound on the size of a reloadable file."""

_RACY_WINDOW_NS = 2_000_000_000
"""A file whose mtime is this close to the moment it was read may be rewritten undetected within
the same timestamp tick (FAT has 2 s granularity), so it is re-checked by content digest."""

_logger = logging.getLogger("credweave.sources")

_Fingerprint = tuple[int, int, int, int]


@dataclass(frozen=True)
class ReloadStatus:
    """Secret-safe snapshot of a reloadable source's state.

    Attributes:
        generation: Number of distinct snapshots published so far (1 after the initial load).
        loaded_at: When the currently served snapshot was published.
        last_error: Description of the latest failed reload, or ``None`` when the latest
            reload attempt succeeded. While set, the previous good snapshot is still served.
            It never contains file content.
        last_error_at: When ``last_error`` was recorded.
        consecutive_failures: Failed reload attempts since the last success.
    """

    generation: int
    loaded_at: datetime | None
    last_error: str | None = None
    last_error_at: datetime | None = None
    consecutive_failures: int = 0

    @property
    def ok(self) -> bool:
        """True when the latest reload attempt succeeded (no pending error)."""
        return self.last_error is None

    def __repr__(self) -> str:
        """Return secret-safe string representation."""
        masked_error = mask_secret_text(self.last_error) if self.last_error is not None else None
        return (
            f"{self.__class__.__name__}("
            f"generation={self.generation!r}, "
            f"loaded_at={self.loaded_at!r}, "
            f"last_error={masked_error!r}, "
            f"last_error_at={self.last_error_at!r}, "
            f"consecutive_failures={self.consecutive_failures!r}"
            f")"
        )

    def __str__(self) -> str:
        return self.__repr__()


@dataclass(frozen=True)
class _Attempt:
    """What the engine last read from the file, whether or not it parsed."""

    fingerprint: _Fingerprint
    digest: bytes
    suspect: bool  # read within the racy window of the file's mtime
    parsed: bool


def _fingerprint(st: os.stat_result) -> _Fingerprint:
    return (st.st_mtime_ns, st.st_size, st.st_ino, st.st_dev)


def _is_suspect(st: os.stat_result, read_at_ns: int) -> bool:
    return abs(read_at_ns - st.st_mtime_ns) < _RACY_WINDOW_NS


def _os_reason(exc: OSError) -> str:
    reason = exc.strerror or type(exc).__name__
    return reason[:200]


class FileReloader(Generic[T]):
    """Keeps an immutable parsed snapshot of one file fresh, with last-known-good semantics.

    Args:
        path: File to watch. Symlinks are followed on every check, so symlink-swap rotation
            (for example Kubernetes secret volumes) is detected.
        parser: Turns the file's bytes into an immutable snapshot. Must raise
            :class:`~credweave.domain.errors.CredentialSourceError` with a secret-safe message
            on invalid content.
        name: Human-readable description used in messages.
        clock: Clock for status timestamps and ``min_check_interval``; defaults to the system
            clock.
        min_check_interval: Minimum seconds between two ``stat`` checks. ``0`` (the default)
            checks the file on every call.
        max_bytes: Files larger than this are rejected without being parsed.

    Raises:
        ConfigurationError: ``min_check_interval`` or ``max_bytes`` is invalid.
        CredentialSourceError: The initial load failed.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        parser: Callable[[bytes], T],
        *,
        name: str = "credentials file",
        clock: Clock | None = None,
        min_check_interval: float = 0.0,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        if (
            isinstance(min_check_interval, bool)
            or not isinstance(min_check_interval, (int, float))
            or not math.isfinite(min_check_interval)
            or min_check_interval < 0
        ):
            raise ConfigurationError("min_check_interval must be a finite number of seconds >= 0.")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ConfigurationError("max_bytes must be an integer of at least 1.")

        self._path = os.fspath(path)
        self._parser = parser
        self._name = name
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._min_check_interval = float(min_check_interval)
        self._max_bytes = max_bytes

        self._lock = threading.Lock()
        self._snapshot: T | None = None
        self._attempt: _Attempt | None = None
        self._served_digest: bytes | None = None
        self._last_check: float | None = None
        self._status = ReloadStatus(generation=0, loaded_at=None)

        with self._lock:
            self._refresh_locked(force=True)
        if self._snapshot is None:
            raise CredentialSourceError(f"Failed to load {self._status.last_error}")

    @property
    def path(self) -> str:
        """The watched file path."""
        return self._path

    @property
    def snapshot(self) -> T:
        """The latest good snapshot, without checking the file."""
        snapshot = self._snapshot
        assert snapshot is not None  # guaranteed by the eager initial load
        return snapshot

    @property
    def status(self) -> ReloadStatus:
        """The current reload status, without checking the file."""
        return self._status

    def refresh(self, *, force: bool = False) -> ReloadStatus:
        """Pick up file changes, never raising once an initial snapshot exists.

        Args:
            force: Skip the ``stat`` fast path and ``min_check_interval`` and re-read the file.
                Content identical to the last read still does not create a new snapshot.
        """
        with self._lock:
            self._refresh_locked(force=force)
            return self._status

    async def refresh_async(self, *, force: bool = False) -> ReloadStatus:
        """Asynchronous :meth:`refresh`; the file work runs in a worker thread."""
        return await asyncio.to_thread(self.refresh, force=force)

    def get(self) -> T:
        """Refresh, then return the latest good snapshot."""
        self.refresh()
        return self.snapshot

    async def get_async(self) -> T:
        """Asynchronous :meth:`get`."""
        await self.refresh_async()
        return self.snapshot

    # ------------------------------------------------------------------ internals

    def _refresh_locked(self, *, force: bool) -> None:
        if self._skip_check(force):
            return
        try:
            st = os.stat(self._path)
        except OSError as exc:
            self._attempt = None
            self._record_failure(f"cannot be read ({_os_reason(exc)})")
            return

        attempt = self._attempt
        if (
            not force
            and attempt is not None
            and attempt.fingerprint == _fingerprint(st)
            and not attempt.suspect
        ):
            return
        self._read_and_parse()

    def _skip_check(self, force: bool) -> bool:
        """Apply ``min_check_interval``; a performed check restarts the interval."""
        if self._min_check_interval <= 0:
            return False
        now = self._clock.monotonic()
        last = self._last_check
        if not force and last is not None and 0 <= now - last < self._min_check_interval:
            return True
        self._last_check = now
        return False

    def _read_and_parse(self) -> None:
        try:
            with open(self._path, "rb") as handle:
                # fstat of the open handle describes exactly the bytes read below.
                st = os.fstat(handle.fileno())
                data = handle.read(self._max_bytes + 1)
        except OSError as exc:
            self._attempt = None
            self._record_failure(f"cannot be read ({_os_reason(exc)})")
            return

        digest = hashlib.sha256(data).digest()
        fingerprint = _fingerprint(st)
        suspect = _is_suspect(st, time.time_ns())
        previous = self._attempt

        if digest == self._served_digest:
            # The file holds exactly what is being served (e.g. restored after a failure):
            # nothing to publish, any pending error is resolved.
            self._attempt = _Attempt(fingerprint, digest, suspect, parsed=True)
            self._clear_failure()
            return

        if previous is not None and previous.digest == digest:
            # Same bytes as last time: keep the existing outcome (served snapshot or recorded
            # error) and just remember the fresh fingerprint.
            self._attempt = _Attempt(fingerprint, digest, suspect, previous.parsed)
            return

        if len(data) > self._max_bytes:
            self._attempt = _Attempt(fingerprint, digest, suspect, parsed=False)
            self._record_failure(f"is larger than the {self._max_bytes} byte limit")
            return

        try:
            value = self._parser(data)
        except CredentialSourceError as exc:
            message = str(exc)
        except Exception as exc:
            message = f"could not be parsed ({type(exc).__name__})"
        else:
            self._attempt = _Attempt(fingerprint, digest, suspect, parsed=True)
            self._served_digest = digest
            self._publish(value)
            return

        self._attempt = _Attempt(fingerprint, digest, suspect, parsed=False)
        self._record_failure(message)

    def _publish(self, value: T) -> None:
        recovered = self._status.last_error is not None
        self._snapshot = value
        self._status = ReloadStatus(
            generation=self._status.generation + 1,
            loaded_at=self._clock.now(),
        )
        if self._status.generation > 1:
            _logger.info(
                "credweave: reloaded %s %r (generation %d%s)",
                self._name,
                self._path,
                self._status.generation,
                ", recovered from an earlier failure" if recovered else "",
            )

    def _clear_failure(self) -> None:
        if self._status.last_error is None:
            return
        self._status = ReloadStatus(
            generation=self._status.generation, loaded_at=self._status.loaded_at
        )
        _logger.info("credweave: %s %r is valid again", self._name, self._path)

    def _record_failure(self, message: str) -> None:
        sanitized_msg = mask_secret_text(message) or message
        full = f"{self._name} {self._path!r}: {sanitized_msg}"
        previous = self._status
        self._status = ReloadStatus(
            generation=previous.generation,
            loaded_at=previous.loaded_at,
            last_error=full,
            last_error_at=self._clock.now(),
            consecutive_failures=previous.consecutive_failures + 1,
        )
        if previous.last_error != full and self._snapshot is not None:
            _logger.warning(
                "credweave: reload of %s failed, keeping the last known good credentials: %s",
                self._name,
                sanitized_msg,
            )
