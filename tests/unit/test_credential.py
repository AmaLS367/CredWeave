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

    # Secret values in cred.secrets are masked ('***'), protecting against leaks
    assert cred.secrets["api_key"] == "***"
    assert cred.secrets["private_data"] == "***"
    assert super_secret_1 not in str(cred.secrets)
    assert super_secret_2 not in str(cred.secrets)
    assert super_secret_1 not in repr(cred.secrets)
    assert super_secret_2 not in repr(cred.secrets)
    assert set(cred.secrets.values()) == {"***"}
    assert dict(cred.secrets) == {"api_key": "***", "private_data": "***"}

    # Explicit secret retrieval returns the actual unmasked secrets
    assert cred.get_secret("api_key") == super_secret_1
    assert cred.get_secret("private_data") == super_secret_2
    assert cred.require_secret("api_key") == super_secret_1
    assert cred.require_secret("private_data") == super_secret_2


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


def test_credential_hash_and_equality_contract_with_subclasses() -> None:
    """Regression test: Credential.__eq__ and __hash__ contract must hold across subclasses.

    If a == b, then hash(a) == hash(b) MUST hold true, enabling subclasses
    of Credential to work seamlessly in sets and dict lookups.
    """

    class SubCredential(Credential):
        pass

    class AnotherSubCredential(Credential):
        pass

    base = Credential(id="shared-id", secrets={"key": "secret1"})
    sub1 = SubCredential(id="shared-id", secrets={"key": "secret2"})
    sub2 = AnotherSubCredential(id="shared-id", secrets={"key": "secret3"})
    different = SubCredential(id="other-id", secrets={"key": "secret1"})

    # Symmetry
    assert base == sub1
    assert sub1 == base
    assert sub1 == sub2
    assert sub2 == sub1

    # Python hash contract: a == b => hash(a) == hash(b)
    assert hash(base) == hash(sub1)
    assert hash(sub1) == hash(sub2)

    # Set membership & deduplication
    cred_set = {base}
    assert sub1 in cred_set
    assert sub2 in cred_set
    assert different not in cred_set

    # Dict key equivalence
    mapping = {base: "primary_entry"}
    assert mapping[sub1] == "primary_entry"
    assert mapping[sub2] == "primary_entry"

    # Inequality
    assert base != different
    assert sub1 != different
    assert hash(base) != hash(different)
    assert (base == "shared-id") is False
    assert (base == None) is False  # noqa: E711
