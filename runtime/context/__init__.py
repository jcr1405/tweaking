"""Presence Environmental Context Subsystem (Phase 2C)
Provides deeply immutable context models, lifecycle contracts, expiration evaluators,
and context source management.
"""

from .contracts import (
    ContextError,
    ContextItemImmutableError,
    ContextSourceState,
    ContextSourceUnavailableError,
    HookShutdownTimeoutError,
    InvalidStateTransitionError,
)
from .expiration import filter_unexpired_items, is_item_expired, is_snapshot_expired
from .manager import ContextManager, MockContextSource
from .models import (
    FILETIME_UNIX_EPOCH_OFFSET_TICKS,
    ContextItem,
    ContextSensitivity,
    ContextSnapshot,
    WindowIdentity,
    deep_freeze,
    filetime_ticks_to_unix_parts,
    filetime_ticks_to_utc_datetime,
    filetime_to_ticks,
)
from .source import ContextSource

__all__ = [
    "ContextError",
    "ContextItemImmutableError",
    "ContextSourceState",
    "ContextSourceUnavailableError",
    "HookShutdownTimeoutError",
    "InvalidStateTransitionError",
    "ContextSensitivity",
    "ContextItem",
    "WindowIdentity",
    "ContextSnapshot",
    "deep_freeze",
    "filetime_to_ticks",
    "filetime_ticks_to_unix_parts",
    "filetime_ticks_to_utc_datetime",
    "FILETIME_UNIX_EPOCH_OFFSET_TICKS",
    "is_item_expired",
    "is_snapshot_expired",
    "filter_unexpired_items",
    "ContextSource",
    "ContextManager",
    "MockContextSource",
]
