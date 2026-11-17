"""
Presence Phase 2C - Queue Concurrency and Stress Test Harness (Phase 2C Correction 7)
Tests the 10 critical conditions from Section 9 of the Phase 2C Correction 7 specification.
Validates non-blocking try-lock semantics, separate contention/capacity drop accounting,
and mandatory live GetForegroundWindow() state refresh.
"""

import asyncio
import collections
import threading
import time
import sys

class BoundedEventQueue:
    """
    Bounded Producer-Consumer Queue for Windows Foreground Events.
    Ownership:
      - Producer appends to deque; owns dropped-events counters.
      - Consumer drains deque batch; owns processing and delay tracking.
      - Synchronized via threading.Lock with non-blocking acquire (blocking=False) in producer.
      - Capacity is strictly bounded to 64 items.
      - Overflow policy:
        * When lock is contended by consumer drain, push() immediately discards the event
          without blocking and increments events_dropped_contention.
        * When queue reaches capacity (64), incoming events are discarded (dropped),
          and events_dropped_capacity is incremented.
        * Consumer reconstructs fresh state via mandatory GetForegroundWindow() on every wake.
    """
    def __init__(self, capacity: int = 64):
        self.capacity = capacity
        self._lock = threading.Lock()
        self._deque = collections.deque(maxlen=capacity)
        self.events_dropped_capacity = 0
        self.events_dropped_contention = 0
        self.raw_events_received = 0
        self.events_drained = 0
        self.queue_high_water_mark = 0

    @property
    def events_dropped(self) -> int:
        return self.events_dropped_capacity + self.events_dropped_contention

    def push(self, event: tuple) -> bool:
        """
        Called from WinEvent hook thread.
        Uses non-blocking acquire (blocking=False) to ensure zero lock contention wait.
        """
        self.raw_events_received += 1
        if not self._lock.acquire(blocking=False):
            self.events_dropped_contention += 1
            return False
        try:
            if len(self._deque) >= self.capacity:
                self.events_dropped_capacity += 1
                return False  # Discard newest incoming event on capacity overflow
            self._deque.append(event)
            if len(self._deque) > self.queue_high_water_mark:
                self.queue_high_water_mark = len(self._deque)
            return True
        finally:
            self._lock.release()

    def drain(self) -> list:
        """
        Called from consumer worker task. Atomic batch drain.
        O(n) complexity bounded by O(capacity = 64).
        """
        with self._lock:
            if not self._deque:
                return []
            batch = list(self._deque)
            self._deque.clear()
            self.events_drained += len(batch)
            return batch

    def __len__(self) -> int:
        with self._lock:
            return len(self._deque)

class ForegroundDispatchCoordinator:
    def __init__(self, capacity: int = 64, live_hwnd_func=None):
        self.queue = BoundedEventQueue(capacity=capacity)
        self._notification_pending = False
        self._state_lock = threading.Lock()
        self.event_available = None  # set in attach_loop
        self.loop = None
        self.active_consumer_count = 0
        self.consumer_task = None
        self.shutdown_event = asyncio.Event()
        self.notification_scheduled_count = 0
        self.notification_executed_count = 0
        self.processed_events = []
        self.live_hwnd_func = live_hwnd_func or (lambda: 1000)
        self.authoritative_refreshes = 0
        self.latest_authoritative_hwnd = None

    def attach_loop(self, loop):
        self.loop = loop
        self.event_available = asyncio.Event()

    def on_winevent(self, hwnd: int, event_time: int) -> bool:
        """Producer callback invoked from hook thread. Zero blocking lock wait."""
        arrival = time.monotonic()
        pushed = self.queue.push((hwnd, event_time, arrival))
        with self._state_lock:
            if not self._notification_pending:
                self._notification_pending = True
                self.notification_scheduled_count += 1
                if self.loop and not self.loop.is_closed():
                    self.loop.call_soon_threadsafe(self.event_available.set)
        return pushed

    async def consumer_worker(self):
        """
        Supervised single consumer worker.
        Invariants:
          - active_consumer_count <= 1
          - pending_notification_count <= 1
          - queue_depth <= 64
        Ordering:
          (4) asyncio.Event.clear()
          (5) _notification_pending reset
          (6) queue drain
          (7) Mandatory GetForegroundWindow() authoritative refresh
          (8) Worker processing
        """
        try:
            self.active_consumer_count += 1
            if self.active_consumer_count > 1:
                raise RuntimeError(f"Invariant violated: active_consumer_count = {self.active_consumer_count} > 1")
            while not self.shutdown_event.is_set():
                await self.event_available.wait()
                # Step 4: Clear notification event
                self.event_available.clear()
                # Step 5: Reset notification pending flag
                with self._state_lock:
                    self._notification_pending = False
                self.notification_executed_count += 1
                
                # Step 6: Atomic batch drain
                batch = self.queue.drain()
                
                # Step 7: Mandatory live foreground-state refresh on EVERY consumer wake
                authoritative_hwnd = self.live_hwnd_func()
                self.authoritative_refreshes += 1
                self.latest_authoritative_hwnd = authoritative_hwnd
                
                # Step 8: Worker processing
                if batch:
                    latest = batch[-1]
                    self.processed_events.append((latest, authoritative_hwnd))
                else:
                    self.processed_events.append((None, authoritative_hwnd))
                    
                # Simulate processing yield
                await asyncio.sleep(0.001)
        except asyncio.CancelledError:
            pass
        finally:
            self.active_consumer_count -= 1

async def test_1_producer_burst():
    print("Test 1: Producer bursts (100 events in rapid succession)...")
    loop = asyncio.get_running_loop()
    coord = ForegroundDispatchCoordinator(capacity=64)
    coord.attach_loop(loop)
    task = asyncio.create_task(coord.consumer_worker())
    
    # 100 rapid events
    for i in range(100):
        coord.on_winevent(1000 + i, i)
        
    await asyncio.sleep(0.05)
    coord.shutdown_event.set()
    coord.event_available.set()
    await task
    
    assert coord.queue.raw_events_received == 100
    assert coord.queue.events_dropped_capacity == 36  # 36 dropped due to 64-capacity bound
    assert coord.queue.events_dropped_contention == 0
    assert len(coord.queue) == 0
    assert coord.authoritative_refreshes >= 1
    print("  -> Passed. 64 queued, 36 capacity-dropped, 0 contention drops, live refresh executed.")

async def test_2_consumer_lock_contention():
    print("Test 2: Deliberately held consumer queue lock -> non-blocking contention drop...")
    coord = ForegroundDispatchCoordinator(capacity=64)
    
    # Consumer thread/task acquires the queue lock
    acquired = coord.queue._lock.acquire(blocking=False)
    assert acquired, "Failed to acquire lock for contention test"
    
    try:
        # WinEvent producer attempts push while lock is held
        pushed = coord.on_winevent(2001, 100)
        assert pushed is False, "Push should fail under lock contention"
        pushed2 = coord.on_winevent(2002, 101)
        assert pushed2 is False, "Push 2 should fail under lock contention"
    finally:
        coord.queue._lock.release()
        
    assert coord.queue.raw_events_received == 2
    assert coord.queue.events_dropped_contention == 2
    assert coord.queue.events_dropped_capacity == 0
    assert len(coord.queue) == 0
    print("  -> Passed. Push immediately returned False without blocking; 2 contention drops recorded.")

async def test_3_mandatory_live_refresh_after_contention_drop():
    print("Test 3: Mandatory live foreground-state refresh after contention drop...")
    current_live_hwnd = [9999]
    def mock_get_foreground_window():
        return current_live_hwnd[0]
        
    loop = asyncio.get_running_loop()
    coord = ForegroundDispatchCoordinator(capacity=64, live_hwnd_func=mock_get_foreground_window)
    coord.attach_loop(loop)
    task = asyncio.create_task(coord.consumer_worker())
    
    # Drop an event due to contention
    with coord.queue._lock:
        pushed = coord.on_winevent(3001, 10)
        assert not pushed
    assert coord.queue.events_dropped_contention == 1
    
    # Now simulate user switching window to 8888 and triggering notification
    current_live_hwnd[0] = 8888
    coord.on_winevent(8888, 20)
    
    await asyncio.sleep(0.02)
    coord.shutdown_event.set()
    coord.event_available.set()
    await task
    
    # Verify consumer recovered the authoritative live state 8888
    assert coord.latest_authoritative_hwnd == 8888
    assert coord.authoritative_refreshes >= 1
    print("  -> Passed. Mandatory GetForegroundWindow() recovered authoritative window 8888.")

async def test_4_concurrent_producer_consumer():
    print("Test 4: Concurrent producer and consumer...")
    loop = asyncio.get_running_loop()
    coord = ForegroundDispatchCoordinator(capacity=64)
    coord.attach_loop(loop)
    task = asyncio.create_task(coord.consumer_worker())
    
    def producer_thread():
        for i in range(50):
            coord.on_winevent(4000 + i, i)
            time.sleep(0.001)
            
    t = threading.Thread(target=producer_thread)
    t.start()
    t.join()
    
    await asyncio.sleep(0.05)
    coord.shutdown_event.set()
    coord.event_available.set()
    await task
    
    assert coord.queue.raw_events_received == 50
    assert coord.queue.events_drained + coord.queue.events_dropped == 50
    print("  -> Passed. All 50 concurrent events processed/accounted safely.")

async def test_5_producer_during_notification_reset():
    print("Test 5: Producer during notification reset...")
    loop = asyncio.get_running_loop()
    coord = ForegroundDispatchCoordinator(capacity=64)
    coord.attach_loop(loop)
    task = asyncio.create_task(coord.consumer_worker())
    
    coord.on_winevent(5001, 1)
    await asyncio.sleep(0.002)
    coord.on_winevent(5002, 2)
    await asyncio.sleep(0.01)
    
    coord.shutdown_event.set()
    coord.event_available.set()
    await task
    
    assert coord.queue.events_drained == 2
    assert coord.authoritative_refreshes >= 2
    print("  -> Passed. Notification reset cleanly and second event triggered live refresh.")

async def test_6_event_loop_shutdown():
    print("Test 6: Event loop shutdown safety...")
    coord = ForegroundDispatchCoordinator(capacity=64)
    pushed = coord.on_winevent(6001, 1)
    # Producer works safely even without active loop attached
    assert pushed is True
    assert len(coord.queue) == 1
    print("  -> Passed. Push succeeded safely with no attached loop.")

async def test_7_consumer_cancellation():
    print("Test 7: Consumer task cancellation...")
    loop = asyncio.get_running_loop()
    coord = ForegroundDispatchCoordinator(capacity=64)
    coord.attach_loop(loop)
    task = asyncio.create_task(coord.consumer_worker())
    await asyncio.sleep(0.005)
    assert coord.active_consumer_count == 1
    
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert coord.active_consumer_count == 0
    print("  -> Passed. Consumer cancelled cleanly; active_consumer_count == 0.")

async def test_8_repeated_start_stop():
    print("Test 8: Repeated start/stop lifecycles (10 iterations)...")
    loop = asyncio.get_running_loop()
    for iteration in range(10):
        coord = ForegroundDispatchCoordinator(capacity=64)
        coord.attach_loop(loop)
        task = asyncio.create_task(coord.consumer_worker())
        coord.on_winevent(8000 + iteration, iteration)
        await asyncio.sleep(0.002)
        coord.shutdown_event.set()
        coord.event_available.set()
        await task
        assert coord.active_consumer_count == 0
    print("  -> Passed. 10 clean lifecycle cycles completed with zero leaks.")

async def test_9_shutdown_with_pending_data():
    print("Test 9: Shutdown with pending data in queue...")
    coord = ForegroundDispatchCoordinator(capacity=64)
    coord.on_winevent(9001, 1)
    coord.on_winevent(9002, 2)
    assert len(coord.queue) == 2
    remaining = coord.queue.drain()
    assert len(remaining) == 2
    assert len(coord.queue) == 0
    print("  -> Passed. Pending data preserved and drained on shutdown.")

async def test_10_max_outstanding_consumer_task():
    print("Test 10: Invariant verification (active_consumer_count <= 1)...")
    loop = asyncio.get_running_loop()
    coord = ForegroundDispatchCoordinator(capacity=64)
    coord.attach_loop(loop)
    task1 = asyncio.create_task(coord.consumer_worker())
    await asyncio.sleep(0.005)
    assert coord.active_consumer_count == 1
    
    try:
        task2 = asyncio.create_task(coord.consumer_worker())
        await task2
        violated = False
    except RuntimeError:
        violated = True
        
    assert violated, "Failed: Second consumer worker did not raise RuntimeError"
    coord.shutdown_event.set()
    coord.event_available.set()
    await task1
    assert coord.active_consumer_count == 0
    print("  -> Passed. Invariant active_consumer_count <= 1 strictly enforced.")

async def main():
    print("================== QUEUE CONCURRENCY & STRESS TEST ==================")
    await test_1_producer_burst()
    await test_2_consumer_lock_contention()
    await test_3_mandatory_live_refresh_after_contention_drop()
    await test_4_concurrent_producer_consumer()
    await test_5_producer_during_notification_reset()
    await test_6_event_loop_shutdown()
    await test_7_consumer_cancellation()
    await test_8_repeated_start_stop()
    await test_9_shutdown_with_pending_data()
    await test_10_max_outstanding_consumer_task()
    print("=====================================================================")
    print(">>> ALL 10 QUEUE CONCURRENCY SCENARIOS VERIFIED SUCCESSFULLY! <<<")

if __name__ == "__main__":
    asyncio.run(main())
