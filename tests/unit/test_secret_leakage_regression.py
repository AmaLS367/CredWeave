"""Regression test suite guaranteeing zero secret leakage across the public API.

Audits:
- repr and str of domain models, candidates, records and contexts
- Exception messages and representation formatting
- Logging of domain entities, leases, outcomes, and failure states
- Secret retention and exposure inside Lease and Outcome metadata
- Defense against chained exception contexts leaking raw data
"""

import logging
import traceback
from datetime import datetime, timezone

import pytest

from credweave import (
    ConfigurationError,
    Credential,
    CredentialCandidate,
    CredentialRecord,
    CredentialState,
    InvalidLeaseError,
    InvalidOutcomeError,
    Lease,
    MemoryStateStore,
    Outcome,
    SecretAccessError,
    SelectionContext,
    SystemClock,
)
from credweave.application.services.pool import CredentialPool

SECRET_VAL = "sk-LIVE-SECRET-KEY-998877"
SECRET_VAL_2 = "cs-CLIENT-SUPER-SECRET-443322"
BEARER_TOKEN = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.secret_sig"


def _full_exception_text(exc: BaseException) -> str:
    """Format all observable aspects of an exception into a single string."""
    parts = [
        "".join(traceback.format_exception(exc)),
        repr(exc),
        str(exc),
        repr(exc.args),
    ]
    chained: BaseException | None = exc
    while chained is not None:
        parts.append(repr(vars(chained)))
        chained = chained.__context__ or chained.__cause__
    return "\n".join(parts)


# =========================================================================
# 1. Credential Security & Metadata
# =========================================================================


def test_credential_metadata_masking_in_repr_and_str() -> None:
    """Sensitive keys or raw secrets in Credential metadata must be masked in repr/str."""
    cred = Credential(
        id="cred-sec-1",
        secrets={"api_key": SECRET_VAL, "client_secret": SECRET_VAL_2},
        metadata={
            "tier": "enterprise",
            "api_key": SECRET_VAL,  # duplicate in metadata
            "custom_token": "token-12345",
            "nested": {"password": "admin-password", "safe": "public-val"},
            "raw_leak": SECRET_VAL_2,  # arbitrary key holding raw secret
        },
    )

    repr_str = repr(cred)
    str_str = str(cred)
    meta_repr = repr(cred.metadata)
    meta_str = str(cred.metadata)

    for text in (repr_str, str_str, meta_repr, meta_str):
        assert SECRET_VAL not in text
        assert SECRET_VAL_2 not in text
        assert "admin-password" not in text
        assert "token-12345" not in text
        assert "***" in text

    # Non-sensitive metadata remains visible
    assert "enterprise" in repr_str
    assert "public-val" in repr_str

    # Programmatic access to metadata is preserved
    assert cred.metadata["tier"] == "enterprise"
    assert cred.metadata["api_key"] == SECRET_VAL
    assert cred.get_metadata("tier") == "enterprise"

    # Metadata is immutable
    with pytest.raises(TypeError):
        cred.metadata["tier"] = "tampered"  # type: ignore[index]


# =========================================================================
# 2. Lease Representation & Metadata
# =========================================================================


def test_lease_metadata_and_repr_never_leak_secrets() -> None:
    """Lease repr, str, and metadata must redact credential secrets and sensitive keys."""
    now = datetime.now(timezone.utc)
    cred = Credential(
        id="cred-lease-sec",
        secrets={"key": SECRET_VAL, "secret": SECRET_VAL_2},
    )

    lease = Lease(
        credential=cred,
        lease_id="lease-sec-001",
        acquired_at=now,
        metadata={
            "worker_id": "worker-42",
            "auth_token": "bearer-abc-123",
            "custom_header": SECRET_VAL,  # raw secret under non-sensitive name
            "nested_auth": {"client_secret": SECRET_VAL_2},
        },
    )

    repr_str = repr(lease)
    str_str = str(lease)
    meta_repr = repr(lease.metadata)
    meta_str = str(lease.metadata)

    for text in (repr_str, str_str, meta_repr, meta_str):
        assert SECRET_VAL not in text
        assert SECRET_VAL_2 not in text
        assert "bearer-abc-123" not in text
        assert "***" in text

    # Safe attributes remain readable
    assert "worker-42" in repr_str
    assert "lease-sec-001" in repr_str
    assert lease.metadata["worker_id"] == "worker-42"
    assert lease.credential_id == "cred-lease-sec"

    # Metadata is immutable
    with pytest.raises(TypeError):
        lease.metadata["worker_id"] = "modified"  # type: ignore[index]


# =========================================================================
# 3. Outcome Reason, Metadata & Redaction
# =========================================================================


def test_outcome_repr_and_str_mask_reason_and_metadata() -> None:
    """Outcome representations must mask sensitive reasons and metadata keys."""
    outcome = Outcome.auth_failed(
        reason=f"Failed auth for {SECRET_VAL} with {BEARER_TOKEN}",
        metadata={
            "status_code": 401,
            "api_key": SECRET_VAL,
            "private_key": "raw-key-content",
            "headers": {"Authorization": BEARER_TOKEN},
        },
    )

    repr_str = repr(outcome)
    str_str = str(outcome)
    meta_repr = repr(outcome.metadata)
    meta_str = str(outcome.metadata)

    for text in (repr_str, str_str, meta_repr, meta_str):
        assert SECRET_VAL not in text
        assert "raw-key-content" not in text
        assert "***" in text

    assert "401" in repr_str
    assert outcome.metadata["status_code"] == 401

    # Metadata mapping is immutable
    with pytest.raises(TypeError):
        outcome.metadata["status_code"] = 200  # type: ignore[index]


def test_outcome_explicit_redaction_method() -> None:
    """Outcome.redact strips specific secret values from reason and metadata."""
    outcome = Outcome.transient_error(
        reason=f"Network timeout contacting server with secret {SECRET_VAL}",
        metadata={"diag": f"Header {SECRET_VAL}", "attempt": 1},
    )

    redacted = outcome.redact([SECRET_VAL])
    assert SECRET_VAL not in str(redacted.reason)
    assert SECRET_VAL not in str(redacted.metadata["diag"])
    assert "***" in str(redacted.reason)
    assert redacted.metadata["attempt"] == 1


def test_pool_report_automatically_redacts_credentials_from_outcome() -> None:
    """CredentialPool.report_sync and report redact raw secrets before store settlement."""
    cred = Credential(id="cred-p1", secrets={"token": SECRET_VAL})
    pool = CredentialPool(
        credentials=[cred],
        clock=SystemClock(),
        store=MemoryStateStore(),
    )

    lease = pool.acquire_sync()

    # User accidentally passes raw secret in reason and metadata
    outcome = Outcome.auth_failed(
        reason=f"Failed with credential secret {cred.get_secret('token')}",
        metadata={"used_secret": cred.get_secret("token")},
    )

    pool.report_sync(lease, outcome)

    # Inspect records from the store
    record = pool.get_record("cred-p1")
    assert record is not None
    record_text = f"{record!r} {record!s} {record.metadata!r}"
    assert SECRET_VAL not in record_text


# =========================================================================
# 4. Exception Security & Masking
# =========================================================================


def test_domain_exceptions_never_leak_secrets_in_str_repr_traceback() -> None:
    """CredWeave domain exceptions must sanitize messages, details, and args."""
    err_config = ConfigurationError(f"Invalid config with secret token: {SECRET_VAL}")
    text_config = _full_exception_text(err_config)
    assert SECRET_VAL not in text_config
    assert "***" in text_config

    err_lease = InvalidLeaseError("lease-99", reason=f"Key {SECRET_VAL} expired")
    text_lease = _full_exception_text(err_lease)
    assert SECRET_VAL not in text_lease
    assert "***" in text_lease

    err_outcome = InvalidOutcomeError(
        f"Invalid payload for {SECRET_VAL}",
        details={"api_key": SECRET_VAL, "code": "ERR_AUTH"},
    )
    text_outcome = _full_exception_text(err_outcome)
    assert SECRET_VAL not in text_outcome
    assert "***" in text_outcome

    err_access = SecretAccessError("cred-1", key=SECRET_VAL)
    text_access = _full_exception_text(err_access)
    assert SECRET_VAL not in text_access
    assert "***" in text_access


def test_outcome_invalid_type_exception_does_not_chain_or_leak() -> None:
    """Invalid outcome type raises InvalidOutcomeError without chained causes."""
    with pytest.raises(InvalidOutcomeError) as exc_info:
        Outcome(type=SECRET_VAL)  # type: ignore[arg-type]

    full = _full_exception_text(exc_info.value)
    assert SECRET_VAL not in full
    assert exc_info.value.__cause__ is None


# =========================================================================
# 5. Logging Protection
# =========================================================================


def test_logging_domain_objects_does_not_leak_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Logging domain models and mappings via logger must never expose secrets."""
    logger = logging.getLogger("credweave.test.audit")
    cred = Credential(id="log-cred", secrets={"token": SECRET_VAL}, metadata={"key": SECRET_VAL})
    lease = Lease(credential=cred, lease_id="l-1", acquired_at=datetime.now(timezone.utc))
    outcome = Outcome.rate_limited(
        retry_after=5.0,
        reason=f"Rate limit for {SECRET_VAL}",
        metadata={"token": SECRET_VAL},
    )

    candidate = CredentialCandidate(
        credential=cred,
        state=CredentialState.AVAILABLE,
        metadata={"auth": SECRET_VAL},
    )
    record = CredentialRecord(
        credential_id="log-cred",
        state=CredentialState.AVAILABLE,
        metadata={"token": SECRET_VAL},
    )
    context = SelectionContext(preferred_metadata={"api_key": SECRET_VAL})

    with caplog.at_level(logging.DEBUG):
        logger.info("Credential: %s, repr: %r", cred, cred)
        logger.info("Credential metadata: %s, repr: %r", cred.metadata, cred.metadata)
        logger.info("Lease: %s, repr: %r", lease, lease)
        logger.info("Outcome: %s, repr: %r", outcome, outcome)
        logger.info("Outcome metadata: %s, repr: %r", outcome.metadata, outcome.metadata)
        logger.info("Candidate: %s, repr: %r", candidate, candidate)
        logger.info("Record: %s, repr: %r", record, record)
        logger.info("Context: %s, repr: %r", context, context)

    visible_logs = caplog.text + "".join(r.getMessage() for r in caplog.records)
    assert SECRET_VAL not in visible_logs
    assert "***" in visible_logs


# =========================================================================
# 6. Internal Security Helpers Unit Coverage
# =========================================================================


def test_security_helpers_and_mapping_edge_cases() -> None:
    """Ensure full test coverage for security helper primitives and edge cases."""
    from credweave.domain._security import (
        SecretSafeMapping,
        is_sensitive_key,
        mask_metadata,
        mask_secret_text,
    )

    # 1. is_sensitive_key edge cases
    assert is_sensitive_key(12345) is False
    assert is_sensitive_key(None) is False
    assert is_sensitive_key("master_backup_key") is True
    assert is_sensitive_key("public_attribute") is False

    # 2. mask_secret_text edge cases
    assert mask_secret_text(None) is None
    # Short secrets (<3 chars) and non-strings in raw_secrets are ignored
    text = mask_secret_text("secret_val_long", raw_secrets=["s", 123, "secret_val_long"])  # type: ignore[list-item]
    assert text == "***"

    # 3. mask_metadata collections (lists, tuples, sets, primitives)
    meta_list = mask_metadata(["Bearer abcdef123", {"password": "admin"}, 42])
    assert meta_list == ["Bearer ***", {"password": "***"}, 42]

    meta_set = mask_metadata({"token: xyz", "safe_value"}, raw_secrets=["xyz"])
    assert "token:***" in meta_set or "token: ***" in meta_set or any("***" in x for x in meta_set)

    meta_dict = mask_metadata(
        {
            "tags_set": {"my_secret", "normal"},
            "num": 100,
            "sub_list": ["Bearer secret-tok"],
        },
        raw_secrets=["my_secret"],
    )
    assert meta_dict["num"] == 100
    assert any("***" in x for x in meta_dict["tags_set"])
    assert meta_dict["sub_list"] == ["Bearer ***"]

    # Primitive values pass through
    assert mask_metadata(12345) == 12345
    assert mask_metadata("Bearer abc") == "Bearer ***"

    # 4. SecretSafeMapping wrap another SecretSafeMapping, len, eq, get_masked
    base_mapping = SecretSafeMapping({"api_key": "val1", "tier": "free"}, raw_secrets=["val1"])
    assert len(base_mapping) == 2
    assert base_mapping["tier"] == "free"
    assert base_mapping.get("tier") == "free"
    assert base_mapping.get("unknown", "default") == "default"

    # Wrapping another SecretSafeMapping combines secrets and preserves immutability
    wrapped = SecretSafeMapping(base_mapping, raw_secrets=["val2"])
    assert len(wrapped) == 2
    assert repr(wrapped) == repr(base_mapping)
    assert wrapped == {"api_key": "val1", "tier": "free"}
    assert wrapped != 42
    assert wrapped != {"tier": "free"}

    # get_masked returns dictionary with redacted values
    masked_dict = wrapped.get_masked()
    assert masked_dict["api_key"] == "***"
    assert masked_dict["tier"] == "free"
