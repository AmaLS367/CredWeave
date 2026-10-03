"""Unit tests for the RoundRobinStrategy."""

import concurrent.futures
from collections.abc import Sequence

from credweave.application.ports.strategy import (
    CredentialCandidate,
    SelectionContext,
    SelectionStrategy,
)
from credweave.domain.enums import CredentialState
from credweave.domain.models import Credential
from credweave.strategies.round_robin import RoundRobinStrategy


def _make_candidates(
    creds: Sequence[Credential],
    states: Sequence[CredentialState] | None = None,
) -> list[CredentialCandidate]:
    if states is None:
        states = [CredentialState.AVAILABLE] * len(creds)
    return [CredentialCandidate(credential=c, state=s) for c, s in zip(creds, states, strict=False)]


def test_round_robin_strategy_protocol_conformance() -> None:
    """Verify RoundRobinStrategy satisfies SelectionStrategy protocol."""
    strat = RoundRobinStrategy()
    assert isinstance(strat, SelectionStrategy)
    assert strat.name == "round_robin"


def test_round_robin_empty_and_ineligible(sample_credential: Credential) -> None:
    """Verify select returns None when candidate list is empty or none are AVAILABLE."""
    strat = RoundRobinStrategy()
    assert strat.select([]) is None

    unavailable_candidate = CredentialCandidate(
        credential=sample_credential,
        state=CredentialState.RATE_LIMITED,
    )
    assert strat.select([unavailable_candidate]) is None


def test_round_robin_single_candidate(sample_credential: Credential) -> None:
    """Verify single candidate is repeatedly selected."""
    strat = RoundRobinStrategy()
    candidate = CredentialCandidate(
        credential=sample_credential,
        state=CredentialState.AVAILABLE,
    )
    for _ in range(5):
        selected = strat.select([candidate])
        assert selected is candidate


def test_round_robin_cyclic_order(sample_credentials: list[Credential]) -> None:
    """Verify cyclic selection order across 3 candidates."""
    strat = RoundRobinStrategy()
    candidates = _make_candidates(sample_credentials)

    selections = [strat.select(candidates) for _ in range(6)]
    selected_ids = [s.credential_id for s in selections if s is not None]

    assert selected_ids == [
        "cred-alpha",
        "cred-beta",
        "cred-gamma",
        "cred-alpha",
        "cred-beta",
        "cred-gamma",
    ]


def test_round_robin_skips_unavailable_candidate(sample_credentials: list[Credential]) -> None:
    """Verify strategy skips candidates not in AVAILABLE state."""
    strat = RoundRobinStrategy()
    cands_all = _make_candidates(sample_credentials)

    # 1. First pick is alpha
    sel1 = strat.select(cands_all)
    assert sel1 is not None
    assert sel1.credential_id == "cred-alpha"

    # 2. Beta becomes RATE_LIMITED
    cands_with_cooldown = _make_candidates(
        sample_credentials,
        [CredentialState.AVAILABLE, CredentialState.RATE_LIMITED, CredentialState.AVAILABLE],
    )
    sel2 = strat.select(cands_with_cooldown)
    # Skips beta, picks gamma
    assert sel2 is not None
    assert sel2.credential_id == "cred-gamma"

    # 3. Next pick wraps around to alpha
    sel3 = strat.select(cands_with_cooldown)
    assert sel3 is not None
    assert sel3.credential_id == "cred-alpha"

    # 4. Beta becomes AVAILABLE again
    sel4 = strat.select(cands_all)
    assert sel4 is not None
    assert sel4.credential_id == "cred-beta"


def test_round_robin_context_tag_filtering(sample_credentials: list[Credential]) -> None:
    """Verify filtering by required_tags in SelectionContext."""
    strat = RoundRobinStrategy()
    candidates = _make_candidates(sample_credentials)

    # 'fast' tag is on alpha and beta
    ctx_fast = SelectionContext(required_tags=frozenset({"fast"}))
    s1 = strat.select(candidates, ctx_fast)
    s2 = strat.select(candidates, ctx_fast)
    s3 = strat.select(candidates, ctx_fast)

    assert s1 is not None
    assert s1.credential_id == "cred-alpha"
    assert s2 is not None
    assert s2.credential_id == "cred-beta"
    assert s3 is not None
    assert s3.credential_id == "cred-alpha"

    # 'backup' tag is only on gamma
    ctx_backup = SelectionContext(required_tags=frozenset({"backup"}))
    s_backup = strat.select(candidates, ctx_backup)
    assert s_backup is not None
    assert s_backup.credential_id == "cred-gamma"


def test_round_robin_concurrent_selection(sample_credentials: list[Credential]) -> None:
    """Verify thread-safe concurrent selection across threads without exceptions."""
    strat = RoundRobinStrategy()
    candidates = _make_candidates(sample_credentials)

    def worker() -> str:
        res = strat.select(candidates)
        assert res is not None
        return res.credential_id

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(worker) for _ in range(100)]
        results = [f.result() for f in futures]

    assert len(results) == 100
    assert set(results) == {"cred-alpha", "cred-beta", "cred-gamma"}
