"""Integration tests verifying top-level package exports and public API surface."""

import credweave
from credweave import (
    Clock,
    Credential,
    CredentialPool,
    CredentialState,
    CredWeaveError,
    Lease,
    Outcome,
    OutcomeType,
    SystemClock,
    __version__,
)


def test_top_level_exports() -> None:
    """Verify that all core public primitives are directly importable from credweave."""
    assert Credential is not None
    assert CredentialPool is not None
    assert Outcome is not None
    assert OutcomeType is not None
    assert CredentialState is not None
    assert Lease is not None
    assert CredWeaveError is not None
    assert Clock is not None
    assert SystemClock is not None


def test_package_version_format() -> None:
    """Verify that __version__ is a valid version string."""
    assert isinstance(__version__, str)
    assert len(__version__.split(".")) >= 2


def test_all_contains_expected_symbols() -> None:
    """Verify __all__ is properly defined and does not leak private modules."""
    for symbol in credweave.__all__:
        assert hasattr(credweave, symbol), f"Symbol {symbol} in __all__ not found in package"

    # Private modules must not be in __all__
    assert "_internal" not in credweave.__all__
    assert "models" not in credweave.__all__
    assert "pool" not in credweave.__all__


def test_submodule_exports() -> None:
    """Verify that domain, application, infrastructure, and strategies submodules export cleanly."""
    from credweave.application import CredentialPool as AppPool
    from credweave.domain import Credential as DomCred
    from credweave.infrastructure import SystemClock as InfraClock
    from credweave.strategies import (
        CredentialCandidate,
        SelectionContext,
        SelectionStrategy,
    )

    assert AppPool is CredentialPool
    assert DomCred is Credential
    assert InfraClock is SystemClock
    assert CredentialCandidate is not None
    assert SelectionContext is not None
    assert SelectionStrategy is not None
