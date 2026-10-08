"""Security hardening regression tests: deep-freeze, nested structures, unusual tokens."""

from datetime import datetime, timezone
from typing import Any

import pytest

from credweave.application.ports.state_store import CredentialRecord
from credweave.domain._security import (
    is_sensitive_key,
    mask_secret_text,
)
from credweave.domain.enums import CredentialState
from credweave.domain.models import Credential, Lease
from credweave.domain.outcomes import Outcome


def test_deep_freeze_nested_mutables() -> None:
    """Mutating caller dictionaries, lists, or sets after creation does not affect metadata."""
    sub_list = ["item1", "item2"]
    sub_set = {"tag1", "tag2"}
    sub_dict = {"inner_key": "inner_val", "list": sub_list}
    raw_meta = {
        "tier": "enterprise",
        "nested": sub_dict,
        "set_data": sub_set,
    }

    cred = Credential(id="c1", secrets={"k": "secret-123"}, metadata=raw_meta)

    # 1. Mutate caller structures after Credential creation
    sub_list.append("item3")
    sub_set.add("tag3")
    sub_dict["inner_key"] = "tampered"
    raw_meta["tier"] = "tampered"

    # 2. Assert Credential metadata remained unchanged
    assert cred.metadata["tier"] == "enterprise"
    assert cred.metadata["nested"]["inner_key"] == "inner_val"
    assert cred.metadata["nested"]["list"] == ("item1", "item2")
    assert cred.metadata["set_data"] == frozenset({"tag1", "tag2"})

    # 3. Assert in-place mutation attempts on nested metadata raise errors
    nested: Any = cred.metadata["nested"]
    with pytest.raises(TypeError):
        nested["inner_key"] = "hacked"

    with pytest.raises(AttributeError):
        nested["list"].append("hacked")

    set_data: Any = cred.metadata["set_data"]
    with pytest.raises(AttributeError):
        set_data.add("hacked")


def test_lease_and_outcome_deep_freeze() -> None:
    """Lease, Outcome, and CredentialRecord deep freeze their metadata structures."""
    now = datetime.now(timezone.utc)
    cred = Credential(id="c2", secrets={"k": "secret-abc"})
    list_val = [1, [2, 3]]
    dict_val: dict[str, Any] = {"a": {"b": list_val}}

    lease = Lease(credential=cred, lease_id="l1", acquired_at=now, metadata={"cfg": dict_val})
    outcome = Outcome.success(metadata={"cfg": dict_val})
    record = CredentialRecord(
        credential_id="c2", state=CredentialState.AVAILABLE, metadata={"cfg": dict_val}
    )

    list_val.append(99)
    dict_val["a"]["b"] = "changed"

    assert lease.metadata["cfg"]["a"]["b"] == (1, (2, 3))
    assert outcome.metadata["cfg"]["a"]["b"] == (1, (2, 3))
    assert record.metadata["cfg"]["a"]["b"] == (1, (2, 3))

    lease_cfg: Any = lease.metadata["cfg"]
    with pytest.raises(TypeError):
        lease_cfg["a"] = 123

    outcome_cfg: Any = outcome.metadata["cfg"]
    with pytest.raises(TypeError):
        outcome_cfg["a"] = 123

    record_cfg: Any = record.metadata["cfg"]
    with pytest.raises(TypeError):
        record_cfg["a"] = 123


def test_unusual_token_formats_masked() -> None:
    """Diagnostic masking handles modern and unusual token formats in reason strings."""
    tokens = [
        "github_pat_11AAAAAAA0123456789_abcdefghijklmnopqrstuvwxyz",
        "ghp_0123456789abcdefghijklmnopqrstuvwxyz",
        "sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456",
        "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890",
        "sk_" + "live_51AbcDefGhIjKlMnOpQrStUvWxYz",
        "rk_" + "live_51AbcDefGhIjKlMnOpQrStUvWxYz",
        "sk_test_51AbcDefGhIjKlMnOpQrStUvWxYz",
        "AKIAIOSFODNN7EXAMPLE",
        "ASIAIOSFODNN7EXAMPLE",
        "AIzaSyD-1234567890abcdefghijklmnopqr",
        "hf_abcdefghijklmnopqrstuvwxyz0123456789",
        "pypi-AgEIcHlwaS5vcmcCJDEyMzQ1Njc4LTlhYmNkZWY=",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.doNotLeakSignature",
    ]

    for tok in tokens:
        masked = mask_secret_text(f"Error communicating with upstream token {tok}")
        assert masked is not None
        assert tok not in masked, f"Failed to mask token: {tok}"
        assert "***" in masked


def test_quoted_and_unusual_key_value_delimiters() -> None:
    """Key-value diagnostics with quotes and unusual spacing are sanitized."""
    samples = [
        'api_key="super_secret_value_123"',
        "api_key='super_secret_value_123'",
        "password: 'my-db-password'",
        'token : "token-value-xyz"',
        'secret="quoted_val"',
        "api-key: unquoted_api_key_val",
    ]

    for text in samples:
        masked = mask_secret_text(f"Context: {text}")
        assert masked is not None
        assert "super_secret_value_123" not in masked
        assert "my-db-password" not in masked
        assert "token-value-xyz" not in masked
        assert "quoted_val" not in masked
        assert "unquoted_api_key_val" not in masked
        assert "***" in masked


def test_credential_id_not_flagged_as_sensitive() -> None:
    """Identifiers like id and credential_id are not masked in metadata."""
    assert is_sensitive_key("credential_id") is False
    assert is_sensitive_key("cred_id") is False
    assert is_sensitive_key("id") is False
    assert is_sensitive_key("api_key") is True
    assert is_sensitive_key("client_secret") is True


def test_secret_fingerprint_deterministic_and_opaque() -> None:
    """Secret fingerprinting is deterministic and does not leak raw secrets."""
    cred1 = Credential(id="c1", secrets={"a": "sec1", "b": "sec2"})
    cred2 = Credential(id="c1", secrets={"b": "sec2", "a": "sec1"})
    cred3 = Credential(id="c1", secrets={"a": "sec1", "b": "different"})

    assert cred1.secret_fingerprint == cred2.secret_fingerprint
    assert cred1.secret_fingerprint != cred3.secret_fingerprint
    assert "sec1" not in cred1.secret_fingerprint
    assert "sec2" not in cred1.secret_fingerprint
    assert len(cred1.secret_fingerprint) == 64
