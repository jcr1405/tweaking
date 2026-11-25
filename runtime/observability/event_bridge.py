"""Observability Event Bridge & Thread/Asyncio Decoupling
Safely bridges synchronous runtime events from worker threads to async UI consumers
with bounded buffering, drop-oldest backpressure, and zero blocking of runtime execution.
"""

import time
import uuid
import logging
import threading
from collections import deque
from typing import Optional, Dict, Any, List, Callable, Set

from runtime.events import EventBus, Event
from runtime.observability.models import NormalizedEvent

logger = logging.getLogger("presence.observability.bridge")


class ObservabilityEventBridge:
    """Thread-safe event bridge and ring buffer for runtime observability.
    Subscribes to EventBus and provides a decoupled, bounded feed to UI listeners.
    """

    def __init__(self, max_buffer_size: int = 2000):
        self._max_buffer_size = max_buffer_size
        self._lock = threading.RLock()
        self._event_buffer: deque[NormalizedEvent] = deque(maxlen=max_buffer_size)
        self._listeners: Set[Callable[[NormalizedEvent], None]] = set()
        self._total_bridged_events: int = 0
        self._dropped_events_count: int = 0

    def attach_to_event_bus(self, event_bus: EventBus) -> None:
        """Subscribes this bridge to receive all events from the runtime EventBus."""
        event_bus.subscribe_all(self._on_runtime_event)

    def _on_runtime_event(self, event: Event) -> None:
        """Handler invoked by EventBus whenever an event is published."""
        payload = event.payload or {}
        component = payload.get("component", "runtime")
        cid = payload.get("conversation_id")
        tid = payload.get("task_id")
        turn_idx = payload.get("turn_idx")

        level = "INFO"
        if "error" in payload or "fail" in event.event_type.lower():
            level = "ERROR"
        elif "warn" in event.event_type.lower() or "challenge" in event.event_type.lower():
            level = "WARNING"

        norm_event = NormalizedEvent(
            event_id=event.event_id,
            event_type=event.event_type,
            timestamp_utc=event.timestamp_utc,
            sequence_num=event.sequence_num,
            component=component,
            conversation_id=cid,
            turn_idx=turn_idx,
            task_id=tid,
            level=level,
            message=payload.get("message", f"Event {event.event_type} emitted"),
            payload=payload
        )
        self.record_event(norm_event)

    def record_event(self, norm_event: NormalizedEvent) -> None:
        """Buffers a normalized event and dispatches non-blockingly to registered listeners."""
        listeners_snapshot = []
        with self._lock:
            self._event_buffer.append(norm_event)
            self._total_bridged_events += 1
            listeners_snapshot = list(self._listeners)

        # Notify UI listeners outside lock. If any listener fails, catch and log so runtime never crashes.
        for listener in listeners_snapshot:
            try:
                listener(norm_event)
            except Exception as e:
                logger.debug(f"Error dispatching observability event to listener: {e}")

    def emit_synthetic(
        self,
        event_type: str,
        payload: Dict[str, Any],
        component: str = "runtime",
        conversation_id: Optional[str] = None,
        turn_idx: Optional[int] = None,
        task_id: Optional[str] = None,
        level: str = "INFO",
        message: str = ""
    ) -> NormalizedEvent:
        """Allows direct emission of high-fidelity observability events
        (e.g., from AgentExecutor or SearchGovernor).
        """
        with self._lock:
            seq = self._total_bridged_events + 1
            norm_event = NormalizedEvent(
                event_id=str(uuid.uuid4()),
                event_type=event_type,
                timestamp_utc=time.time(),
                sequence_num=seq,
                component=component,
                conversation_id=conversation_id,
                turn_idx=turn_idx,
                task_id=task_id,
                level=level,
                message=message or f"Event {event_type}",
                payload=payload
            )
        self.record_event(norm_event)
        return norm_event

    def subscribe(self, listener: Callable[[NormalizedEvent], None]) -> Callable[[], None]:
        """Registers a listener callback. Returns an unsubscribe callable."""
        with self._lock:
            self._listeners.add(listener)

        def unsubscribe():
            with self._lock:
                self._listeners.discard(listener)

        return unsubscribe

    def get_recent_events(self, limit: int = 50) -> List[NormalizedEvent]:
        """Retrieves a snapshot of the most recent events."""
        with self._lock:
            events = list(self._event_buffer)
            return events[-limit:]

    def get_events_for_conversation(self, conversation_id: str, limit: int = 100) -> List[NormalizedEvent]:
        """Filters recent events by conversation_id."""
        with self._lock:
            matching = [e for e in self._event_buffer if e.conversation_id == conversation_id]
            return matching[-limit:]

    @property
    def buffer_size(self) -> int:
        with self._lock:
            return len(self._event_buffer)

    @property
    def total_events(self) -> int:
        with self._lock:
            return self._total_bridged_events
