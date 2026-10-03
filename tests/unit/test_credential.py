"""Unit tests for the Credential domain model."""

import pytest

from credweave.domain.errors import ConfigurationError, SecretAccessError
from credweave.domain.models import Credential


def test_credential_creation_and_attributes() -> None:
    """Test valid credential initialization and property accessors."""
    cred = Credential(
        id="modal-main",
        secrets={"token_id": "tok_123", "token_secret": "sec_456"},
        metadata={"account_id": "acc_789", "provider": "modal"},
    )

    assert cred.id == "modal-main"
    assert cred.get_secret("token_id") == "tok_123"
    assert cred.get_secret("token_secret") == "sec_456"
    assert cred.get_secret("non_existent") is None
    assert cred.get_secret("non_existent", "default_val") == "default_val"

    assert cred.has_secret("token_id") is True
    assert cred.has_secret("unknown") is False

    assert cred.require_secret("token_id") == "tok_123"
    with pytest.raises(SecretAccessError) as exc_info:
        cred.require_secret("missing_key")
    assert "missing_key" in str(exc_info.value)
    assert cred.id in str(exc_info.value)

    assert cred.get_metadata("provider") == "modal"
    assert cred.get_metadata("account_id") == "acc_789"
    assert cred.get_metadata("missing") is None
    assert cred.has_metadata("provider") is True
    assert cred.has_metadata("missing") is False

    assert cred.secret_keys == frozenset({"token_id", "token_secret"})


def test_credential_id_validation() -> None:
    """Test that invalid or blank credential IDs raise ConfigurationError."""
    with pytest.raises(ConfigurationError):
        Credential(id="")

    with pytest.raises(ConfigurationError):
        Credential(id="   ")

    with pytest.raises(ConfigurationError):
        Credential(id=None)  # type: ignore[arg-type]


def test_credential_secret_redaction_in_repr_and_str() -> None:
    """Test that secret values NEVER appear in repr() or str()."""
    super_secret_1 = "SUPER_SECRET_TOKEN_VALUE_XYZ"
    super_secret_2 = "TOP_SECRET_PRIVATE_KEY_DATA"

    cred = Credential(
        id="test-key",
        secrets={
            "api_key": super_secret_1,
            "private_data": super_secret_2,
        },
        metadata={"provider": "anthropic", "environment": "production"},
    )

    repr_str = repr(cred)
    str_str = str(cred)

    # Prove secrets are completely redacted
    assert super_secret_1 not in repr_str
    assert super_secret_2 not in repr_str
    assert super_secret_1 not in str_str
    assert super_secret_2 not in str_str

    # Secret keys and metadata remain visible for debugging
    assert "api_key" in repr_str
    assert "private_data" in repr_str
    assert "***" in repr_str
    assert "anthropic" in repr_str
    assert "production" in repr_str


def test_credential_immutability() -> None:
    """Test that secrets and metadata cannot be mutated externally."""
    initial_secrets = {"key": "secret_value"}
    initial_metadata = {"env": "prod"}

    cred = Credential(
        id="immutable-cred",
        secrets=initial_secrets,
        metadata=initial_metadata,
    )

    # Modifying the source dictionary after passing it should NOT mutate the credential
    initial_secrets["key"] = "tampered"
    initial_secrets["new_key"] = "added"
    initial_metadata["env"] = "tampered"

    assert cred.get_secret("key") == "secret_value"
    assert cred.has_secret("new_key") is False
    assert cred.get_metadata("env") == "prod"

    # Attempting to mutate through the returned mapping proxy should raise TypeError
    with pytest.raises(TypeError):
        cred.secrets["key"] = "hacked"  # type: ignore[index]

    with pytest.raises(TypeError):
        cred.metadata["env"] = "hacked"  # type: ignore[index]


def test_credential_equality_and_hash() -> None:
    """Test deliberate identity-based equality and hashing semantics."""
    cred1 = Credential(
        id="account-alpha",
        secrets={"key": "secret_v1"},
        metadata={"tier": "free"},
    )
    cred2 = Credential(
        id="account-alpha",
        secrets={"key": "secret_v2_rotated"},
        metadata={"tier": "enterprise"},
    )
    cred3 = Credential(
        id="account-beta",
        secrets={"key": "secret_v1"},
        metadata={"tier": "free"},
    )

    # Same ID -> equal, even if secrets or metadata differ (managed identity entity)
    assert cred1 == cred2
    assert hash(cred1) == hash(cred2)

    # Different ID -> not equal
    assert cred1 != cred3
    assert hash(cred1) != hash(cred3)

    # Not equal to non-Credential objects
    assert cred1 != "account-alpha"
    assert cred1 != 12345

    # Can be used in sets and as dict keys
    cred_set = {cred1, cred2, cred3}
    assert len(cred_set) == 2
    assert cred1 in cred_set
    assert cred3 in cred_set
