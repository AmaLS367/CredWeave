"""Deterministic tests for store-owned secret generations and stale-snapshot refusal."""

import hashlib
from datetime import datetime, timedelta

import pytest

from credweave import CredentialState, Outcome
from credweave.application.ports.state_store import LeaseReservation, LeaseSettlement
from credweave.domain._security import compute_secrets_fingerprint
from credweave.domain.models import Credential
from credweave.infrastructure.stores.memory import MemoryStateStore
from tests.conftest import TestClock
from tests.lease_helpers import assert_lease_accounting


def _fp(secret: str) -> str:
    return Credential(id="c1", secrets={"key": secret}).secret_fingerprint


def _reserve(store: MemoryStateStore, lease_id: str, now: datetime, fingerprint: str) -> object:
    return store.reserve_lease("c1", lease_id, now, secret_fingerprint=fingerprint)


def _lease(store: MemoryStateStore, lease_id: str) -> object:
    return next(lease for lease in store.list_active_leases() if lease.lease_id == lease_id)


def test_first_fingerprint_is_generation_one_and_stamps_leases() -> None:
    """The first secret the store sees is generation 1, and leases record it."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    store.sync_credential("c1", _fp("A"))

    assert _reserve(store, "l1", clock.now(), _fp("A")) is LeaseReservation.RESERVED
    lease = _lease(store, "l1")
    assert lease.secret_generation == 1
    assert lease.secret_fingerprint == _fp("A")


def test_rotation_advances_generation_without_recovering_revoked_credential() -> None:
    """A new fingerprint is the next generation but never reactivates a REVOKED credential."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    store.sync_credential("c1", _fp("A"))
    _reserve(store, "l1", clock.now(), _fp("A"))
    store.settle_lease("l1", "c1", Outcome.auth_failed(reason="revoked"), clock.now())
    assert store.get_record("c1").state is CredentialState.REVOKED

    record = store.sync_credential("c1", _fp("B"))
    assert record.state is CredentialState.REVOKED
    assert _reserve(store, "l2", clock.now(), _fp("B")) is LeaseReservation.INELIGIBLE

    store.authorize_secret("c1", _fp("B"))
    assert _reserve(store, "l3", clock.now(), _fp("B")) is LeaseReservation.RESERVED
    assert _lease(store, "l3").secret_generation == 2


@pytest.mark.parametrize(
    "state",
    [CredentialState.AVAILABLE, CredentialState.REVOKED, CredentialState.UNHEALTHY],
)
def test_unseen_fingerprint_never_changes_lifecycle_state(state: CredentialState) -> None:
    """Synchronisation adopts an unseen secret but leaves state, failures and cooldown alone."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    store.sync_credential("c1", _fp("A"))
    cooldown_until = clock.now() + timedelta(seconds=30)
    store.update_state("c1", state, cooldown_until=cooldown_until)
    before = store.get_record("c1")

    after = store.sync_credential("c1", _fp("B"))
    assert after.state is before.state
    assert after.consecutive_failures == before.consecutive_failures
    assert after.cooldown_until == before.cooldown_until


def test_authorize_recovers_revoked_credential_under_current_secret_without_new_generation() -> (
    None
):
    """Authorizing the secret already active resets the state but keeps the generation."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    store.sync_credential("c1", _fp("A"))
    store.update_state("c1", CredentialState.REVOKED)

    record = store.authorize_secret("c1", _fp("A"))
    assert record.state is CredentialState.AVAILABLE
    assert _reserve(store, "l1", clock.now(), _fp("A")) is LeaseReservation.RESERVED
    assert _lease(store, "l1").secret_generation == 1


def test_authorize_rolls_back_to_earlier_secret_under_a_new_generation() -> None:
    """Rollback re-activates A, but leases granted under A's first generation stay isolated."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    store.sync_credential("c1", _fp("A"))
    _reserve(store, "old_a", clock.now(), _fp("A"))
    store.sync_credential("c1", _fp("B"))
    _reserve(store, "b", clock.now(), _fp("B"))
    store.settle_lease("b", "c1", Outcome.auth_failed(reason="B revoked"), clock.now())
    assert store.get_record("c1").state is CredentialState.REVOKED

    store.authorize_secret("c1", _fp("A"))
    assert store.get_record("c1").state is CredentialState.AVAILABLE
    assert _reserve(store, "a2", clock.now(), _fp("A")) is LeaseReservation.RESERVED
    assert _lease(store, "a2").secret_generation == 3
    assert _reserve(store, "stale_b", clock.now(), _fp("B")) is LeaseReservation.STALE

    settlement = store.settle_lease(
        "old_a", "c1", Outcome.permanent_failure(reason="old A"), clock.now()
    )
    assert settlement is LeaseSettlement.SETTLED
    assert store.get_record("c1").state is CredentialState.AVAILABLE
    assert store.get_record("c1").consecutive_failures == 0
    assert_lease_accounting(store)


def test_authorize_keeps_in_flight_counters_and_clears_failures_and_cooldown() -> None:
    """Authorization resets health but must not lose the slots of leases still in flight."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    store.sync_credential("c1", _fp("A"))
    _reserve(store, "l1", clock.now(), _fp("A"))
    store.update_state("c1", CredentialState.UNHEALTHY)

    record = store.authorize_secret("c1", _fp("A"))
    assert record.in_flight_leases == 1
    assert record.total_leases == 1
    assert record.cooldown_until is None
    assert_lease_accounting(store)


def test_history_is_bounded_and_truncation_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A forgotten secret cannot be replayed by sync; explicit authorization still works."""
    monkeypatch.setattr(MemoryStateStore, "_SECRET_HISTORY_LIMIT", 2)
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    store.sync_credential("c1", _fp("A"))
    store.sync_credential("c1", _fp("B"))
    store.sync_credential("c1", _fp("C"))  # evicts A; history is now truncated

    replay = store.sync_credential("c1", _fp("A"))
    assert replay.state is CredentialState.AVAILABLE
    assert _reserve(store, "replay", clock.now(), _fp("A")) is LeaseReservation.STALE

    store.sync_credential("c1", _fp("D"))  # unseen, but truncated: refused, not adopted
    assert _reserve(store, "d", clock.now(), _fp("D")) is LeaseReservation.STALE
    assert _reserve(store, "c", clock.now(), _fp("C")) is LeaseReservation.RESERVED
    assert _lease(store, "c").secret_generation == 3

    store.authorize_secret("c1", _fp("D"))
    assert _reserve(store, "d2", clock.now(), _fp("D")) is LeaseReservation.RESERVED
    assert _lease(store, "d2").secret_generation == 4


def test_adopted_history_stays_within_the_limit_under_many_rotations() -> None:
    """Memory per credential is bounded: once truncated, further unseen rotations are refused."""
    limit = MemoryStateStore._SECRET_HISTORY_LIMIT
    store = MemoryStateStore(clock=TestClock())
    for n in range(3 * limit):
        store.sync_credential("c1", _fp(f"secret-{n}"))

    generations = store._secret_generations["c1"]
    assert len(generations.adopted) == limit
    assert generations.truncated is True
    # Generations 1..limit fill the history; generation limit+1 forgets the oldest and truncates.
    # Every later unseen secret is refused, so the generation stops growing.
    assert generations.generation == limit + 1
    assert _fp(f"secret-{3 * limit - 1}") not in generations.adopted


def test_superseded_fingerprint_is_refused_and_changes_nothing() -> None:
    """A stale snapshot's sync and reservation leave records and leases exactly as they were."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    store.sync_credential("c1", _fp("A"))
    store.sync_credential("c1", _fp("B"))
    before_records = store.list_records()
    before_leases = store.list_active_leases()

    store.sync_credential("c1", _fp("A"))
    assert _reserve(store, "stale", clock.now(), _fp("A")) is LeaseReservation.STALE

    assert store.list_records() == before_records
    assert store.list_active_leases() == before_leases


def test_stale_source_cannot_reactivate_revoked_credential() -> None:
    """A stale secret must not recover a credential revoked under the newer secret."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    store.sync_credential("c1", _fp("A"))
    store.sync_credential("c1", _fp("B"))
    _reserve(store, "l1", clock.now(), _fp("B"))
    store.settle_lease("l1", "c1", Outcome.auth_failed(reason="revoked"), clock.now())

    record = store.sync_credential("c1", _fp("A"))
    assert record.state is CredentialState.REVOKED
    assert _reserve(store, "stale", clock.now(), _fp("A")) is LeaseReservation.STALE
    assert _reserve(store, "current", clock.now(), _fp("B")) is LeaseReservation.INELIGIBLE
    assert store.get_record("c1").state is CredentialState.REVOKED


def test_superseded_snapshot_does_not_clear_cooldown_of_current_credential() -> None:
    """A stale sync must leave the cooldown of the current credential untouched."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    store.sync_credential("c1", _fp("A"))
    cooldown_until = clock.now() + timedelta(seconds=60)
    store.update_state("c1", CredentialState.COOLDOWN, cooldown_until=cooldown_until)
    store.sync_credential("c1", _fp("B"))

    store.sync_credential("c1", _fp("A"))
    record = store.get_record("c1")
    assert record.state is CredentialState.COOLDOWN
    assert record.cooldown_until == cooldown_until
    assert _reserve(store, "stale", clock.now(), _fp("A")) is LeaseReservation.STALE
    assert _reserve(store, "current", clock.now(), _fp("B")) is LeaseReservation.INELIGIBLE


def test_reverted_secret_is_not_readopted_and_old_lease_outcome_is_discarded() -> None:
    """A->B->A: a lease from the first A cannot revoke the credential after the revert."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    store.sync_credential("c1", _fp("A"))
    assert _reserve(store, "old", clock.now(), _fp("A")) is LeaseReservation.RESERVED
    store.sync_credential("c1", _fp("B"))
    store.sync_credential("c1", _fp("A"))

    settlement = store.settle_lease("old", "c1", Outcome.auth_failed(reason="old A"), clock.now())
    assert settlement is LeaseSettlement.SETTLED
    record = store.get_record("c1")
    assert record.state is CredentialState.AVAILABLE
    assert record.consecutive_failures == 0
    assert_lease_accounting(store)


def test_pre_rotation_lease_releases_slot_without_applying_outcome() -> None:
    """Leases from an older generation free their slot but leave health untouched."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    store.sync_credential("c1", _fp("A"))
    _reserve(store, "old", clock.now(), _fp("A"))
    store.sync_credential("c1", _fp("B"))
    _reserve(store, "new", clock.now(), _fp("B"))

    settlement = store.settle_lease("old", "c1", Outcome.permanent_failure(reason="x"), clock.now())
    assert settlement is LeaseSettlement.SETTLED
    record = store.get_record("c1")
    assert record.state is CredentialState.AVAILABLE
    assert record.in_flight_leases == 1
    assert_lease_accounting(store)


def test_rotation_keeps_pre_rotation_leases_counted_against_concurrency_cap() -> None:
    """Concurrency is per credential, so a superseded lease still occupies its slot."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    store.sync_credential("c1", _fp("A"))
    assert (
        store.reserve_lease(
            "c1", "old", clock.now(), max_concurrency=1, secret_fingerprint=_fp("A")
        )
        is LeaseReservation.RESERVED
    )
    store.sync_credential("c1", _fp("B"))

    result = store.reserve_lease(
        "c1", "new", clock.now(), max_concurrency=1, secret_fingerprint=_fp("B")
    )
    assert result is LeaseReservation.AT_CAPACITY
    assert_lease_accounting(store)


def test_every_fingerprint_is_adopted_at_most_once() -> None:
    """Re-presenting an adopted secret never creates a generation, so churn always ends."""
    store = MemoryStateStore(clock=TestClock())
    store.sync_credential("c1", _fp("A"))
    for _ in range(3):
        store.sync_credential("c1", _fp("B"))
        store.sync_credential("c1", _fp("A"))
    store.sync_credential("c1", _fp("C"))

    clock = TestClock()
    assert _reserve(store, "c", clock.now(), _fp("C")) is LeaseReservation.RESERVED
    assert _lease(store, "c").secret_generation == 3


def test_reservation_without_prior_observation_adopts_its_secret() -> None:
    """A reservation that carries a secret for an unseen credential makes it the baseline."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    assert _reserve(store, "first", clock.now(), _fp("A")) is LeaseReservation.RESERVED
    assert _lease(store, "first").secret_generation == 1

    store.sync_credential("c1", _fp("B"))
    assert _reserve(store, "second", clock.now(), _fp("A")) is LeaseReservation.STALE


def test_non_reserved_result_does_not_adopt_the_secret() -> None:
    """Losing a reservation race (here: INELIGIBLE) must not register the caller's secret."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    store.update_state("c1", CredentialState.REVOKED)

    assert _reserve(store, "l1", clock.now(), _fp("A")) is LeaseReservation.INELIGIBLE
    assert store.list_active_leases() == ()

    store.reset("c1")
    assert _reserve(store, "l2", clock.now(), _fp("B")) is LeaseReservation.RESERVED
    assert _lease(store, "l2").secret_generation == 1


def test_expired_superseded_lease_is_reclaimed_without_outcome() -> None:
    """Reclaiming a lease from an older generation changes no health state."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    store.sync_credential("c1", _fp("A"))
    store.reserve_lease(
        "c1", "old", clock.now(), expires_at=clock.now(), secret_fingerprint=_fp("A")
    )
    store.sync_credential("c1", _fp("B"))
    clock.advance(3600)

    reclaimed = store.reclaim_expired_leases(clock.now())
    assert [lease.lease_id for lease in reclaimed] == ["old"]
    assert store.get_record("c1").state is CredentialState.AVAILABLE
    assert_lease_accounting(store)


@pytest.mark.asyncio
async def test_async_sync_and_reserve_refuse_superseded_secret() -> None:
    """The async store API applies the same generation rules as the sync one."""
    clock = TestClock()
    store = MemoryStateStore(clock=clock)
    await store.sync_credential_async("c1", _fp("A"))
    await store.sync_credential_async("c1", _fp("B"))

    stale = await store.reserve_lease_async("c1", "stale", clock.now(), secret_fingerprint=_fp("A"))
    assert stale is LeaseReservation.STALE

    current = await store.reserve_lease_async(
        "c1", "current", clock.now(), secret_fingerprint=_fp("B")
    )
    assert current is LeaseReservation.RESERVED
    assert _lease(store, "current").secret_generation == 2
    assert_lease_accounting(store)


def test_fingerprint_is_keyed_so_an_exposed_value_cannot_be_checked_offline() -> None:
    """An unkeyed digest of the secret must not match the fingerprint a lease exposes."""
    secret = "sk-very-guessable"
    plain = hashlib.sha256(b"key\x00" + secret.encode() + b"\x00").hexdigest()

    fingerprint = compute_secrets_fingerprint({"key": secret})
    assert fingerprint != plain
    assert len(fingerprint) == 64
    assert fingerprint == compute_secrets_fingerprint({"key": secret})
    assert fingerprint != compute_secrets_fingerprint({"key": "sk-other"})
