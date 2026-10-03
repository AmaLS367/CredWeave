"""Infrastructure layer containing adapters for clocks, sources, and state stores."""

from credweave.infrastructure.clocks.system import SystemClock
from credweave.infrastructure.sources.static import StaticSource
from credweave.infrastructure.stores.memory import MemoryStateStore

__all__ = [
    "MemoryStateStore",
    "StaticSource",
    "SystemClock",
]
