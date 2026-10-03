"""Composition root configuring default adapters for CredWeave.

This module provides the wiring between concrete infrastructure adapters
and application services, preserving the Clean Architecture inward dependency rule.
"""

from credweave.application.services.pool import register_default_adapters
from credweave.infrastructure.clocks.system import SystemClock
from credweave.infrastructure.sources.static import StaticSource
from credweave.infrastructure.stores.memory import MemoryStateStore
from credweave.strategies.round_robin import RoundRobinStrategy


def configure_default_adapters() -> None:
    """Wire default infrastructure adapters into the application layer."""
    register_default_adapters(
        clock_factory=SystemClock,
        strategy_factory=RoundRobinStrategy,
        source_factory=lambda creds: StaticSource(creds),
        store_factory=lambda clock, cooldown, max_failures: MemoryStateStore(
            clock=clock,
            default_cooldown=cooldown,
            max_consecutive_failures=max_failures,
        ),
    )
