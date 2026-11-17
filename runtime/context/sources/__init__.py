"""Sources Package for Environmental Context (Phase 2C.1)
"""

from .foreground_window import (
    BoundedEventQueue,
    Win32ForegroundWindowSource,
    WinEventHookThread,
    query_live_foreground_identity,
    get_window_text_safe,
    get_window_class_safe,
    get_window_process_id_safe,
    get_process_creation_filetime_ticks_safe,
)

__all__ = [
    "BoundedEventQueue",
    "Win32ForegroundWindowSource",
    "WinEventHookThread",
    "query_live_foreground_identity",
    "get_window_text_safe",
    "get_window_class_safe",
    "get_window_process_id_safe",
    "get_process_creation_filetime_ticks_safe",
]
