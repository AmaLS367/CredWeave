"""Unit tests for the CredentialPool runtime service."""

import pytest

from credweave.application.ports.strategy import SelectionContext
from credweave.application.services.pool import CredentialPool
from credweave.domain.enums import CredentialState
from credweave.domain.errors import (
    ConfigurationError,
    CredentialAlreadyExistsError,
    InvalidLeaseError,
    InvalidOutcomeError,
    LeaseExpiredError,
    NoCredentialsAvailableError,
    StateStoreError,
)
from credweave.domain.models import Credential, Lease
from credweave.domain.outcomes import Outcome
from credweave.infrastructure.clocks.system import SystemClock
from credweave.infrastructure.sources.static import StaticSource
from credweave.infrastructure.stores.memory import MemoryStateStore
from credweave.strategies.round_robin import RoundRobinStrategy
from tests.conftest import TestClock


def test_credential_pool_initialization_defaults(sample_credential: Credential) -> None:
    """Test initializing CredentialPool with default adapters."""
    pool = CredentialPool(credentials=[sample_credential])

    assert pool.initial_credentials == (sample_credential,)
    assert isinstance(pool.clock, SystemClock)
    assert isinstance(pool.source, StaticSource)
    assert isinstance(pool.strategy, RoundRobinStrategy)
    assert isinstance(pool.store, MemoryStateStore)
    assert pool.in_flight_leases == 0
    assert len(pool.active_leases) == 0


def test_credential_pool_custom_ports(sample_credential: Credential, test_clock: TestClock) -> None:
    """Test initializing CredentialPool with explicitly injected ports."""
    source = StaticSource([sample_credential])
    strategy = RoundRobinStrategy()
    store = MemoryStateStore(clock=test_clock)

    pool = CredentialPool(
        source=source,
        strategy=strategy,
        store=store,
        clock=test_clock,
    )

    assert pool.clock is test_clock
    assert pool.source is source
    assert pool.strategy is strategy
    assert pool.store is store


def test_credential_pool_validation_errors(sample_credential: Credential) -> None:
    """Test input validation for pool initialization."""
    # Neither credentials nor source
    with pytest.raises(ConfigurationError):
        CredentialPool()

    # Invalid credential element
    with pytest.raises(ConfigurationError):
        CredentialPool(credentials=["not_a_credential"])  # type: ignore[list-item]

    # Duplicate credential ID
    duplicate = Credential(id=sample_credential.id, secrets={"key": "val"})
    with pytest.raises(CredentialAlreadyExistsError):
        CredentialPool(credentials=[sample_credential, duplicate])


def test_credential_pool_get_credential(sample_credentials: list[Credential]) -> None:
    """Test get_credential finds existing credentials or returns None."""
    pool = CredentialPool(credentials=sample_credentials)
    assert pool.get_credential("cred-alpha") == sample_credentials[0]
    assert pool.get_credential("non-existent") is None


def test_acquire_and_report_sync_success(
    sample_credential: Credential, test_clock: TestClock
) -> None:
    """Test standard synchronous acquire, lease verification, and successful report."""
    pool = CredentialPool(credentials=[sample_credential], clock=test_clock)

    lease = pool.acquire_sync()
    assert isinstance(lease, Lease)
    assert lease.credential_id == sample_credential.id
    assert lease.acquired_at == test_clock.now()
    assert pool.in_flight_leases == 1
    leases_before = pool.active_leases
    assert len(leases_before) == 1
    assert leases_before[0] == lease

    # Report success
    pool.report_sync(lease, Outcome.success())
    assert pool.in_flight_leases == 0
    leases_after = pool.active_leases
    assert len(leases_after) == 0

    record = pool.get_record(sample_credential.id)
    assert record is not None
    assert record.state == CredentialState.AVAILABLE
    assert record.in_flight_leases == 0
    assert record.total_leases == 1
    assert record.consecutive_failures == 0


@pytest.mark.asyncio
async def test_acquire_and_report_async_success(
    sample_credential: Credential, test_clock: TestClock
) -> None:
    """Test standard asynchronous acquire, lease verification, and successful report."""
    pool = CredentialPool(credentials=[sample_credential], clock=test_clock)

    lease = await pool.acquire()
    assert isinstance(lease, Lease)
    assert lease.credential_id == sample_credential.id
    assert pool.in_flight_leases == 1

    await pool.report(lease, Outcome.success())
    assert pool.in_flight_leases == 0

    record = await pool.get_record_async(sample_credential.id)
    assert record is not None
    assert record.state == CredentialState.AVAILABLE
    assert record.in_flight_leases == 0
    assert record.total_leases == 1


def test_double_report_raises_invalid_lease_error(
    sample_credential: Credential, test_clock: TestClock
) -> None:
    """Test reporting an already released lease raises InvalidLeaseError."""
    pool = CredentialPool(credentials=[sample_credential], clock=test_clock)
    lease = pool.acquire_sync()

    pool.report_sync(lease, Outcome.success())

    with pytest.raises(InvalidLeaseError) as exc_info:
        pool.report_sync(lease, Outcome.success())
    assert lease.lease_id in str(exc_info.value)


def test_report_unknown_or_fake_lease_raises(
    sample_credential: Credential, test_clock: TestClock
) -> None:
    """Test reporting an unissued lease raises InvalidLeaseError."""
    pool = CredentialPool(credentials=[sample_credential], clock=test_clock)
    fake_lease = Lease(
        credential=sample_credential,
        lease_id="fake_lease_999",
        acquired_at=test_clock.now(),
    )

    with pytest.raises(InvalidLeaseError):
        pool.report_sync(fake_lease, Outcome.success())


def test_report_invalid_types_raise(sample_credential: Credential) -> None:
    """Test non-Lease and non-Outcome inputs raise appropriate errors."""
    pool = CredentialPool(credentials=[sample_credential])

    with pytest.raises(InvalidLeaseError):
        pool.report_sync("not_a_lease", Outcome.success())  # type: ignore[arg-type]

    lease = pool.acquire_sync()
    with pytest.raises(InvalidOutcomeError):
        pool.report_sync(lease, "not_an_outcome")  # type: ignore[arg-type]


def test_report_mismatched_credential_lease(
    sample_credentials: list[Credential], test_clock: TestClock
) -> None:
    """Test reporting a lease tampered with mismatched credential raises InvalidLeaseError."""
    pool = CredentialPool(credentials=sample_credentials, clock=test_clock)
    lease = pool.acquire_sync()

    tampered_lease = Lease(
        credential=sample_credentials[1],
        lease_id=lease.lease_id,
        acquired_at=lease.acquired_at,
    )

    with pytest.raises(InvalidLeaseError) as exc_info:
        pool.report_sync(tampered_lease, Outcome.success())
    assert "credential mismatch" in str(exc_info.value).lower()


def test_lease_timeout_raises_lease_expired_error(
    sample_credential: Credential, test_clock: TestClock
) -> None:
    """Test that configured lease_timeout triggers LeaseExpiredError on late report."""
    pool = CredentialPool(
        credentials=[sample_credential],
        clock=test_clock,
        lease_timeout=10.0,
    )
    lease = pool.acquire_sync()

    test_clock.advance(15.0)

    with pytest.raises(LeaseExpiredError) as exc_info:
        pool.report_sync(lease, Outcome.success())
    assert lease.lease_id in str(exc_info.value)

    # In-flight lease should still be released from tracking
    assert pool.in_flight_leases == 0


def test_rate_limited_and_automatic_cooldown_recovery(
    sample_credential: Credential, test_clock: TestClock
) -> None:
    """Test rate-limited outcome triggers cooldown, blocking acquire until cooldown expires."""
    pool = CredentialPool(credentials=[sample_credential], clock=test_clock)

    lease = pool.acquire_sync()
    pool.report_sync(
        lease,
        Outcome.rate_limited(retry_after=30.0, reason="Rate limit reached"),
    )

    record = pool.get_record(sample_credential.id)
    assert record is not None
    assert record.state == CredentialState.RATE_LIMITED
    assert record.cooldown_until is not None

    # Immediate next acquire should fail because the only credential is in cooldown
    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync()

    # Advance clock by 29 seconds (still within cooldown)
    test_clock.advance(29.0)
    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync()

    # Advance clock by 2 more seconds (31s total: cooldown expired)
    test_clock.advance(2.0)
    recovered_lease = pool.acquire_sync()
    assert recovered_lease.credential_id == sample_credential.id

    rec_after = pool.get_record(sample_credential.id)
    assert rec_after is not None
    assert rec_after.state == CredentialState.AVAILABLE


def test_auth_failed_permanently_revokes(
    sample_credential: Credential, test_clock: TestClock
) -> None:
    """Test auth_failed marks credential REVOKED, and time passage never recovers it."""
    pool = CredentialPool(credentials=[sample_credential], clock=test_clock)

    lease = pool.acquire_sync()
    pool.report_sync(lease, Outcome.auth_failed(reason="API key expired"))

    record = pool.get_record(sample_credential.id)
    assert record is not None
    assert record.state == CredentialState.REVOKED
    assert record.cooldown_until is None

    # Advance clock significantly
    test_clock.advance(10000.0)

    # Credential is still permanently revoked
    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync()


def test_quota_exhausted_lifecycle(sample_credential: Credential, test_clock: TestClock) -> None:
    """Test quota_exhausted sets QUOTA_EXHAUSTED state and recovers if retry_after passes."""
    pool = CredentialPool(credentials=[sample_credential], clock=test_clock)

    lease = pool.acquire_sync()
    pool.report_sync(lease, Outcome.quota_exhausted(retry_after=60.0))

    record = pool.get_record(sample_credential.id)
    assert record is not None
    assert record.state == CredentialState.QUOTA_EXHAUSTED

    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync()

    test_clock.advance(61.0)
    new_lease = pool.acquire_sync()
    assert new_lease.credential_id == sample_credential.id


def test_repeated_failures_escalate_to_unhealthy(
    sample_credential: Credential, test_clock: TestClock
) -> None:
    """Test that consecutive failures reaching max threshold escalate to UNHEALTHY."""
    pool = CredentialPool(
        credentials=[sample_credential],
        clock=test_clock,
        max_consecutive_failures=2,
    )

    # First transient failure: state becomes COOLDOWN
    lease1 = pool.acquire_sync()
    pool.report_sync(lease1, Outcome.transient_error(retry_after=5.0))
    rec1 = pool.get_record(sample_credential.id)
    assert rec1 is not None
    assert rec1.state == CredentialState.COOLDOWN
    assert rec1.consecutive_failures == 1

    test_clock.advance(6.0)

    # Second transient failure: reaches max_consecutive_failures (2) -> UNHEALTHY
    lease2 = pool.acquire_sync()
    pool.report_sync(lease2, Outcome.transient_error(retry_after=5.0))
    rec2 = pool.get_record(sample_credential.id)
    assert rec2 is not None
    assert rec2.state == CredentialState.UNHEALTHY
    assert rec2.consecutive_failures == 2

    # Advancing time does not recover UNHEALTHY state
    test_clock.advance(1000.0)
    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync()

    # Manual reset recovers credential
    pool.reset_credential(sample_credential.id)
    rec_reset = pool.get_record(sample_credential.id)
    assert rec_reset is not None
    assert rec_reset.state == CredentialState.AVAILABLE
    assert rec_reset.consecutive_failures == 0

    lease3 = pool.acquire_sync()
    assert lease3.credential_id == sample_credential.id


@pytest.mark.asyncio
async def test_reset_credential_async(sample_credential: Credential) -> None:
    """Test reset_credential_async restores credential to AVAILABLE."""
    pool = CredentialPool(credentials=[sample_credential])
    lease = await pool.acquire()
    await pool.report(lease, Outcome.auth_failed())

    rec = await pool.get_record_async(sample_credential.id)
    assert rec is not None
    assert rec.state == CredentialState.REVOKED

    await pool.reset_credential_async(sample_credential.id)
    rec2 = await pool.get_record_async(sample_credential.id)
    assert rec2 is not None
    assert rec2.state == CredentialState.AVAILABLE


def test_round_robin_order_across_pool(sample_credentials: list[Credential]) -> None:
    """Test cyclic selection order across 3 available credentials."""
    pool = CredentialPool(credentials=sample_credentials)

    l1 = pool.acquire_sync()
    l2 = pool.acquire_sync()
    l3 = pool.acquire_sync()
    l4 = pool.acquire_sync()

    assert l1.credential_id == "cred-alpha"
    assert l2.credential_id == "cred-beta"
    assert l3.credential_id == "cred-gamma"
    assert l4.credential_id == "cred-alpha"


def test_selection_context_filtering(sample_credentials: list[Credential]) -> None:
    """Test acquiring with SelectionContext requiring specific tags."""
    pool = CredentialPool(credentials=sample_credentials)

    # Only gamma has tag 'backup'
    ctx = SelectionContext(required_tags=frozenset({"backup"}))
    lease = pool.acquire_sync(context=ctx)
    assert lease.credential_id == "cred-gamma"

    # Non-existent tag raises NoCredentialsAvailableError
    ctx_missing = SelectionContext(required_tags=frozenset({"nonexistent"}))
    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync(context=ctx_missing)


def test_list_records_sync_and_async(sample_credentials: list[Credential]) -> None:
    """Test list_records and list_records_async return all records."""
    pool = CredentialPool(credentials=sample_credentials)

    records = pool.list_records()
    assert len(records) == 3
    ids = {r.credential_id for r in records}
    assert ids == {"cred-alpha", "cred-beta", "cred-gamma"}


def test_expired_lease_does_not_apply_caller_outcome_sync(
    sample_credential: Credential, test_clock: TestClock
) -> None:
    """Verify expired lease raises LeaseExpiredError without altering credential state."""
    pool = CredentialPool(
        credentials=[sample_credential],
        clock=test_clock,
        lease_timeout=10.0,
    )
    lease = pool.acquire_sync()
    assert pool.in_flight_leases == 1

    test_clock.advance(15.0)

    # Caller reports AUTH_FAILED on expired lease
    with pytest.raises(LeaseExpiredError):
        pool.report_sync(lease, Outcome.auth_failed(reason="Should not be applied"))

    # Active lease tracking is released
    assert pool.in_flight_leases == 0

    # Store record must NOT be REVOKED or modified by the rejected outcome
    rec = pool.get_record(sample_credential.id)
    assert rec is not None
    assert rec.state == CredentialState.AVAILABLE
    assert rec.in_flight_leases == 0
    assert rec.consecutive_failures == 0


@pytest.mark.asyncio
async def test_expired_lease_does_not_apply_caller_outcome_async(
    sample_credential: Credential, test_clock: TestClock
) -> None:
    """Verify expired lease in async report releases lease without altering health state."""
    pool = CredentialPool(
        credentials=[sample_credential],
        clock=test_clock,
        lease_timeout=10.0,
    )
    lease = await pool.acquire()
    assert pool.in_flight_leases == 1

    test_clock.advance(15.0)

    # Caller reports PERMANENT_FAILURE on expired lease
    with pytest.raises(LeaseExpiredError):
        await pool.report(lease, Outcome.permanent_failure(reason="Late failure"))

    assert pool.in_flight_leases == 0
    rec = await pool.get_record_async(sample_credential.id)
    assert rec is not None
    assert rec.state == CredentialState.AVAILABLE
    assert rec.in_flight_leases == 0


def test_report_sync_failure_safe_allows_retry(
    sample_credential: Credential, test_clock: TestClock
) -> None:
    """Verify failed store.record_outcome retains lease so caller can retry safely."""
    store = MemoryStateStore(clock=test_clock)
    pool = CredentialPool(
        credentials=[sample_credential],
        store=store,
        clock=test_clock,
    )

    lease = pool.acquire_sync()
    assert pool.in_flight_leases == 1

    # Temporarily monkeypatch store.record_outcome to simulate transient failure
    original_record_outcome = store.record_outcome
    attempts = 0

    def flaky_record_outcome(credential_id: str, outcome: Outcome, timestamp: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise StateStoreError("Transient storage connection failure")
        original_record_outcome(credential_id, outcome, timestamp)  # type: ignore[arg-type]

    store.record_outcome = flaky_record_outcome  # type: ignore[method-assign]

    # First attempt: store fails
    with pytest.raises(StateStoreError):
        pool.report_sync(lease, Outcome.success())

    # Lease must still be active and preserved in tracking
    assert pool.in_flight_leases == 1
    assert lease.lease_id in [active_l.lease_id for active_l in pool.active_leases]

    # Second attempt (retry): succeeds
    pool.report_sync(lease, Outcome.success())

    # Now the lease is released and outcome recorded
    assert pool.in_flight_leases == 0
    rec = pool.get_record(sample_credential.id)
    assert rec is not None
    assert rec.in_flight_leases == 0
    assert rec.state == CredentialState.AVAILABLE


@pytest.mark.asyncio
async def test_report_async_failure_safe_allows_retry(
    sample_credential: Credential, test_clock: TestClock
) -> None:
    """Verify failed store.record_outcome_async retains lease in async report for safe retry."""
    store = MemoryStateStore(clock=test_clock)
    pool = CredentialPool(
        credentials=[sample_credential],
        store=store,
        clock=test_clock,
    )

    lease = await pool.acquire()
    assert pool.in_flight_leases == 1

    original_record_outcome_async = store.record_outcome_async
    attempts = 0

    async def flaky_record_outcome_async(
        credential_id: str, outcome: Outcome, timestamp: object
    ) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise StateStoreError("Transient storage connection failure")
        await original_record_outcome_async(credential_id, outcome, timestamp)  # type: ignore[arg-type]

    store.record_outcome_async = flaky_record_outcome_async  # type: ignore[method-assign]

    # First attempt fails
    with pytest.raises(StateStoreError):
        await pool.report(lease, Outcome.success())

    # Lease preserved
    assert pool.in_flight_leases == 1

    # Retry succeeds
    await pool.report(lease, Outcome.success())
    assert pool.in_flight_leases == 0


def test_pool_transient_error_uses_configured_default_cooldown(
    sample_credential: Credential, test_clock: TestClock
) -> None:
    """Verify pool passes default_cooldown to transient errors when outcome has no retry_after."""
    pool = CredentialPool(
        credentials=[sample_credential],
        clock=test_clock,
        default_cooldown=120.0,
    )
    now = test_clock.now()
    lease = pool.acquire_sync()
    pool.report_sync(lease, Outcome.transient_error())

    rec = pool.get_record(sample_credential.id)
    assert rec is not None
    assert rec.state == CredentialState.COOLDOWN
    assert rec.cooldown_until is not None
    elapsed = (rec.cooldown_until - now).total_seconds()
    assert elapsed == 120.0
