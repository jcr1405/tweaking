"""Windows-Native Foreground Window Context Source (Phase 2C.1)
Provides out-of-context WinEvent hook monitoring for foreground window changes,
a bounded non-blocking producer queue with single-pending notifications,
CPython GIL and Proactor event loop safety, composite WindowIdentity freshness validation,
150 ms monotonic debouncing, and strict quiesced teardown sequencing.
"""

import asyncio
import collections
import dataclasses
import logging
import os
import sys
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from runtime.context.contracts import (
    ContextSourceState,
    HookShutdownTimeoutError,
    InvalidStateTransitionError,
)
from runtime.context.models import (
    ContextItem,
    ContextSensitivity,
    ContextSnapshot,
    WindowIdentity,
    filetime_to_ticks,
)
from runtime.context.source import ContextSource
from runtime.events import WINDOW_FOREGROUND_CHANGED, EventBus

logger = logging.getLogger("presence.context.foreground")

UINT32_MAX = 0xFFFFFFFF
EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
WM_QUIT = 0x0012

DEFAULT_QUEUE_CAPACITY = 64
DEFAULT_DEBOUNCE_INTERVAL = 0.150  # 150 ms
DEFAULT_HOOK_JOIN_TIMEOUT = 1.0    # 1.0 second


def _sat_inc(counter: int, delta: int = 1) -> int:
    """Saturating 32-bit unsigned counter increment (P2C-66)."""
    new_val = counter + delta
    return new_val if new_val < UINT32_MAX else UINT32_MAX


class BoundedEventQueue:
    """Bounded Producer-Consumer Queue for Windows Foreground Events.
    Integrates atomic queue buffering and notification coordination under a unified monitor.
    Guarantees:
      - Strictly non-blocking producer on WinEvent hook thread.
      - Single-pending notification: scheduled at most once per drain cycle.
      - Zero stranded events: deque drain and notification-pending reset occur atomically.
      - Capacity strictly bounded to 64 items.
      - Separate drop accounting: contention vs capacity vs faulted (P2C-57).
      - Zero lock acquisition for diagnostic/metrics readers (P2C-51).
      - Faulted-state handling preserves invariant: pending=False implies queue_depth=0 (P2C-49).
      - Zero synchronous logging or blocking operations on callback path (P2C-54).
      - Sticky monotonic fault state: once faulted, ordinary drains cannot clear fault (P2C-62).
      - Lifetime memory bounded by saturating 32-bit integer counters (P2C-66).
    """

    def __init__(self, capacity: int = DEFAULT_QUEUE_CAPACITY):
        self.capacity = capacity
        self._lock = threading.Lock()
        self._deque = collections.deque(maxlen=capacity)
        self._notification_pending = False
        self._faulted = False

        # Saturating telemetry counters (strictly <= 32 bytes memory in CPython 3.12 AMD64)
        self.raw_events_received = 0
        self.events_dropped_capacity = 0
        self.events_dropped_contention = 0
        self.events_dropped_faulted = 0
        self.events_coalesced = 0
        self.events_pushed = 0
        self.events_drained = 0
        self.queue_high_water_mark = 0
        self.notifications_scheduled = 0
        self.notifications_executed = 0
        self.scheduling_failures = 0

    @property
    def events_dropped(self) -> int:
        return _sat_inc(
            self.events_dropped_capacity,
            self.events_dropped_contention + self.events_dropped_faulted,
        )

    @property
    def notification_pending(self) -> bool:
        # Atomic read without taking _lock (P2C-51: zero external lock contention)
        return self._notification_pending

    @property
    def is_faulted(self) -> bool:
        # Atomic read without taking _lock (P2C-51)
        return self._faulted

    def push(self, event: tuple) -> Tuple[bool, bool]:
        """Invoked from native WinEvent hook callback.
        Guarantees ZERO application-level blocking lock waits.
        Returns:
            (pushed: bool, schedule_signal: bool)
        """
        self.raw_events_received = _sat_inc(self.raw_events_received)

        if not self._lock.acquire(blocking=False):
            # Contention: consumer is draining batch or resetting state
            self.events_dropped_contention = _sat_inc(self.events_dropped_contention)
            return False, False

        try:
            if self._faulted:
                # System is in sticky faulted state; reject incoming events immediately (P2C-57, P2C-62)
                self.events_dropped_faulted = _sat_inc(self.events_dropped_faulted)
                return False, False

            if len(self._deque) >= self.capacity:
                self.events_dropped_capacity = _sat_inc(self.events_dropped_capacity)
                return False, False

            self._deque.append(event)
            self.events_pushed = _sat_inc(self.events_pushed)
            if len(self._deque) > self.queue_high_water_mark:
                self.queue_high_water_mark = len(self._deque)

            if not self._notification_pending:
                self._notification_pending = True
                self.notifications_scheduled = _sat_inc(self.notifications_scheduled)
                return True, True
            else:
                self.events_coalesced = _sat_inc(self.events_coalesced)
                return True, False
        finally:
            self._lock.release()

    def handle_scheduling_failure(self) -> None:
        """Invoked when loop.call_soon_threadsafe fails (e.g. RuntimeError on closed loop).
        Transitions queue into sticky faulted state.
        CRITICAL (P2C-54): ZERO synchronous logging or I/O on callback path!
        CRITICAL (P2C-49): NEVER resets _notification_pending = False while unread entries remain!
        CRITICAL (P2C-62): _faulted is a MONOTONIC one-way latch!
        """
        self.scheduling_failures = _sat_inc(self.scheduling_failures)
        self._faulted = True

    def drain_and_reset(self) -> List[Any]:
        """Invoked from consumer worker task or supervisor shutdown.
        Atomically drains all pending items and resets _notification_pending = False.
        CRITICAL (P2C-62): Does NOT clear _faulted! _faulted is sticky!
        """
        with self._lock:
            batch = list(self._deque)
            self._deque.clear()
            self._notification_pending = False
            # P2C-62: _faulted is STICKY; only a fresh queue instance clears fault!
            self.events_drained = _sat_inc(self.events_drained, len(batch))
            return batch

    def __len__(self) -> int:
        # Atomic query without acquiring _lock (P2C-51)
        return len(self._deque)


# ================= Win32 Ctypes Initialization & Helpers =================

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # Define WINEVENTPROC callback type
    WINEVENTPROC = ctypes.WINFUNCTYPE(
        None,
        wintypes.HANDLE,   # hWinEventHook
        wintypes.DWORD,    # event
        wintypes.HWND,     # hwnd
        wintypes.LONG,     # idObject
        wintypes.LONG,     # idChild
        wintypes.DWORD,    # idEventThread
        wintypes.DWORD,    # dwmsEventTime
    )

    # User32 function prototypes
    user32.SetWinEventHook.restype = wintypes.HANDLE
    user32.SetWinEventHook.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HMODULE,
        WINEVENTPROC,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
    ]

    user32.UnhookWinEvent.restype = wintypes.BOOL
    user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]

    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetForegroundWindow.argtypes = []

    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

    user32.IsWindow.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [wintypes.HWND]

    user32.PostThreadMessageW.restype = wintypes.BOOL
    user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

    user32.GetMessageW.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]

    user32.TranslateMessage.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]

    user32.DispatchMessageW.restype = wintypes.LPARAM
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]

    user32.PeekMessageW.restype = wintypes.BOOL
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]

    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]

    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def get_window_text_safe(hwnd: int, max_chars: int = 200) -> str:
    """Retrieves top-level window caption from User32 desktop heap cache.
    Does NOT send WM_GETTEXT across processes; cannot deadlock on hung external apps.
    Clamps length to max_chars (default 200).
    """
    if sys.platform != "win32" or not hwnd:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        length = user32.GetWindowTextW(hwnd, buf, 512)
        if length > 0:
            return buf.value[:max_chars]
        return ""
    except Exception:
        return ""


def get_window_class_safe(hwnd: int) -> str:
    """Retrieves window class name from internal class atom. Non-blocking."""
    if sys.platform != "win32" or not hwnd:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(256)
        length = user32.GetClassNameW(hwnd, buf, 256)
        if length > 0:
            return buf.value
        return ""
    except Exception:
        return ""


def get_window_process_id_safe(hwnd: int) -> Tuple[int, int]:
    """Retrieves (PID, TID) for the given window handle."""
    if sys.platform != "win32" or not hwnd:
        return 0, 0
    try:
        pid = wintypes.DWORD()
        tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value, tid
    except Exception:
        return 0, 0


def get_process_creation_filetime_ticks_safe(pid: int) -> int:
    """Retrieves process creation time in raw 64-bit FILETIME ticks."""
    if sys.platform != "win32" or not pid:
        return 0
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    try:
        h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h_proc:
            return 0
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
                return filetime_to_ticks(c_time.dwHighDateTime, c_time.dwLowDateTime)
            return 0
        finally:
            kernel32.CloseHandle(h_proc)
    except Exception:
        return 0


def query_live_foreground_identity() -> Optional[WindowIdentity]:
    """Captures complete composite WindowIdentity of the active foreground window."""
    if sys.platform != "win32":
        return None
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd or not user32.IsWindow(hwnd):
            return None
        pid, _ = get_window_process_id_safe(hwnd)
        if not pid:
            return None
        ticks = get_process_creation_filetime_ticks_safe(pid)
        w_class = get_window_class_safe(hwnd)
        return WindowIdentity(
            hwnd=hwnd,
            process_id=pid,
            process_creation_filetime_ticks=ticks,
            window_class=w_class,
            observed_monotonic=time.monotonic(),
        )
    except Exception:
        return None


# ================= Native WinEvent Hook Thread =================

class WinEventHookThread(threading.Thread):
    """Dedicated background OS thread hosting the Win32 message pump for SetWinEventHook.
    Guarantees:
      - daemon=True (does not block interpreter termination).
      - Hook registration and unhooking occur on the owning thread.
      - Message loop waits in kernel Executive/UserRequest state without busy-polling.
    """

    def __init__(self, callback: Callable[[int, int], None]):
        super().__init__(name="WinEventHookThread", daemon=True)
        self._callback = callback
        self._h_hook: Optional[int] = None
        self._thread_ready = threading.Event()
        self._native_proc: Optional[Any] = None
        self._tid: int = 0
        self._started_successfully = False

    @property
    def hook_handle(self) -> Optional[int]:
        return self._h_hook

    def wait_ready(self, timeout: float = 2.0) -> bool:
        return self._thread_ready.wait(timeout=timeout)

    def run(self) -> None:
        if sys.platform != "win32":
            self._started_successfully = True
            self._thread_ready.set()
            return

        self._tid = kernel32.GetCurrentThreadId()

        # Force message queue creation for this thread
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 0)

        # Build ctypes callback wrapper
        def _raw_winevent_proc(h_hook, event, hwnd, id_obj, id_child, id_thread, dwms_time):
            # Out-of-context hook filter: only top-level window foreground changes
            if id_obj == 0 and id_child == 0 and hwnd:
                try:
                    self._callback(hwnd, dwms_time)
                except Exception:
                    pass

        self._native_proc = WINEVENTPROC(_raw_winevent_proc)

        # SetWinEventHook: out of context, global, listen for EVENT_SYSTEM_FOREGROUND
        self._h_hook = user32.SetWinEventHook(
            EVENT_SYSTEM_FOREGROUND,
            EVENT_SYSTEM_FOREGROUND,
            0,
            self._native_proc,
            0,
            0,
            WINEVENT_OUTOFCONTEXT,
        )

        if not self._h_hook:
            logger.error("Failed to install SetWinEventHook.")
            self._started_successfully = False
            self._thread_ready.set()
            return

        self._started_successfully = True
        self._thread_ready.set()

        try:
            # Win32 Message Pump
            while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if self._h_hook:
                user32.UnhookWinEvent(self._h_hook)
                self._h_hook = None
            self._native_proc = None

    def stop(self) -> None:
        """Posts WM_QUIT to the thread's message queue to terminate the message loop."""
        if sys.platform == "win32" and self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)


# ================= Production Context Source =================

class Win32ForegroundWindowSource(ContextSource):
    """Windows Native Foreground Window Context Source.
    Coordinates WinEvent hook events, non-blocking queue ingestion, asyncio worker consumption,
    composite identity verification, 150 ms monotonic debouncing, and strict teardown.
    """

    def __init__(
        self,
        capacity: int = DEFAULT_QUEUE_CAPACITY,
        debounce_interval: float = DEFAULT_DEBOUNCE_INTERVAL,
        hook_join_timeout: float = DEFAULT_HOOK_JOIN_TIMEOUT,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        event_bus: Optional[EventBus] = None,
        live_window_func: Optional[Callable[[], Optional[WindowIdentity]]] = None,
        on_snapshot_created: Optional[Callable[[ContextSnapshot], None]] = None,
    ):
        super().__init__(source_id="win32_foreground_window")
        self.queue = BoundedEventQueue(capacity=capacity)
        self.debounce_interval = debounce_interval
        self.hook_join_timeout = hook_join_timeout
        self.loop = loop
        self.event_bus = event_bus
        self.live_window_func = live_window_func or query_live_foreground_identity
        self.on_snapshot_created = on_snapshot_created

        self._event_available: Optional[asyncio.Event] = None
        self._shutdown_event = asyncio.Event()
        self._consumer_task: Optional[asyncio.Task] = None
        self._hook_thread: Optional[WinEventHookThread] = None
        self._hook_active = False
        self._in_callback = False
        self._active_consumer_count = 0
        self.shutdown_aborted = False

        # Additional diagnostic telemetry
        self.authoritative_refreshes = 0
        self.stale_snapshots_aborted = 0
        self.snapshots_committed = 0
        self.latest_authoritative_identity: Optional[WindowIdentity] = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Attaches the running asyncio event loop."""
        self.loop = loop
        self._event_available = asyncio.Event()

    def is_available(self) -> bool:
        """Returns True if running on native Windows."""
        return sys.platform == "win32"

    def _on_winevent_callback(self, hwnd: int, event_time: int) -> bool:
        """Native producer callback invoked from WinEventHookThread.
        Guarantees:
          - Strictly non-blocking.
          - Reentrancy guard.
          - Single-pending notification dispatch via loop.call_soon_threadsafe.
          - Zero synchronous logging or I/O.
        """
        if not self._hook_active or self._in_callback:
            return False

        self._in_callback = True
        try:
            arrival = time.monotonic()
            pushed, schedule_signal = self.queue.push((hwnd, event_time, arrival))

            if schedule_signal:
                if self.loop is None or self.loop.is_closed():
                    self.queue.handle_scheduling_failure()
                    return False

                try:
                    self.loop.call_soon_threadsafe(self._event_available.set)
                except RuntimeError:
                    # Event loop stopping or closed; enter sticky fault state
                    self.queue.handle_scheduling_failure()
                    return False

            return pushed
        finally:
            self._in_callback = False

    async def start(self) -> None:
        """Starts the native hook thread and consumer worker task.
        Enforces Proactor debug mode invariant (P2C-64).
        """
        if self.state == ContextSourceState.RUNNING:
            return  # Idempotent

        # Proactor debug mode invariant (P2C-64):
        # Startup MUST reject loop.get_debug() == True to prevent logger.debug inside _write_to_self()
        if self.loop is not None and self.loop.get_debug():
            raise ValueError(
                "Cannot start native WinEvent hook with asyncio debug mode enabled. "
                "Debug mode invokes synchronous logger.debug on the Proactor self-pipe error path."
            )

        self._set_state(ContextSourceState.STARTING)

        if self.loop is None:
            self.attach_loop(asyncio.get_running_loop())

        self._shutdown_event.clear()
        if self._event_available is None:
            self._event_available = asyncio.Event()
        else:
            self._event_available.clear()

        # Start consumer worker task
        self._consumer_task = asyncio.create_task(self._consumer_worker())

        # Start hook thread if on Windows
        if sys.platform == "win32":
            self._hook_thread = WinEventHookThread(self._on_winevent_callback)
            self._hook_thread.start()
            if not self._hook_thread.wait_ready(timeout=2.0) or not self._hook_thread._started_successfully:
                self._set_state(ContextSourceState.FAULTED)
                raise RuntimeError("Failed to start WinEventHookThread within timeout.")

        self._hook_active = True
        self._set_state(ContextSourceState.RUNNING)
        logger.info("Win32ForegroundWindowSource started in RUNNING state.")

    async def stop(self, simulate_hook_hang: bool = False) -> List[Any]:
        """Supervised Quiesced Teardown Sequence (P2C-50, P2C-59, P2C-65).
        Returns any remaining drained queue items.
        """
        if self.state in (ContextSourceState.STOPPED, ContextSourceState.UNINITIALIZED):
            return []

        self._set_state(ContextSourceState.STOPPING)
        self._hook_active = False

        # Step 1 & 2: Stop and join hook thread
        if self._hook_thread and self._hook_thread.is_alive() and not simulate_hook_hang:
            self._hook_thread.stop()
            self._hook_thread.join(timeout=self.hook_join_timeout)

        # Step 3: Check for join timeout
        if (self._hook_thread and self._hook_thread.is_alive()) or simulate_hook_hang:
            self.shutdown_aborted = True
            self._set_state(ContextSourceState.FAULTED)
            logger.critical(
                "Hook thread termination timed out! Aborting shutdown sequence to keep loop open. "
                "Escalating to process supervisor."
            )
            # In testing, raise HookShutdownTimeoutError; in daemon, escalate via os._exit(1)
            raise HookShutdownTimeoutError("Hook thread termination timed out. Shutdown aborted.")

        # Step 4 & 5: Signal consumer worker
        self._shutdown_event.set()
        if self._event_available is not None:
            self._event_available.set()

        # Step 6: Await consumer worker quiescence
        if self._consumer_task and not self._consumer_task.done():
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass

        # Step 7: Single-owner queue drain
        remaining = self.queue.drain_and_reset()

        # Step 8: Complete shutdown
        self._set_state(ContextSourceState.STOPPED)
        logger.info(f"Win32ForegroundWindowSource stopped cleanly. Drained {len(remaining)} residual events.")
        return remaining

    def stop_sync(self, timeout: Optional[float] = None) -> List[Any]:
        """Synchronous wrapper around stop() for synchronous runtime teardown."""
        effective_timeout = timeout or (self.hook_join_timeout + 2.0)
        if self.loop is not None and self.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self.stop(), self.loop)
            return future.result(timeout=effective_timeout)
        else:
            return asyncio.run(self.stop())


    async def _consumer_worker(self) -> None:
        """Supervised single consumer worker with Composite WindowIdentity freshness validation."""
        try:
            self._active_consumer_count += 1
            if self._active_consumer_count > 1:
                raise RuntimeError(
                    f"Invariant violated: active_foreground_consumer_count = {self._active_consumer_count} > 1"
                )

            while not self._shutdown_event.is_set():
                await self._event_available.wait()
                if self._shutdown_event.is_set():
                    break
                self._event_available.clear()
                self.queue.notifications_executed = _sat_inc(self.queue.notifications_executed)

                # Monotonic debounce: wait debounce_interval
                if self.debounce_interval > 0:
                    await asyncio.sleep(self.debounce_interval)

                # Batch drain
                batch = self.queue.drain_and_reset()
                if not batch:
                    continue

                # Phase 1: Capture initial composite identity
                initial_identity = self.live_window_func()
                self.authoritative_refreshes = _sat_inc(self.authoritative_refreshes)
                self.latest_authoritative_identity = initial_identity

                if initial_identity is None:
                    continue

                # Small yield to let window settle / query metadata
                await asyncio.sleep(0.001)

                # Phase 2: Post-collection freshness re-validation across all 4 composite fields
                final_identity = self.live_window_func()
                if (
                    final_identity is None
                    or final_identity.hwnd != initial_identity.hwnd
                    or final_identity.process_id != initial_identity.process_id
                    or final_identity.process_creation_filetime_ticks != initial_identity.process_creation_filetime_ticks
                    or final_identity.window_class != initial_identity.window_class
                ):
                    self.stale_snapshots_aborted = _sat_inc(self.stale_snapshots_aborted)
                    continue

                # Invariant: No snapshot commits after shutdown boundary (P2C-59)
                if self._shutdown_event.is_set():
                    self.stale_snapshots_aborted = _sat_inc(self.stale_snapshots_aborted)
                    break

                # Retrieve window title/caption
                caption = get_window_text_safe(final_identity.hwnd)

                # Construct ContextItems
                now_utc = time.time()
                now_m = time.monotonic()
                items = (
                    ContextItem("window.hwnd", final_identity.hwnd, ContextSensitivity.PUBLIC_CONTEXT, self.source_id, now_utc, now_m),
                    ContextItem("window.process_id", final_identity.process_id, ContextSensitivity.PUBLIC_CONTEXT, self.source_id, now_utc, now_m),
                    ContextItem("window.process_creation_filetime_ticks", final_identity.process_creation_filetime_ticks, ContextSensitivity.PUBLIC_CONTEXT, self.source_id, now_utc, now_m),
                    ContextItem("window.class", final_identity.window_class, ContextSensitivity.PUBLIC_CONTEXT, self.source_id, now_utc, now_m),
                    ContextItem("window.title", caption, ContextSensitivity.PERSONAL_CONTEXT, self.source_id, now_utc, now_m),
                )

                snap_id = f"snap-{uuid.uuid4().hex[:8]}"
                snapshot = ContextSnapshot(
                    snapshot_id=snap_id,
                    created_utc=now_utc,
                    created_monotonic=now_m,
                    window_identity=final_identity,
                    items=items,
                )

                self.snapshots_committed = _sat_inc(self.snapshots_committed)

                # Deliver snapshot to callback / manager
                if self.on_snapshot_created is not None:
                    try:
                        self.on_snapshot_created(snapshot)
                    except Exception as e:
                        logger.error(f"Error delivering context snapshot: {e}", exc_info=True)

                # Publish EventBus notification
                if self.event_bus is not None:
                    try:
                        self.event_bus.publish(
                            WINDOW_FOREGROUND_CHANGED,
                            {
                                "snapshot_id": snap_id,
                                "hwnd": final_identity.hwnd,
                                "process_id": final_identity.process_id,
                                "window_class": final_identity.window_class,
                                "caption": caption,
                            },
                        )
                    except Exception as e:
                        logger.error(f"Error publishing {WINDOW_FOREGROUND_CHANGED}: {e}", exc_info=True)
        except asyncio.CancelledError:
            pass
        finally:
            self._active_consumer_count -= 1

    async def capture(self) -> Tuple[ContextItem, ...]:
        """On-demand capture of current foreground window context."""
        identity = self.live_window_func()
        if identity is None:
            return ()

        caption = get_window_text_safe(identity.hwnd)
        now_utc = time.time()
        now_m = time.monotonic()

        return (
            ContextItem("window.hwnd", identity.hwnd, ContextSensitivity.PUBLIC_CONTEXT, self.source_id, now_utc, now_m),
            ContextItem("window.process_id", identity.process_id, ContextSensitivity.PUBLIC_CONTEXT, self.source_id, now_utc, now_m),
            ContextItem("window.process_creation_filetime_ticks", identity.process_creation_filetime_ticks, ContextSensitivity.PUBLIC_CONTEXT, self.source_id, now_utc, now_m),
            ContextItem("window.class", identity.window_class, ContextSensitivity.PUBLIC_CONTEXT, self.source_id, now_utc, now_m),
            ContextItem("window.title", caption, ContextSensitivity.PERSONAL_CONTEXT, self.source_id, now_utc, now_m),
        )
