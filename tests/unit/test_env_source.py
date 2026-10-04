"""Unit tests for EnvSource and EnvCredential."""

import pytest

from credweave import (
    ConfigurationError,
    CredentialAlreadyExistsError,
    CredentialSource,
    CredentialSourceError,
    EnvCredential,
    EnvSource,
)


def make_specs() -> list[EnvCredential]:
    return [
        EnvCredential(
            id="primary",
            secrets={"api_key": "PRIMARY_KEY", "client_secret": "PRIMARY_SECRET"},
            optional_secrets={"org_id": "PRIMARY_ORG"},
            metadata={"tier": "primary", "tags": ["a", "b"]},
        ),
        EnvCredential(id="backup", secrets={"api_key": "BACKUP_KEY"}),
    ]


def make_env() -> dict[str, str]:
    return {
        "PRIMARY_KEY": "pk-test-1",
        "PRIMARY_SECRET": "ps-test-1",
        "BACKUP_KEY": "bk-test-1",
    }


def test_builds_multiple_credentials_in_order() -> None:
    source = EnvSource(make_specs(), environ=make_env())
    creds = source.get_credentials()
    assert [c.id for c in creds] == ["primary", "backup"]
    assert list(creds[0].secret_keys) != []
    assert creds[0].require_secret("api_key") == "pk-test-1"
    assert creds[0].require_secret("client_secret") == "ps-test-1"
    assert creds[1].require_secret("api_key") == "bk-test-1"


def test_metadata_is_attached_and_frozen() -> None:
    creds = EnvSource(make_specs(), environ=make_env()).get_credentials()
    assert creds[0].get_metadata("tier") == "primary"
    assert creds[0].get_metadata("tags") == ("a", "b")
    assert creds[1].metadata == {}


def test_optional_secret_present_and_absent() -> None:
    env = make_env()
    source = EnvSource(make_specs(), environ=env)
    assert not source.get_credentials()[0].has_secret("org_id")
    env["PRIMARY_ORG"] = "org-test"
    assert source.get_credentials()[0].require_secret("org_id") == "org-test"
    env["PRIMARY_ORG"] = ""
    assert not source.get_credentials()[0].has_secret("org_id")


def test_defaults_to_live_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CW_TEST_LIVE_KEY", "live-1")
    source = EnvSource([EnvCredential(id="live", secrets={"api_key": "CW_TEST_LIVE_KEY"})])
    assert source.get_credentials()[0].require_secret("api_key") == "live-1"
    monkeypatch.setenv("CW_TEST_LIVE_KEY", "live-2")
    assert source.get_credentials()[0].require_secret("api_key") == "live-2"


def test_rereads_changed_values_without_recreating() -> None:
    env = make_env()
    source = EnvSource(make_specs(), environ=env)
    before = source.get_credentials()
    env["BACKUP_KEY"] = "bk-test-2"
    after = source.get_credentials()
    assert after[1].require_secret("api_key") == "bk-test-2"
    assert after[1] is not before[1]
    assert after[0] is before[0]  # untouched credential keeps its identity


def test_returns_identical_snapshot_while_unchanged() -> None:
    source = EnvSource(make_specs(), environ=make_env())
    assert source.get_credentials() is source.get_credentials()


async def test_async_matches_sync() -> None:
    env = make_env()
    source = EnvSource(make_specs(), environ=env)
    sync = source.get_credentials()
    assert await source.get_credentials_async() is sync
    env["PRIMARY_KEY"] = "pk-test-2"
    fresh = await source.get_credentials_async()
    assert fresh[0].require_secret("api_key") == "pk-test-2"
    assert source.get_credentials() is fresh


def test_protocol_conformance_and_hot_reload_flag() -> None:
    source = EnvSource(make_specs(), environ=make_env())
    assert isinstance(source, CredentialSource)
    assert source.supports_hot_reload is True


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_missing_or_empty_required_variable_raises(bad: str | None) -> None:
    env = make_env()
    if bad is None:
        del env["PRIMARY_SECRET"]
    else:
        env["PRIMARY_SECRET"] = bad
    with pytest.raises(CredentialSourceError, match="PRIMARY_SECRET"):
        EnvSource(make_specs(), environ=env)


def test_variable_removed_after_construction_raises_on_next_read() -> None:
    env = make_env()
    source = EnvSource(make_specs(), environ=env)
    del env["BACKUP_KEY"]
    with pytest.raises(CredentialSourceError, match="BACKUP_KEY"):
        source.get_credentials()
    env["BACKUP_KEY"] = "bk-test-3"
    assert source.get_credentials()[1].require_secret("api_key") == "bk-test-3"


async def test_missing_variable_raises_async() -> None:
    env = make_env()
    source = EnvSource(make_specs(), environ=env)
    del env["PRIMARY_KEY"]
    with pytest.raises(CredentialSourceError):
        await source.get_credentials_async()


def test_non_string_value_raises() -> None:
    env: dict[str, object] = dict(make_env())
    env["BACKUP_KEY"] = 12345
    with pytest.raises(CredentialSourceError, match="string"):
        EnvSource(make_specs(), environ=env)  # type: ignore[arg-type]


def test_duplicate_ids_rejected() -> None:
    spec = EnvCredential(id="dup", secrets={"k": "V"})
    with pytest.raises(CredentialAlreadyExistsError):
        EnvSource(
            [spec, EnvCredential(id=" dup ", secrets={"k": "W"})], environ={"V": "x", "W": "y"}
        )


def test_non_spec_rejected() -> None:
    with pytest.raises(ConfigurationError):
        EnvSource(["nope"], environ={})  # type: ignore[list-item]


def test_empty_source_is_allowed() -> None:
    assert EnvSource([], environ={}).get_credentials() == ()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"id": "", "secrets": {"k": "V"}},
        {"id": "x", "secrets": {}},
        {"id": "x", "secrets": {"k": ""}},
        {"id": "x", "secrets": {"k": "A=B"}},
        {"id": "x", "secrets": {"k": 5}},
        {"id": "x", "secrets": {"": "V"}},
        {"id": "x", "secrets": {"k": "V"}, "optional_secrets": {"k": "W"}},
        {"id": "x", "secrets": {"k": "V"}, "optional_secrets": {"o": "bad\0"}},
        {"id": "x", "secrets": {"k": "V"}, "metadata": {"max_concurrency": 0}},
        {"id": "x", "secrets": {"k": "V"}, "metadata": {"max_concurrency": "2"}},
        {"id": "x", "secrets": ["V"]},
        {"id": "x", "secrets": {"k": "V"}, "optional_secrets": ["W"]},
        {"id": "x", "secrets": {"k": "V"}, "metadata": ["m"]},
        {"id": 5, "secrets": {"k": "V"}},
    ],
)
def test_invalid_env_credential_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ConfigurationError):
        EnvCredential(**kwargs)  # type: ignore[arg-type]


def test_valid_max_concurrency_metadata_accepted() -> None:
    spec = EnvCredential(id="x", secrets={"k": "V"}, metadata={"max_concurrency": None})
    assert spec.metadata["max_concurrency"] is None


def test_env_credential_is_immutable() -> None:
    spec = EnvCredential(id=" x ", secrets={"k": "V"})
    assert spec.id == "x"
    with pytest.raises(TypeError):
        spec.secrets["other"] = "W"  # type: ignore[index]
