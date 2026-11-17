"""In-Memory Typed Event Bus Subsystem
Provides strongly typed event envelopes, isolated error boundaries,
and decoupled communication between runtime components.
"""

import time
import uuid
import logging
from dataclasses import dataclass, field
from typing import Callable, Any, Dict, List, Optional
import threading

logger = logging.getLogger("presence.events")


@dataclass(frozen=True)
class Event:
    """Immutable event envelope passed through the event bus."""
    event_id: str
    event_type: str
    timestamp_utc: float
    sequence_num: int
    payload: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, event_type: str, sequence_num: int, payload: Optional[Dict[str, Any]] = None) -> "Event":
        return cls(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            timestamp_utc=time.time(),
            sequence_num=sequence_num,
            payload=payload or {},
        )


EventHandler = Callable[[Event], None]

# Environmental Context Event Types (Phase 2C)
WINDOW_FOREGROUND_CHANGED = "WindowForegroundChanged"
CONTEXT_SNAPSHOT_CREATED = "ContextSnapshotCreated"


class EventBus:
    """Thread-safe, error-isolated in-memory event bus."""

    def __init__(self):
        self._lock = threading.RLock()
        self._subscribers: Dict[str, List[EventHandler]] = {}
        self._global_subscribers: List[EventHandler] = []
        self._sequence_counter: int = 0

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """Subscribes a handler to a specific event type."""
        with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            if handler not in self._subscribers[event_type]:
                self._subscribers[event_type].append(handler)

    def subscribe_all(self, handler: EventHandler) -> None:
        """Subscribes a handler to all events on the bus."""
        with self._lock:
            if handler not in self._global_subscribers:
                self._global_subscribers.append(handler)

    def unsubscribe(self, event_type: str, handler: EventHandler) -> bool:
        """Unsubscribes a handler from a specific event type."""
        with self._lock:
            if event_type in self._subscribers and handler in self._subscribers[event_type]:
                self._subscribers[event_type].remove(handler)
                return True
            return False

    def publish(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> Event:
        """Publishes an event synchronously to all registered listeners.
        Guarantees that an exception in one handler never halts execution of other handlers.
        """
        with self._lock:
            self._sequence_counter += 1
            seq = self._sequence_counter

            event = Event.create(event_type=event_type, sequence_num=seq, payload=payload)
            type_handlers = list(self._subscribers.get(event_type, []))
            global_handlers = list(self._global_subscribers)

        # Dispatch outside the lock to prevent deadlocks
        all_handlers = type_handlers + global_handlers
        for handler in all_handlers:
            try:
                handler(event)
            except Exception as e:
                logger.error(
                    f"Error in event handler {handler.__name__ if hasattr(handler, '__name__') else str(handler)} "
                    f"for event {event_type} ({event.event_id}): {e}",
                    exc_info=True,
                )

        return event

    def clear(self) -> None:
        """Clears all subscribers and resets sequence count (primarily for tests)."""
        with self._lock:
            self._subscribers.clear()
            self._global_subscribers.clear()
            self._sequence_counter = 0

    @property
    def total_events_published(self) -> int:
        with self._lock:
            return self._sequence_counter
