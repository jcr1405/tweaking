"""Context Models, Deep Immutability & FILETIME Representations (Phase 2C.0)
Defines deeply immutable value objects for environmental context observations,
canonical 64-bit FILETIME conversions, sensitivity levels, and window identity.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import total_ordering
import sys
from typing import Any, Dict, List, Optional, Tuple, Union

from .contracts import ContextItemImmutableError

# Number of 100-nanosecond intervals between 1601-01-01 00:00:00 UTC and 1970-01-01 00:00:00 UTC:
# (369 years * 365 days + 89 leap days) * 86,400 s/day * 10,000,000 ticks/s
FILETIME_UNIX_EPOCH_OFFSET_TICKS = 116444736000000000


def filetime_to_ticks(high: int, low: int) -> int:
    """Constructs canonical 64-bit integer tick count from FILETIME high/low DWORDs."""
    return ((high & 0xFFFFFFFF) << 32) | (low & 0xFFFFFFFF)


def filetime_ticks_to_unix_parts(ticks: int) -> Tuple[int, int, int]:
    """Exact integer conversion relations without floating-point arithmetic.
    Returns: (unix_100ns, unix_ns, unix_seconds_integer)
    """
    unix_100ns = ticks - FILETIME_UNIX_EPOCH_OFFSET_TICKS
    unix_ns = unix_100ns * 100
    unix_seconds_integer = unix_100ns // 10_000_000
    return unix_100ns, unix_ns, unix_seconds_integer


def filetime_ticks_to_utc_datetime(ticks: int) -> datetime:
    """Converts FILETIME ticks to UTC datetime using pure integer arithmetic.
    Note: Python datetime has microsecond resolution (1,000 ns); sub-microsecond
    precision (unix_100ns % 10, representing 0-900 ns) is truncated.
    The raw ticks: int is never altered.
    """
    unix_100ns = ticks - FILETIME_UNIX_EPOCH_OFFSET_TICKS
    unix_microseconds = unix_100ns // 10
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return epoch + timedelta(microseconds=unix_microseconds)


@total_ordering
class ContextSensitivity(Enum):
    """Hierarchical classification of context data sensitivity."""
    PUBLIC_CONTEXT = 0
    PERSONAL_CONTEXT = 1
    SENSITIVE_CONTEXT = 2
    PROTECTED_CONTEXT = 3

    def __lt__(self, other: Any) -> bool:
        if self.__class__ is other.__class__:
            return self.value < other.value
        return NotImplemented


def deep_freeze(val: Any) -> Any:
    """Recursively converts collections to deeply immutable tuples and validates types.
    Rejects arbitrary mutable or unapproved opaque objects.
    """
    if val is None or isinstance(val, (int, float, str, bool, bytes)):
        return val
    elif isinstance(val, (list, tuple)):
        return tuple(deep_freeze(x) for x in val)
    elif isinstance(val, dict):
        # Sort keys to ensure deterministic representation
        return tuple(sorted(((str(k), deep_freeze(v)) for k, v in val.items()), key=lambda x: x[0]))
    elif isinstance(val, (set, frozenset)):
        return tuple(sorted(deep_freeze(x) for x in val))
    else:
        raise ContextItemImmutableError(
            f"Unsupported mutable or unapproved type in context: {type(val).__name__}"
        )


@dataclass(frozen=True)
class WindowIdentity:
    """Strongly bound, composite identity of an observed Windows desktop window.
    Guarantees raw integer FILETIME ticks and deep immutability.
    """
    hwnd: int
    process_id: int
    process_creation_filetime_ticks: int
    window_class: str
    application_name: str = ""
    observed_monotonic: float = 0.0
    snapshot_id: str = ""

    def __post_init__(self):
        if not isinstance(self.hwnd, int):
            raise TypeError(f"hwnd must be int, got {type(self.hwnd).__name__}")
        if not isinstance(self.process_id, int):
            raise TypeError(f"process_id must be int, got {type(self.process_id).__name__}")
        if not isinstance(self.process_creation_filetime_ticks, int):
            raise TypeError(
                f"process_creation_filetime_ticks must be int, got {type(self.process_creation_filetime_ticks).__name__}"
            )
        if not isinstance(self.window_class, str):
            raise TypeError(f"window_class must be str, got {type(self.window_class).__name__}")
        if not isinstance(self.application_name, str):
            raise TypeError(f"application_name must be str, got {type(self.application_name).__name__}")
        if not isinstance(self.observed_monotonic, (int, float)):
            raise TypeError(f"observed_monotonic must be numeric, got {type(self.observed_monotonic).__name__}")
        if not isinstance(self.snapshot_id, str):
            raise TypeError(f"snapshot_id must be str, got {type(self.snapshot_id).__name__}")

    def matches_live_window(self) -> bool:
        """Best-effort validation that the physical window has not been recycled or closed.
        Safe against handles disappearing or platforms where ctypes Win32 is unavailable.
        """
        if sys.platform != "win32":
            return True

        import ctypes
        from ctypes import wintypes

        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            # 1. Verify window handle is still recognized by User32
            if not user32.IsWindow(self.hwnd):
                return False

            # 2. Verify current owning PID matches expected PID
            current_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(self.hwnd, ctypes.byref(current_pid))
            if current_pid.value != self.process_id:
                return False

            # 3. Verify process creation time matches original process instance (TOCTOU guard)
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, current_pid.value)
            if not h_proc:
                return False

            try:
                c_time = wintypes.FILETIME()
                e_time = wintypes.FILETIME()
                k_time = wintypes.FILETIME()
                u_time = wintypes.FILETIME()
                if kernel32.GetProcessTimes(
                    h_proc,
                    ctypes.byref(c_time),
                    ctypes.byref(e_time),
                    ctypes.byref(k_time),
                    ctypes.byref(u_time),
                ):
                    live_ticks = filetime_to_ticks(c_time.dwHighDateTime, c_time.dwLowDateTime)
                    return live_ticks == self.process_creation_filetime_ticks
                return False
            finally:
                kernel32.CloseHandle(h_proc)
        except Exception:
            return False


@dataclass(frozen=True)
class ContextItem:
    """An individual piece of environmental context with sensitivity and TTL metadata."""
    key: str
    value: Any
    sensitivity: ContextSensitivity
    source_id: str
    observed_utc: float
    observed_monotonic: float
    ttl_seconds: Optional[float] = None
    metadata: Tuple[Any, ...] = ()

    def __post_init__(self):
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("ContextItem key must be a non-empty string")
        if not isinstance(self.sensitivity, ContextSensitivity):
            raise TypeError(f"sensitivity must be ContextSensitivity enum, got {type(self.sensitivity).__name__}")
        if not isinstance(self.source_id, str):
            raise TypeError("source_id must be a string")
        if not isinstance(self.observed_utc, (int, float)):
            raise TypeError("observed_utc must be numeric")
        if not isinstance(self.observed_monotonic, (int, float)):
            raise TypeError("observed_monotonic must be numeric")
        if self.ttl_seconds is not None and not isinstance(self.ttl_seconds, (int, float)):
            raise TypeError("ttl_seconds must be numeric or None")

        # Deeply freeze value and metadata to enforce alias protection and immutability
        frozen_value = deep_freeze(self.value)
        frozen_metadata = deep_freeze(self.metadata)
        object.__setattr__(self, "value", frozen_value)
        object.__setattr__(self, "metadata", frozen_metadata)


@dataclass(frozen=True)
class ContextSnapshot:
    """An immutable, consolidated snapshot of environmental context at a point in time.
    Transient by default; never persisted automatically to SQLite or disk.
    """
    snapshot_id: str
    created_utc: float
    created_monotonic: float
    window_identity: Optional[WindowIdentity] = None
    items: Tuple[ContextItem, ...] = ()

    def __post_init__(self):
        if not isinstance(self.snapshot_id, str):
            raise TypeError("snapshot_id must be a string")
        if not isinstance(self.created_utc, (int, float)):
            raise TypeError("created_utc must be numeric")
        if not isinstance(self.created_monotonic, (int, float)):
            raise TypeError("created_monotonic must be numeric")
        if self.window_identity is not None and not isinstance(self.window_identity, WindowIdentity):
            raise TypeError("window_identity must be WindowIdentity or None")

        # Ensure items is a tuple of ContextItem
        if not isinstance(self.items, tuple):
            try:
                items_tuple = tuple(self.items)
            except Exception as e:
                raise ContextItemImmutableError(f"items must be an iterable of ContextItem: {e}")
            object.__setattr__(self, "items", items_tuple)

        for item in self.items:
            if not isinstance(item, ContextItem):
                raise ContextItemImmutableError(f"All items must be ContextItem instances, got {type(item).__name__}")

    def get_item(self, key: str) -> Optional[ContextItem]:
        """Retrieves an item by its key, or None if not present."""
        for item in self.items:
            if item.key == key:
                return item
        return None

    def filter_by_sensitivity(self, max_sensitivity: ContextSensitivity) -> Tuple[ContextItem, ...]:
        """Returns items whose sensitivity level is less than or equal to max_sensitivity."""
        return tuple(item for item in self.items if item.sensitivity <= max_sensitivity)

    def is_expired(self, current_monotonic: float) -> bool:
        """Evaluates whether all items in the snapshot with a TTL have expired."""
        if not self.items:
            return False
        # If any item with a TTL has NOT expired, snapshot is still considered active
        for item in self.items:
            if item.ttl_seconds is None:
                return False  # Non-expiring item keeps snapshot active
            if (current_monotonic - item.observed_monotonic) <= item.ttl_seconds:
                return False  # At least one item is still fresh
        return True

    def to_dict(self) -> Dict[str, Any]:
        """Converts snapshot to a serializable dictionary representation."""
        return {
            "snapshot_id": self.snapshot_id,
            "created_utc": self.created_utc,
            "created_monotonic": self.created_monotonic,
            "window_identity": {
                "hwnd": self.window_identity.hwnd,
                "process_id": self.window_identity.process_id,
                "process_creation_filetime_ticks": self.window_identity.process_creation_filetime_ticks,
                "window_class": self.window_identity.window_class,
                "application_name": self.window_identity.application_name,
            } if self.window_identity else None,
            "items": [
                {
                    "key": item.key,
                    "value": item.value,
                    "sensitivity": item.sensitivity.name,
                    "source_id": item.source_id,
                    "observed_utc": item.observed_utc,
                    "observed_monotonic": item.observed_monotonic,
                    "ttl_seconds": item.ttl_seconds,
                }
                for item in self.items
            ],
        }
