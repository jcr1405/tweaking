"""Context Coordinator and Manager (Phase 2C.0)
Coordinates environmental context sources, manages the active transient snapshot,
enforces single-snapshot retention (no history buffer), and dispatches lifecycle events.
"""

import logging
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .contracts import ContextSourceState, InvalidStateTransitionError
from .expiration import is_snapshot_expired
from .models import ContextItem, ContextSensitivity, ContextSnapshot, WindowIdentity
from .source import ContextSource

logger = logging.getLogger("presence.context.manager")


class MockContextSource(ContextSource):
    """Mock context source for deterministic testing without native OS hooks."""

    def __init__(self, source_id: str = "mock_source", available: bool = True):
        super().__init__(source_id=source_id)
        self._available = available
        self._mock_items: List[ContextItem] = []

    def set_mock_items(self, items: List[ContextItem]) -> None:
        self._mock_items = list(items)

    def is_available(self) -> bool:
        return self._available

    async def start(self) -> None:
        if self.state == ContextSourceState.RUNNING:
            return  # Idempotent
        self._set_state(ContextSourceState.STARTING)
        self._set_state(ContextSourceState.RUNNING)

    async def stop(self) -> None:
        if self.state in (ContextSourceState.STOPPED, ContextSourceState.UNINITIALIZED):
            return  # Idempotent
        self._set_state(ContextSourceState.STOPPING)
        self._set_state(ContextSourceState.STOPPED)

    async def capture(self) -> Tuple[ContextItem, ...]:
        return tuple(self._mock_items)


class ContextManager:
    """Central coordinator for environmental context sources and active snapshot.
    Enforces strict architectural guarantees:
      - Single transient current snapshot.
      - Zero ambient window history ring buffer.
      - Zero automatic database or file persistence on window changes.
      - Monotonic TTL expiration evaluation on access (zero background polling threads).
    """

    def __init__(self, event_bus: Optional[Any] = None):
        self._event_bus = event_bus
        self._lock = threading.Lock()
        self._sources: Dict[str, ContextSource] = {}
        self._current_snapshot: Optional[ContextSnapshot] = None

    def register_source(self, source: ContextSource) -> None:
        """Registers a context source with the manager."""
        with self._lock:
            if source.source_id in self._sources:
                logger.warning(f"Context source '{source.source_id}' already registered; replacing.")
            self._sources[source.source_id] = source

    def unregister_source(self, source_id: str) -> Optional[ContextSource]:
        """Unregisters and returns a context source if present."""
        with self._lock:
            return self._sources.pop(source_id, None)

    def get_source(self, source_id: str) -> Optional[ContextSource]:
        """Retrieves a registered context source by its identifier."""
        with self._lock:
            return self._sources.get(source_id)

    @property
    def registered_sources(self) -> Tuple[str, ...]:
        """Returns identifiers of all registered context sources."""
        with self._lock:
            return tuple(self._sources.keys())

    async def start(self) -> None:
        """Starts all available registered context sources."""
        with self._lock:
            sources = list(self._sources.values())

        for source in sources:
            if source.is_available():
                try:
                    await source.start()
                except Exception as e:
                    logger.error(f"Failed to start context source '{source.source_id}': {e}", exc_info=True)
            else:
                logger.debug(f"Skipping unavailable context source '{source.source_id}'")

    async def stop(self) -> None:
        """Stops all registered context sources."""
        with self._lock:
            sources = list(self._sources.values())

        for source in sources:
            try:
                await source.stop()
            except Exception as e:
                logger.error(f"Error stopping context source '{source.source_id}': {e}", exc_info=True)

        with self._lock:
            # Drop current snapshot reference upon stop
            self._current_snapshot = None

    def set_current_snapshot(self, snapshot: Optional[ContextSnapshot]) -> None:
        """Atomically replaces the active transient context snapshot.
        Overwrites any previous snapshot (zero history ring buffer).
        Publishes 'ContextSnapshotCreated' event if configured.
        """
        with self._lock:
            self._current_snapshot = snapshot

        if snapshot is not None and self._event_bus is not None:
            try:
                self._event_bus.publish(
                    "ContextSnapshotCreated",
                    {
                        "snapshot_id": snapshot.snapshot_id,
                        "created_utc": snapshot.created_utc,
                        "item_count": len(snapshot.items),
                        "has_window": snapshot.window_identity is not None,
                        "hwnd": snapshot.window_identity.hwnd if snapshot.window_identity else None,
                        "process_id": snapshot.window_identity.process_id if snapshot.window_identity else None,
                    },
                )
            except Exception as e:
                logger.error(f"Failed to publish ContextSnapshotCreated event: {e}", exc_info=True)

    def get_current_snapshot(self, current_monotonic: Optional[float] = None) -> Optional[ContextSnapshot]:
        """Retrieves the active context snapshot.
        If the snapshot has exceeded its monotonic TTL, it is cleared and None is returned.
        Evaluates TTL strictly on-demand (zero polling threads).
        """
        if current_monotonic is None:
            current_monotonic = time.monotonic()

        with self._lock:
            if self._current_snapshot is None:
                return None

            if is_snapshot_expired(self._current_snapshot, current_monotonic):
                logger.debug("Active context snapshot expired; discarding.")
                self._current_snapshot = None
                return None

            return self._current_snapshot

    def clear(self) -> None:
        """Explicitly discards the active snapshot."""
        with self._lock:
            self._current_snapshot = None
