"""Abstract Base Context Source & State Machine (Phase 2C.0)
Defines the base contract for environmental context sources with strict
lifecycle state transitions and idempotent start/stop semantics.
"""

import abc
import threading
from typing import Optional, Set, Tuple

from .contracts import ContextSourceState, InvalidStateTransitionError
from .models import ContextItem


class ContextSource(abc.ABC):
    """Abstract base class for all environmental context sources."""

    _VALID_TRANSITIONS: dict[ContextSourceState, Set[ContextSourceState]] = {
        ContextSourceState.UNINITIALIZED: {ContextSourceState.STARTING, ContextSourceState.STOPPED},
        ContextSourceState.STARTING: {ContextSourceState.RUNNING, ContextSourceState.FAULTED, ContextSourceState.STOPPING},
        ContextSourceState.RUNNING: {ContextSourceState.STOPPING, ContextSourceState.FAULTED},
        ContextSourceState.STOPPING: {ContextSourceState.STOPPED, ContextSourceState.FAULTED},
        ContextSourceState.STOPPED: {ContextSourceState.STARTING, ContextSourceState.UNINITIALIZED},
        ContextSourceState.FAULTED: {ContextSourceState.STOPPING, ContextSourceState.STOPPED},
    }

    def __init__(self, source_id: str):
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("source_id must be a non-empty string")
        self._source_id = source_id
        self._state = ContextSourceState.UNINITIALIZED
        self._state_lock = threading.Lock()

    @property
    def source_id(self) -> str:
        """Unique identifier for this context source."""
        return self._source_id

    @property
    def state(self) -> ContextSourceState:
        """Current lifecycle state of the context source."""
        with self._state_lock:
            return self._state

    def _set_state(self, new_state: ContextSourceState) -> None:
        """Atomically transitions the source to a new lifecycle state.
        Raises InvalidStateTransitionError if the transition is prohibited.
        """
        with self._state_lock:
            if self._state == new_state:
                return  # Idempotent same-state transition
            allowed = self._VALID_TRANSITIONS.get(self._state, set())
            if new_state not in allowed:
                raise InvalidStateTransitionError(
                    f"Illegal state transition for source '{self._source_id}': "
                    f"{self._state.name} -> {new_state.name}"
                )
            self._state = new_state

    @abc.abstractmethod
    async def start(self) -> None:
        """Initializes and starts the context source. Must be idempotent."""
        pass

    @abc.abstractmethod
    async def stop(self) -> None:
        """Stops and cleans up resources for the context source. Must be idempotent."""
        pass

    @abc.abstractmethod
    async def capture(self) -> Tuple[ContextItem, ...]:
        """Captures and returns the current set of context items from this source."""
        pass

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Returns True if the required OS APIs or capabilities are present on the host."""
        pass
