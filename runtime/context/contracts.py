"""Context Contracts and State Definitions (Phase 2C.0)
Defines lifecycle states and exception hierarchy for context sources and managers.
"""

from enum import Enum, auto


class ContextSourceState(Enum):
    """Lifecycle states for context sources."""
    UNINITIALIZED = "UNINITIALIZED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAULTED = "FAULTED"


class ContextError(Exception):
    """Base exception for all context subsystem errors."""
    pass


class InvalidStateTransitionError(ContextError):
    """Raised when an illegal context source state transition is attempted."""
    pass


class ContextSourceUnavailableError(ContextError):
    """Raised when a requested context source is not available or functional on the host."""
    pass


class HookShutdownTimeoutError(ContextError):
    """Raised when a native hook thread fails to terminate within its shutdown timeout."""
    pass


class ContextItemImmutableError(ContextError):
    """Raised when an attempt is made to mutate an immutable context object or insert unsupported types."""
    pass
