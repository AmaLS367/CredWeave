"""Unit tests for JsonSource parsing, validation and basic reading."""

import json
from pathlib import Path
from typing import Any

import pytest

from credweave import CredentialSource, CredentialSourceError, JsonSource


def write(path: Path, document: Any) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def valid_document() -> dict[str, Any]:
    return {
        "credentials": [
            {
                "id": "primary",
                "secrets": {"api_key": "pk-test-1", "client_secret": "cs-test-1"},
                "metadata": {"tier": "primary", "tags": ["a", "b"], "limits": {"rpm": 5}},
            },
            {"id": "backup", "secrets": {"api_key": "bk-test-1"}},
        ]
    }


def test_loads_valid_file(tmp_path: Path) -> None:
    source = JsonSource(write(tmp_path / "creds.json", valid_document()))
    creds = source.get_credentials()
    assert [c.id for c in creds] == ["primary", "backup"]
    assert creds[0].require_secret("client_secret") == "cs-test-1"
    assert creds[0].get_metadata("tags") == ("a", "b")
    assert creds[0].get_metadata("limits")["rpm"] == 5
    with pytest.raises(TypeError):
        creds[0].get_metadata("limits")["rpm"] = 6
    assert creds[1].metadata == {}
    assert source.supports_hot_reload is True
    assert isinstance(source, CredentialSource)
    assert source.reload_status.generation == 1
    assert source.reload_status.ok


def test_accepts_str_path_bom_and_empty_list(tmp_path: Path) -> None:
    path = tmp_path / "creds.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"credentials": []}).encode())
    source = JsonSource(str(path))
    assert source.get_credentials() == ()
    assert source.path == str(path)


def test_ids_are_stripped_and_order_preserved(tmp_path: Path) -> None:
    doc = {
        "credentials": [{"id": " z ", "secrets": {"k": "v1"}}, {"id": "a", "secrets": {"k": "v2"}}]
    }
    source = JsonSource(write(tmp_path / "c.json", doc))
    assert [c.id for c in source.get_credentials()] == ["z", "a"]


async def test_async_matches_sync(tmp_path: Path) -> None:
    source = JsonSource(write(tmp_path / "c.json", valid_document()))
    sync = source.get_credentials()
    assert await source.get_credentials_async() is sync
    status = await source.refresh_async()
    assert status.generation == 1


def test_missing_file_fails_initial_load(tmp_path: Path) -> None:
    with pytest.raises(CredentialSourceError, match="cannot be read"):
        JsonSource(tmp_path / "absent.json")


def test_directory_fails_initial_load(tmp_path: Path) -> None:
    with pytest.raises(CredentialSourceError):
        JsonSource(tmp_path)


def entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"id": "x", "secrets": {"api_key": "secret-value-1"}}
    base.update(overrides)
    return base


MALFORMED: list[tuple[str, Any, str]] = [
    ("top-level-list", [], "top level must be"),
    ("top-level-string", "creds", "top level must be"),
    ("unknown-top-level", {"credentials": [], "extra": 1}, "unsupported fields"),
    ("missing-credentials", {}, "'credentials' must be a list"),
    ("credentials-not-list", {"credentials": {}}, "'credentials' must be a list"),
    ("entry-not-object", {"credentials": ["x"]}, r"credentials\[0\]: must be an object"),
    ("unknown-entry-field", {"credentials": [entry(extra=1)]}, "unsupported fields"),
    ("typo-secret", {"credentials": [{"id": "x", "secret": {"a": "b"}}]}, "unsupported fields"),
    ("missing-id", {"credentials": [{"secrets": {"a": "b"}}]}, "'id' must be"),
    ("empty-id", {"credentials": [entry(id="  ")]}, "'id' must be"),
    ("numeric-id", {"credentials": [entry(id=5)]}, "'id' must be"),
    ("missing-secrets", {"credentials": [{"id": "x"}]}, "'secrets' must be"),
    ("empty-secrets", {"credentials": [entry(secrets={})]}, "'secrets' must be"),
    ("secrets-list", {"credentials": [entry(secrets=["s"])]}, "'secrets' must be"),
    ("secret-int", {"credentials": [entry(secrets={"k": 12345})]}, "'secrets' must be"),
    ("secret-null", {"credentials": [entry(secrets={"k": None})]}, "'secrets' must be"),
    ("secret-empty", {"credentials": [entry(secrets={"k": ""})]}, "'secrets' must be"),
    ("secret-nested", {"credentials": [entry(secrets={"k": {"a": "b"}})]}, "'secrets' must be"),
    ("secret-empty-name", {"credentials": [entry(secrets={"": "v"})]}, "'secrets' must be"),
    ("metadata-list", {"credentials": [entry(metadata=["m"])]}, "'metadata' must be"),
    ("metadata-null", {"credentials": [entry(metadata=None)]}, "'metadata' must be"),
    (
        "duplicate-ids",
        {"credentials": [entry(), entry(secrets={"k": "other-secret-2"})]},
        r"credentials\[1\].*duplicate credential id 'x'",
    ),
    (
        "duplicate-ids-after-strip",
        {"credentials": [entry(), entry(id=" x ")]},
        "duplicate credential id",
    ),
    (
        "bad-max-concurrency",
        {"credentials": [entry(metadata={"max_concurrency": 0})]},
        "max_concurrency",
    ),
    (
        "bad-max-concurrency-type",
        {"credentials": [entry(metadata={"max_concurrency": "5"})]},
        "max_concurrency",
    ),
]


@pytest.mark.parametrize(
    ("document", "pattern"), [(d, p) for _, d, p in MALFORMED], ids=[n for n, _, _ in MALFORMED]
)
def test_malformed_documents_fail_initial_load(tmp_path: Path, document: Any, pattern: str) -> None:
    with pytest.raises(CredentialSourceError, match=pattern):
        JsonSource(write(tmp_path / "c.json", document))


@pytest.mark.parametrize(
    ("content", "pattern"),
    [
        pytest.param(b"", "invalid JSON", id="empty"),
        (b"{not json", "invalid JSON at line 1"),
        (b'{"credentials": [}', "invalid JSON"),
        (b'{"credentials": [], "credentials": []}', "duplicate key"),
        (b'{"credentials": [{"id": "x", "secrets": {"k": "a", "k": "b"}}]}', "duplicate key"),
        (b'{"credentials": [], "n": NaN}', "NaN"),
        (b'{"credentials": [], "n": Infinity}', "NaN"),
        (b"\xff\xfe\x00bad", "UTF-8"),
        pytest.param(b"[" * 100_000, "nesting is too deep", id="deep-nesting"),
    ],
)
def test_malformed_content_fails_initial_load(tmp_path: Path, content: bytes, pattern: str) -> None:
    path = tmp_path / "c.json"
    path.write_bytes(content)
    with pytest.raises(CredentialSourceError, match=pattern):
        JsonSource(path)


def test_oversized_file_rejected(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(CredentialSourceError, match="byte limit"):
        JsonSource(path)


@pytest.mark.parametrize("bad", [-1.0, float("inf"), float("nan"), True, "1"])
def test_invalid_min_check_interval(tmp_path: Path, bad: Any) -> None:
    from credweave import ConfigurationError

    with pytest.raises(ConfigurationError):
        JsonSource(write(tmp_path / "c.json", valid_document()), min_check_interval=bad)
