"""Unit tests for the reusable FileReloader engine and JsonSource reload semantics."""

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from credweave import ConfigurationError, CredentialSourceError, FileReloader, JsonSource
from tests.conftest import TestClock


def doc(*secrets: str, ids: tuple[str, ...] | None = None) -> bytes:
    ids = ids or tuple(f"c{i}" for i in range(len(secrets)))
    return json.dumps(
        {
            "credentials": [
                {"id": i, "secrets": {"api_key": s}} for i, s in zip(ids, secrets, strict=True)
            ]
        }
    ).encode()


def replace_atomically(path: Path, content: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(content)
    os.replace(tmp, path)


def age(path: Path, seconds: float = 60.0) -> None:
    """Move the mtime into the past so the racy-timestamp guard does not apply."""
    past = time.time_ns() - int(seconds * 1e9)
    os.utime(path, ns=(past, past))


class CountingParser:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, data: bytes) -> bytes:
        self.calls += 1
        if data.startswith(b"BAD"):
            raise CredentialSourceError("bad content")
        return data


def test_unchanged_file_is_not_reread(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"one")
    age(path)
    parser = CountingParser()
    reloader = FileReloader(path, parser)
    first = reloader.get()
    for _ in range(5):
        assert reloader.get() is first
    assert parser.calls == 1
    assert reloader.status.generation == 1


def test_modification_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"one")
    age(path)
    reloader = FileReloader(path, CountingParser())
    path.write_bytes(b"twotwo")
    assert reloader.get() == b"twotwo"
    assert reloader.status.generation == 2


def test_atomic_replace_with_same_size_and_mtime_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"AAAA")
    age(path)
    reloader = FileReloader(path, CountingParser())
    old = path.stat().st_mtime_ns
    replace_atomically(path, b"BBBB")
    os.utime(path, ns=(old, old))  # same size, same mtime: only the inode differs
    assert reloader.get() == b"BBBB"


def test_same_size_rewrite_within_one_mtime_tick_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"AAAA")
    reloader = FileReloader(path, CountingParser())  # read right after write: racy window
    stamp = path.stat().st_mtime_ns
    with open(path, "r+b") as handle:  # in place: same inode, same size
        handle.write(b"BBBB")
    os.utime(path, ns=(stamp, stamp))  # identical fingerprint
    assert reloader.get() == b"BBBB"


def test_touch_without_content_change_keeps_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"same")
    age(path)
    parser = CountingParser()
    reloader = FileReloader(path, parser)
    first = reloader.get()
    os.utime(path)  # new mtime, same bytes
    assert reloader.get() is first
    assert reloader.status.generation == 1
    assert parser.calls == 1


def test_bad_reload_keeps_last_known_good_and_reports(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"good")
    age(path)
    clock = TestClock()
    reloader = FileReloader(path, CountingParser(), clock=clock)
    good = reloader.get()
    path.write_bytes(b"BAD content")
    assert reloader.get() is good
    status = reloader.status
    assert not status.ok
    assert status.last_error is not None
    assert "bad content" in status.last_error
    assert status.last_error_at == clock.now()
    assert status.consecutive_failures == 1
    assert status.generation == 1


def test_bad_file_is_not_reparsed_until_it_changes(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"good")
    age(path)
    parser = CountingParser()
    reloader = FileReloader(path, parser)
    path.write_bytes(b"BAD one")
    age(path)
    for _ in range(4):
        reloader.get()
    assert parser.calls == 2
    assert reloader.status.consecutive_failures == 1


def test_recovery_after_failure(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"good")
    age(path)
    reloader = FileReloader(path, CountingParser())
    path.write_bytes(b"BAD one")
    reloader.get()
    path.write_bytes(b"fixed!!")
    assert reloader.get() == b"fixed!!"
    assert reloader.status.ok
    assert reloader.status.generation == 2
    assert reloader.status.consecutive_failures == 0


def test_reverting_to_served_content_clears_the_error_without_new_generation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"good")
    age(path)
    reloader = FileReloader(path, CountingParser())
    first = reloader.get()
    path.write_bytes(b"BAD one")
    reloader.get()
    assert not reloader.status.ok
    path.write_bytes(b"good")
    assert reloader.get() is first
    assert reloader.status.ok
    assert reloader.status.generation == 1


def test_missing_file_midway_keeps_snapshot_and_is_retried(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"good")
    age(path)
    reloader = FileReloader(path, CountingParser())
    first = reloader.get()
    path.unlink()
    assert reloader.get() is first
    assert reloader.status.consecutive_failures == 1
    assert reloader.get() is first
    assert reloader.status.consecutive_failures == 2  # retried every call
    path.write_bytes(b"good")  # same content restored: no new generation
    assert reloader.get() is first
    assert reloader.status.ok
    assert reloader.status.generation == 1


def test_unexpected_parser_exception_is_reduced_to_type_name(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"ok")

    def parser(data: bytes) -> bytes:
        if data == b"boom":
            raise ValueError("secret-in-message")
        return data

    reloader = FileReloader(path, parser)
    path.write_bytes(b"boom")
    reloader.refresh()
    assert reloader.status.last_error is not None
    assert "ValueError" in reloader.status.last_error
    assert "secret-in-message" not in reloader.status.last_error


def test_initial_failure_raises(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"BAD")
    with pytest.raises(CredentialSourceError, match="Failed to load"):
        FileReloader(path, CountingParser())


def test_min_check_interval_defers_stat(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"one")
    age(path)
    clock = TestClock()
    reloader = FileReloader(path, CountingParser(), clock=clock, min_check_interval=10.0)
    path.write_bytes(b"twotwo")
    assert reloader.get() == b"one"  # within the interval
    clock.advance(10.0)
    assert reloader.get() == b"twotwo"
    path.write_bytes(b"three!")
    assert reloader.refresh(force=True).generation == 3


def test_force_rereads_but_dedupes_identical_content(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"one")
    age(path)
    parser = CountingParser()
    reloader = FileReloader(path, parser)
    reloader.refresh(force=True)
    assert parser.calls == 1
    assert reloader.status.generation == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_check_interval": -1},
        {"min_check_interval": float("nan")},
        {"max_bytes": 0},
        {"max_bytes": True},
    ],
)
def test_invalid_arguments(tmp_path: Path, kwargs: dict[str, Any]) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"x")
    with pytest.raises(ConfigurationError):
        FileReloader(path, CountingParser(), **kwargs)


async def test_async_refresh_and_get(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"one")
    age(path)
    reloader = FileReloader(path, CountingParser())
    path.write_bytes(b"twotwo")
    assert await reloader.get_async() == b"twotwo"
    assert (await reloader.refresh_async()).generation == 2


def test_json_source_last_known_good_end_to_end(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_bytes(doc("sk-one"))
    age(path)
    source = JsonSource(path)
    good = source.get_credentials()
    path.write_bytes(b'{"credentials": [{"id": "c0", "secrets": {"api_key": "sk-two"')  # torn write
    assert source.get_credentials() is good
    assert not source.reload_status.ok
    assert "invalid JSON" in (source.reload_status.last_error or "")
    replace_atomically(path, doc("sk-two"))
    fresh = source.get_credentials()
    assert fresh[0].require_secret("api_key") == "sk-two"
    assert source.reload_status.ok
    assert source.reload_status.generation == 2
    assert source.reload().generation == 2


def test_json_source_rejects_schema_violation_on_reload(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_bytes(doc("sk-one"))
    age(path)
    source = JsonSource(path)
    good = source.get_credentials()
    path.write_bytes(doc("sk-a", "sk-b", ids=("same", "same")))
    assert source.get_credentials() is good
    assert "duplicate credential id" in (source.reload_status.last_error or "")


def test_symlink_swap_is_detected(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")  # pragma: no cover
    one, two = tmp_path / "one.json", tmp_path / "two.json"
    one.write_bytes(doc("sk-one"))
    two.write_bytes(doc("sk-two"))
    link = tmp_path / "current.json"
    try:
        link.symlink_to(one)
    except OSError:
        pytest.skip("symlink creation not permitted")  # pragma: no cover
    source = JsonSource(link)
    assert source.get_credentials()[0].require_secret("api_key") == "sk-one"
    link.unlink()
    link.symlink_to(two)
    assert source.get_credentials()[0].require_secret("api_key") == "sk-two"
