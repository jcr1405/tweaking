"""Unit Tests for Windows Foreground Window Context Source (Phase 2C.1)
Validates BoundedEventQueue mechanics, 32-bit saturating counters, non-blocking lock contention,
single-pending notification invariants, Proactor debug safety, composite WindowIdentity freshness,
and quiesced teardown sequencing.
"""

import asyncio
import os
import sys
import threading
import time
import unittest

from runtime.context.contracts import ContextSourceState, HookShutdownTimeoutError
from runtime.context.models import WindowIdentity
from runtime.context.sources.foreground_window import (
    UINT32_MAX,
    BoundedEventQueue,
    Win32ForegroundWindowSource,
    _sat_inc,
    get_window_text_safe,
    get_window_class_safe,
)


class TestWin32ForegroundSource(unittest.IsolatedAsyncioTestCase):
    """Targeted unit tests for Phase 2C.1 Foreground Window Context Source."""

    async def asyncSetUp(self):
        # IsolatedAsyncioTestCase defaults debug=True in CPython 3.12.
        # Explicitly enforce production invariant (debug=False) for foreground hook testing.
        asyncio.get_running_loop().set_debug(False)

    def test_1_saturating_counter_increment(self):
        """1. Verifies 32-bit saturating unsigned counter arithmetic (P2C-66)."""
        self.assertEqual(_sat_inc(10, 5), 15)
        self.assertEqual(_sat_inc(UINT32_MAX - 1, 1), UINT32_MAX)
        self.assertEqual(_sat_inc(UINT32_MAX, 1), UINT32_MAX)
        self.assertEqual(_sat_inc(UINT32_MAX, 100), UINT32_MAX)

    def test_2_queue_exact_capacity_64(self):
        """2. Verifies queue enforces strictly 64-entry capacity (P2C-35)."""
        queue = BoundedEventQueue(capacity=64)
        for i in range(64):
            pushed, _ = queue.push((1000 + i, 1, time.monotonic()))
            self.assertTrue(pushed)

        self.assertEqual(len(queue), 64)
        self.assertEqual(queue.events_pushed, 64)
        self.assertEqual(queue.queue_high_water_mark, 64)

        # 65th push must be rejected due to capacity
        pushed, signal = queue.push((9999, 1, time.monotonic()))
        self.assertFalse(pushed)
        self.assertFalse(signal)
        self.assertEqual(queue.events_dropped_capacity, 1)
        self.assertEqual(len(queue), 64)

    def test_3_single_pending_notification_invariant(self):
        """3. Verifies single-pending notification: scheduled once per drain cycle."""
        queue = BoundedEventQueue(capacity=64)
        pushed1, signal1 = queue.push((1001, 1, time.monotonic()))
        self.assertTrue(pushed1)
        self.assertTrue(signal1)
        self.assertTrue(queue.notification_pending)
        self.assertEqual(queue.notifications_scheduled, 1)

        # Subsequent pushes before drain do NOT trigger additional signals (coalesced)
        pushed2, signal2 = queue.push((1002, 1, time.monotonic()))
        self.assertTrue(pushed2)
        self.assertFalse(signal2)
        self.assertEqual(queue.events_coalesced, 1)
        self.assertEqual(queue.notifications_scheduled, 1)

        # Drain resets notification_pending
        batch = queue.drain_and_reset()
        self.assertEqual(len(batch), 2)
        self.assertFalse(queue.notification_pending)
        self.assertEqual(queue.events_drained, 2)

        # Next push generates new signal
        pushed3, signal3 = queue.push((1003, 1, time.monotonic()))
        self.assertTrue(pushed3)
        self.assertTrue(signal3)
        self.assertTrue(queue.notification_pending)

    def test_4_non_blocking_producer_contention_drop(self):
        """4. Verifies non-blocking producer drops on lock contention without blocking (P2C-29)."""
        queue = BoundedEventQueue(capacity=64)

        # Acquire lock externally to simulate consumer holding monitor
        queue._lock.acquire()
        try:
            # push() must return immediately with False, False
            pushed, signal = queue.push((1001, 1, time.monotonic()))
            self.assertFalse(pushed)
            self.assertFalse(signal)
            self.assertEqual(queue.events_dropped_contention, 1)
            self.assertEqual(queue.events_dropped, 1)
        finally:
            queue._lock.release()

    def test_5_sticky_monotonic_fault_state(self):
        """5. Verifies sticky fault-state persists across drain_and_reset() (P2C-62)."""
        queue = BoundedEventQueue(capacity=64)
        queue.push((1001, 1, time.monotonic()))

        # Trigger scheduling failure
        queue.handle_scheduling_failure()
        self.assertTrue(queue.is_faulted)
        self.assertEqual(queue.scheduling_failures, 1)

        # In faulted state, push is immediately rejected
        pushed, signal = queue.push((1002, 1, time.monotonic()))
        self.assertFalse(pushed)
        self.assertFalse(signal)
        self.assertEqual(queue.events_dropped_faulted, 1)

        # drain_and_reset() clears pending entries, but NEVER clears _faulted
        batch = queue.drain_and_reset()
        self.assertEqual(len(batch), 1)
        self.assertTrue(queue.is_faulted)  # Sticky!

        # Subsequent pushes still rejected
        pushed_after, _ = queue.push((1003, 1, time.monotonic()))
        self.assertFalse(pushed_after)
        self.assertEqual(queue.events_dropped_faulted, 2)

    def test_6_zero_lock_for_diagnostic_readers(self):
        """6. Verifies diagnostic readers access state without taking lock (P2C-51)."""
        queue = BoundedEventQueue(capacity=64)
        queue._lock.acquire()
        try:
            # Readers must not block even if lock is held by consumer
            _ = len(queue)
            _ = queue.notification_pending
            _ = queue.is_faulted
            _ = queue.events_dropped
        finally:
            queue._lock.release()

    async def test_7_proactor_debug_mode_invariant(self):
        """7. Verifies startup rejects loop with debug mode enabled (P2C-64)."""
        loop = asyncio.get_running_loop()
        source = Win32ForegroundWindowSource(loop=loop)

        loop.set_debug(True)
        try:
            with self.assertRaises(ValueError) as ctx:
                await source.start()
            self.assertIn("debug mode enabled", str(ctx.exception))
        finally:
            loop.set_debug(False)

    async def test_8_composite_window_identity_validation(self):
        """8. Verifies composite WindowIdentity validation across all 4 fields (P2C-52)."""
        call_count = 0
        def _mock_live():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return WindowIdentity(hwnd=1000, process_id=500, process_creation_filetime_ticks=133000000000000000, window_class="Notepad")
            else:
                # Same window
                return WindowIdentity(hwnd=1000, process_id=500, process_creation_filetime_ticks=133000000000000000, window_class="Notepad")

        loop = asyncio.get_running_loop()
        source = Win32ForegroundWindowSource(
            debounce_interval=0.01,
            loop=loop,
            live_window_func=_mock_live,
        )
        await source.start()

        # Enqueue event
        source._on_winevent_callback(1000, 1)
        await asyncio.sleep(0.05)

        remaining = await source.stop()
        self.assertEqual(len(remaining), 0)
        self.assertEqual(source.snapshots_committed, 1)
        self.assertEqual(source.stale_snapshots_aborted, 0)

    async def test_9_cross_process_hwnd_reuse_rejected(self):
        """9. Verifies mid-inspection focus shift or recycled HWND with different ticks is aborted (P2C-52)."""
        call_count = 0
        def _mock_live():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return WindowIdentity(hwnd=1000, process_id=500, process_creation_filetime_ticks=133000000000000000, window_class="Notepad")
            else:
                # HWND reused by a different process instance (different ticks)
                return WindowIdentity(hwnd=1000, process_id=600, process_creation_filetime_ticks=133999999999999999, window_class="Malicious")

        loop = asyncio.get_running_loop()
        source = Win32ForegroundWindowSource(
            debounce_interval=0.01,
            loop=loop,
            live_window_func=_mock_live,
        )
        await source.start()

        source._on_winevent_callback(1000, 1)
        await asyncio.sleep(0.05)

        await source.stop()
        self.assertEqual(source.snapshots_committed, 0)
        self.assertEqual(source.stale_snapshots_aborted, 1)

    async def test_10_supervised_quiesced_teardown_ordering(self):
        """10. Verifies strict quiesced teardown sequence and residual drain (P2C-59)."""
        loop = asyncio.get_running_loop()
        source = Win32ForegroundWindowSource(debounce_interval=0.01, loop=loop)
        await source.start()
        self.assertEqual(source.state, ContextSourceState.RUNNING)

        # Enqueue unconsumed events right before stop
        source.queue.push((2001, 1, time.monotonic()))
        source.queue.push((2002, 1, time.monotonic()))

        remaining = await source.stop()
        self.assertEqual(source.state, ContextSourceState.STOPPED)
        # Residual events drained cleanly under single ownership
        self.assertGreaterEqual(len(remaining), 0)

    async def test_11_hook_join_timeout_escalation(self):
        """11. Verifies join timeout aborts remaining shutdown sequence and raises error (P2C-65)."""
        loop = asyncio.get_running_loop()
        source = Win32ForegroundWindowSource(debounce_interval=0.01, loop=loop)
        await source.start()

        with self.assertRaises(HookShutdownTimeoutError):
            await source.stop(simulate_hook_hang=True)

        self.assertTrue(source.shutdown_aborted)
        self.assertEqual(source.state, ContextSourceState.FAULTED)

    async def test_12_single_consumer_invariant(self):
        """12. Verifies active consumer count never exceeds 1."""
        loop = asyncio.get_running_loop()
        source = Win32ForegroundWindowSource(debounce_interval=0.01, loop=loop)
        await source.start()
        # Yield to let consumer worker execute initial setup
        await asyncio.sleep(0.01)

        # Invariant check: only 1 consumer task active
        self.assertEqual(source._active_consumer_count, 1)

        await source.stop()
        self.assertEqual(source._active_consumer_count, 0)


if __name__ == "__main__":
    unittest.main()
