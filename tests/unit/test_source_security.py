"""Security tests: sources must never leak secret values through errors, reprs or logs."""

import json
import logging
import traceback
from pathlib import Path
from typing import Any

import pytest

from credweave import (
    CredentialSourceError,
    EnvCredential,
    EnvSource,
    JsonSource,
    ReloadStatus,
)

SENTINEL = "sk-SENTINEL-do-not-leak-9f8e7d"
SENTINEL_KEY = "sk-SENTINEL-KEY-NAME-1a2b3c"


def full_error_text(exc: BaseException) -> str:
    """Everything observable about an exception, including its whole chain."""
    parts = ["".join(traceback.format_exception(exc)), repr(exc), str(exc), repr(exc.args)]
    chained: BaseException | None = exc
    while chained is not None:
        parts.append(repr(vars(chained)))
        chained = chained.__context__
    return "\n".join(parts)


def assert_clean(exc: BaseException) -> None:
    assert SENTINEL not in full_error_text(exc)
    assert exc.__cause__ is None
    assert exc.__context__ is None


def init_error(path: Path) -> CredentialSourceError:
    with pytest.raises(CredentialSourceError) as info:
        JsonSource(path)
    return info.value


def _raw(template: str) -> bytes:
    return template.replace("@S", SENTINEL).replace("@K", SENTINEL_KEY).encode()


MALFORMED_WITH_SECRETS: list[bytes] = [
    _raw('{"credentials": [{"id": "x", "secrets": {"api_key": "@S"}'),  # truncated
    _raw('{"credentials": [{"id": "x", "secrets": {"api_key": "@S"},}]}'),
    _raw('{"credentials": [{"id": "x", "secrets": {"api_key": "@S", "api_key": "y"}}]}'),
    _raw('{"credentials": [{"id": "x", "secrets": {"api_key": "@S"}, "bogus": 1}]}'),
    _raw('{"credentials": [{"id": "x", "secrets": {"api_key": 123456789, "k": "@S"}}]}'),
    _raw('{"credentials": [{"id": "x", "secrets": {"api_key": ["@S"]}}]}'),
    _raw('{"credentials": [{"id": "x", "secrets": {"@K": "@S"}, "metadata": 7}]}'),
    _raw(
        '{"credentials": [{"id": "x", "secrets": {"a": "@S"}},'
        ' {"id": "x", "secrets": {"a": "@S"}}]}'
    ),
    _raw('{"credentials": "@S"}'),
    _raw('"@S"'),
    _raw('{"credentials": [], "@K": "@S"}'),
    _raw(
        '{"credentials": [{"id": "x", "secrets": {"a": "@S"},'
        ' "metadata": {"max_concurrency": "@S"}}]}'
    ),
    _raw('{"credentials": [{"id": "x", "secrets": {"a": "@S"}}], "n": NaN}'),
    bytes([0xFF]) + SENTINEL.encode(),
    SENTINEL.encode(),
]


@pytest.mark.parametrize("content", MALFORMED_WITH_SECRETS)
def test_malformed_files_never_echo_values_on_initial_load(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "c.json"
    path.write_bytes(content)
    exc = init_error(path)
    assert_clean(exc)
    assert SENTINEL_KEY not in full_error_text(exc)


@pytest.mark.parametrize("content", MALFORMED_WITH_SECRETS)
def test_malformed_files_never_echo_values_on_reload(
    tmp_path: Path, content: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"credentials": [{"id": "ok", "secrets": {"k": "valid-1"}}]}))
    source = JsonSource(path)
    path.write_bytes(content)
    with caplog.at_level(logging.DEBUG, logger="credweave"):
        source.get_credentials()
        source.reload()
    status = source.reload_status
    assert not status.ok
    visible = "\n".join(
        [repr(status), str(status), status.last_error or "", repr(source), str(source)]
    )
    visible += "\n".join(r.getMessage() for r in caplog.records)
    visible += caplog.text
    assert SENTINEL not in visible
    assert SENTINEL_KEY not in visible
    assert caplog.records, "a failed reload should be logged"


def test_valid_file_secrets_are_not_in_source_repr_or_status(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"credentials": [{"id": "x", "secrets": {"api_key": SENTINEL}}]}))
    source = JsonSource(path)
    creds = source.get_credentials()
    for text in (repr(source), str(source), repr(source.reload_status), repr(creds), str(creds[0])):
        assert SENTINEL not in text


def test_secret_in_valid_credential_id_collision_message_has_only_id(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    doc: dict[str, Any] = {
        "credentials": [
            {"id": "same", "secrets": {"k": SENTINEL}},
            {"id": "same", "secrets": {"k": SENTINEL}},
        ]
    }
    path.write_text(json.dumps(doc))
    exc = init_error(path)
    assert "same" in str(exc)
    assert_clean(exc)


ENV = {"API_KEY_VAR": SENTINEL}


def test_env_source_repr_and_str_do_not_expose_values() -> None:
    spec = EnvCredential(id="x", secrets={"api_key": "API_KEY_VAR"}, metadata={"tier": "t"})
    source = EnvSource([spec], environ=dict(ENV))
    for text in (repr(source), str(source), repr(spec), str(spec), repr(source.get_credentials())):
        assert SENTINEL not in text
    assert "API_KEY_VAR" in repr(spec)  # names are fine, values are not


@pytest.mark.parametrize("bad_value", ["", "   ", None, 12345])
def test_env_validation_errors_never_show_values(bad_value: object) -> None:
    spec_a = EnvCredential(id="a", secrets={"api_key": "API_KEY_VAR", "other": "OTHER_VAR"})
    env: dict[str, Any] = {"API_KEY_VAR": SENTINEL, "OTHER_VAR": bad_value}
    if bad_value is None:
        del env["OTHER_VAR"]
    with pytest.raises(CredentialSourceError) as info:
        EnvSource([spec_a], environ=env)
    assert_clean(info.value)
    assert "OTHER_VAR" in str(info.value)
    assert "12345" not in str(info.value)


def test_env_error_after_rotation_never_shows_current_value() -> None:
    env = dict(ENV)
    source = EnvSource([EnvCredential(id="a", secrets={"k": "API_KEY_VAR"})], environ=env)
    env["API_KEY_VAR"] = ""
    with pytest.raises(CredentialSourceError) as info:
        source.get_credentials()
    assert_clean(info.value)


def test_reload_status_repr_is_value_free() -> None:
    status = ReloadStatus(generation=2, loaded_at=None, last_error="x", consecutive_failures=1)
    assert SENTINEL not in repr(status)
