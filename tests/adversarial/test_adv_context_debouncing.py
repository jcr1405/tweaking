"""Adversarial Tests for Context Debouncing, Burst Coalescing & Queue Saturation (Phase 2C.1)
Simulates hostile event patterns: 100+ event bursts, rapid Alt-Tab oscillation,
queue capacity overflow, multi-threaded lock contention, and repeated lifecycles.
"""

import asyncio
import threading
import time
import unittest

from runtime.context.contracts import ContextSourceState
from runtime.context.models import WindowIdentity
from runtime.context.sources.foreground_window import (
    BoundedEventQueue,
    Win32ForegroundWindowSource,
)


class TestAdvContextDebouncing(unittest.IsolatedAsyncioTestCase):
    """Adversarial stress and edge-case verification for foreground context awareness."""

    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    async def test_adv_high_frequency_event_burst_coalescing(self):
        """1. Simulates 100 raw WinEvent callback arrivals within 10 ms."""
        loop = asyncio.get_running_loop()
        committed_snapshots = []

        def _on_snap(snap):
            committed_snapshots.append(snap)

        active_identity = WindowIdentity(
            hwnd=5001,
            process_id=1234,
            process_creation_filetime_ticks=133000000000000000,
            window_class="BurstClass",
        )

        source = Win32ForegroundWindowSource(
            debounce_interval=0.05,  # 50 ms debounce for fast test
            loop=loop,
            live_window_func=lambda: active_identity,
            on_snapshot_created=_on_snap,
        )
        await source.start()

        # Fire burst of 100 events
        for i in range(100):
            source._on_winevent_callback(5001, 1000 + i)

        # Allow debouncing to settle
        await asyncio.sleep(0.12)

        await source.stop()

        # Telemetry verification: 100 raw events received, high water mark <= 64
        self.assertEqual(source.queue.raw_events_received, 100)
        self.assertLessEqual(source.queue.queue_high_water_mark, 64)
        # Bounded single snapshot committed despite 100 events
        self.assertEqual(len(committed_snapshots), 1)
        self.assertEqual(source.snapshots_committed, 1)

    async def test_adv_rapid_alt_tab_oscillation(self):
        """2. Simulates rapid Alt-Tab oscillation between two windows (A <-> B)."""
        loop = asyncio.get_running_loop()
        committed_snapshots = []

        win_a = WindowIdentity(1001, 10, 133000000000000000, "WindowA")
        win_b = WindowIdentity(1002, 20, 133000000000000000, "WindowB")
        current_win = win_a

        def _live_func():
            return current_win

        source = Win32ForegroundWindowSource(
            debounce_interval=0.08,
            loop=loop,
            live_window_func=_live_func,
            on_snapshot_created=lambda s: committed_snapshots.append(s),
        )
        await source.start()

        # Rapid Alt-Tab: A -> B -> A -> B within 40 ms total (less than 80 ms debounce)
        source._on_winevent_callback(1001, 100)
        await asyncio.sleep(0.01)
        current_win = win_b
        source._on_winevent_callback(1002, 110)
        await asyncio.sleep(0.01)
        current_win = win_a
        source._on_winevent_callback(1001, 120)
        await asyncio.sleep(0.01)
        current_win = win_b
        source._on_winevent_callback(1002, 130)

        # Allow debouncing to settle on final window B
        await asyncio.sleep(0.15)

        await source.stop()

        # Only the settled window B should be committed
        self.assertGreaterEqual(len(committed_snapshots), 1)
        final_snap = committed_snapshots[-1]
        self.assertEqual(final_snap.window_identity.hwnd, 1002)
        self.assertEqual(final_snap.window_identity.window_class, "WindowB")

    def test_adv_queue_overflow_saturation(self):
        """3. Sustained burst of 200 items into 64-entry queue without drain."""
        queue = BoundedEventQueue(capacity=64)

        for i in range(200):
            queue.push((2000 + i, 1, time.monotonic()))

        self.assertEqual(queue.raw_events_received, 200)
        self.assertEqual(len(queue), 64)
        self.assertEqual(queue.events_pushed, 64)
        self.assertEqual(queue.events_dropped_capacity, 136)
        self.assertEqual(queue.queue_high_water_mark, 64)

        # Drain must yield exactly 64 items
        batch = queue.drain_and_reset()
        self.assertEqual(len(batch), 64)
        self.assertEqual(len(queue), 0)

    def test_adv_multi_threaded_lock_contention(self):
        """4. Bombards queue from 4 concurrent producer threads while main thread drains."""
        queue = BoundedEventQueue(capacity=64)
        num_threads = 4
        events_per_thread = 250
        stop_barrier = threading.Barrier(num_threads + 1)

        def _producer_task(thread_id):
            stop_barrier.wait()
            for i in range(events_per_thread):
                queue.push((thread_id * 1000 + i, 1, time.monotonic()))

        threads = [threading.Thread(target=_producer_task, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()

        # Release barrier to start all producers simultaneously
        stop_barrier.wait()

        total_drained = 0
        for _ in range(50):
            batch = queue.drain_and_reset()
            total_drained += len(batch)
            time.sleep(0.001)

        for t in threads:
            t.join()

        # Final drain
        total_drained += len(queue.drain_and_reset())

        # Accounting invariant: total received = pushed + dropped_contention + dropped_capacity + dropped_faulted
        total_received = queue.raw_events_received
        self.assertEqual(total_received, num_threads * events_per_thread)
        self.assertEqual(
            total_received,
            queue.events_pushed + queue.events_dropped_contention + queue.events_dropped_capacity + queue.events_dropped_faulted
        )
        self.assertEqual(total_drained, queue.events_drained)

    async def test_adv_repeated_lifecycle_resilience(self):
        """5. Executes 5 sequential start/stop lifecycle cycles."""
        loop = asyncio.get_running_loop()

        for cycle in range(5):
            source = Win32ForegroundWindowSource(debounce_interval=0.01, loop=loop)
            self.assertEqual(source.state, ContextSourceState.UNINITIALIZED)

            await source.start()
            self.assertEqual(source.state, ContextSourceState.RUNNING)

            # Fire test event
            source._on_winevent_callback(9000 + cycle, 1)
            await asyncio.sleep(0.03)

            remaining = await source.stop()
            self.assertEqual(source.state, ContextSourceState.STOPPED)


if __name__ == "__main__":
    unittest.main()
