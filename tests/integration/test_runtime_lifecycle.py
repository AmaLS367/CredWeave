"""Integration tests for the complete CredWeave runtime lifecycle."""

import pytest

from credweave import (
    Credential,
    CredentialPool,
    CredentialState,
    NoCredentialsAvailableError,
    Outcome,
)
from tests.conftest import TestClock


def test_quickstart_lifecycle_flow(test_clock: TestClock) -> None:
    """Test the complete user workflow modeled after README.md Quickstart."""
    credentials = [
        Credential(
            id="openai-prod-team-a",
            secrets={"api_key": "sk-proj-live-mock-key-alpha"},
            metadata={"tier": "primary", "rate_limit_rpm": 5000},
        ),
        Credential(
            id="openai-prod-team-b",
            secrets={"api_key": "sk-proj-live-mock-key-beta"},
            metadata={"tier": "fallback", "rate_limit_rpm": 2000},
        ),
    ]

    pool = CredentialPool(credentials=credentials, clock=test_clock)

    # 1. Acquire first lease (primary)
    lease1 = pool.acquire_sync()
    assert lease1.credential_id == "openai-prod-team-a"
    assert lease1.credential.require_secret("api_key") == "sk-proj-live-mock-key-alpha"

    # 2. Primary hits rate limit with retry_after=30s
    pool.report_sync(
        lease1,
        Outcome.rate_limited(retry_after=30.0, reason="Rate limit reached"),
    )

    # 3. Next acquire automatically routes to the second eligible credential (fallback)
    lease2 = pool.acquire_sync()
    assert lease2.credential_id == "openai-prod-team-b"

    # 4. Fallback hits auth failure (revoked key)
    pool.report_sync(
        lease2,
        Outcome.auth_failed(reason="Invalid API Key or Revoked Token"),
    )

    # 5. At this moment, team-b is REVOKED and team-a is still in cooldown
    with pytest.raises(NoCredentialsAvailableError):
        pool.acquire_sync()

    # 6. Advance clock past team-a's cooldown
    test_clock.advance(35.0)

    # 7. team-a is eligible again!
    lease3 = pool.acquire_sync()
    assert lease3.credential_id == "openai-prod-team-a"

    # 8. Report success
    pool.report_sync(lease3, Outcome.success())

    rec_a = pool.get_record("openai-prod-team-a")
    assert rec_a is not None
    assert rec_a.state == CredentialState.AVAILABLE
    assert rec_a.consecutive_failures == 0

    rec_b = pool.get_record("openai-prod-team-b")
    assert rec_b is not None
    assert rec_b.state == CredentialState.REVOKED


@pytest.mark.asyncio
async def test_async_and_sync_equivalence(test_clock: TestClock) -> None:
    """Verify sync and async APIs behave with identical state transitions."""
    c1 = Credential(id="c1", secrets={"key": "k1"})
    c2 = Credential(id="c2", secrets={"key": "k2"})

    sync_pool = CredentialPool(credentials=[c1, c2], clock=test_clock)
    async_pool = CredentialPool(credentials=[c1, c2], clock=test_clock)

    # Step 1: Acquire
    sync_l1 = sync_pool.acquire_sync()
    async_l1 = await async_pool.acquire()
    assert sync_l1.credential_id == async_l1.credential_id == "c1"

    # Step 2: Rate limit
    sync_pool.report_sync(sync_l1, Outcome.rate_limited(retry_after=10.0))
    await async_pool.report(async_l1, Outcome.rate_limited(retry_after=10.0))

    # Step 3: Next acquire rotates to c2
    sync_l2 = sync_pool.acquire_sync()
    async_l2 = await async_pool.acquire()
    assert sync_l2.credential_id == async_l2.credential_id == "c2"

    # Step 4: c2 succeeds
    sync_pool.report_sync(sync_l2, Outcome.success())
    await async_pool.report(async_l2, Outcome.success())

    # Step 5: Compare store state records
    sync_rec1 = sync_pool.get_record("c1")
    async_rec1 = await async_pool.get_record_async("c1")
    assert sync_rec1 is not None
    assert async_rec1 is not None
    assert sync_rec1.state == async_rec1.state == CredentialState.RATE_LIMITED
    assert sync_rec1.in_flight_leases == async_rec1.in_flight_leases == 0

    sync_rec2 = sync_pool.get_record("c2")
    async_rec2 = await async_pool.get_record_async("c2")
    assert sync_rec2 is not None
    assert async_rec2 is not None
    assert sync_rec2.state == async_rec2.state == CredentialState.AVAILABLE
    assert sync_rec2.in_flight_leases == async_rec2.in_flight_leases == 0


def test_concurrent_in_flight_leases_tracking(sample_credentials: list[Credential]) -> None:
    """Verify in-flight leases increment for multiple concurrent leases of the same pool."""
    pool = CredentialPool(credentials=sample_credentials)

    l1 = pool.acquire_sync()
    l2 = pool.acquire_sync()
    l3 = pool.acquire_sync()
    l4 = pool.acquire_sync()  # Round-robin picks cred-alpha again

    assert pool.in_flight_leases == 4

    rec_alpha = pool.get_record("cred-alpha")
    assert rec_alpha is not None
    assert rec_alpha.in_flight_leases == 2  # l1 and l4

    rec_beta = pool.get_record("cred-beta")
    assert rec_beta is not None
    assert rec_beta.in_flight_leases == 1

    # Report l1
    pool.report_sync(l1, Outcome.success())
    assert pool.in_flight_leases == 3
    rec_alpha_after = pool.get_record("cred-alpha")
    assert rec_alpha_after is not None
    assert rec_alpha_after.in_flight_leases == 1

    # Report remaining
    pool.report_sync(l2, Outcome.success())
    pool.report_sync(l3, Outcome.success())
    pool.report_sync(l4, Outcome.success())
    assert pool.in_flight_leases == 0
