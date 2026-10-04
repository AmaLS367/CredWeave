"""Shared helpers for tests that drive the lease registry of a state store directly.

The registry (``reserve_lease`` / ``settle_lease`` / ``reclaim_expired_leases``) is the only way
to change in-flight accounting, so tests that need "N concurrent leases" open real leases.
"""

import itertools
from collections import Counter
from datetime import datetime

from credweave.application.ports.state_store import (
    LeaseReservation,
    LeaseSettlement,
    StateStore,
)
from credweave.domain.outcomes import Outcome

_lease_ids = itertools.count(1)


def next_lease_id() -> str:
    """Return a process-unique lease id."""
    return f"test-lease-{next(_lease_ids)}"


def open_leases(
    store: StateStore,
    credential_id: str,
    count: int,
    now: datetime,
    *,
    max_concurrency: int | None = None,
    expires_at: datetime | None = None,
) -> list[str]:
    """Reserve ``count`` leases, asserting each reservation succeeds; return their ids."""
    ids: list[str] = []
    for _ in range(count):
        lease_id = next_lease_id()
        result = store.reserve_lease(
            credential_id,
            lease_id,
            now,
            max_concurrency=max_concurrency,
            expires_at=expires_at,
        )
        assert result is LeaseReservation.RESERVED, result
        ids.append(lease_id)
    return ids


def settle(
    store: StateStore,
    credential_id: str,
    lease_id: str,
    outcome: Outcome,
    now: datetime,
) -> None:
    """Settle a lease, asserting it was active and unexpired."""
    result = store.settle_lease(lease_id, credential_id, outcome, now)
    assert result is LeaseSettlement.SETTLED, result


def assert_lease_accounting(store: StateStore) -> None:
    """Assert the registry and the per-credential counters agree exactly.

    For every credential, ``in_flight_leases`` must equal the number of registered leases that
    belong to it, hence the grand totals match too, and no lease may reference a missing record.
    """
    registered = Counter(lease.credential_id for lease in store.list_active_leases())
    records = {record.credential_id: record for record in store.list_records()}

    for credential_id, count in registered.items():
        assert credential_id in records, f"lease registered for unknown credential {credential_id}"
        assert records[credential_id].in_flight_leases == count, (
            f"{credential_id}: in_flight_leases={records[credential_id].in_flight_leases} "
            f"but {count} leases are registered"
        )
    for credential_id, record in records.items():
        assert record.in_flight_leases == registered.get(credential_id, 0), (
            f"{credential_id}: in_flight_leases={record.in_flight_leases} "
            f"but {registered.get(credential_id, 0)} leases are registered"
        )
        assert record.in_flight_leases >= 0
        assert record.total_leases >= record.in_flight_leases
    assert sum(r.in_flight_leases for r in records.values()) == len(store.list_active_leases())
