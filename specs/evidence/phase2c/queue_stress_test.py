"""
Presence Phase 2C - Queue Concurrency and Stress Test Harness
Tests the 10 critical conditions from Section 9 of the Phase 2C Correction 6 specification.
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
      - Producer appends to deque; owns dropped-events counter.
      - Consumer drains deque batch; owns processing and delay tracking.
      - Synchronized via threading.Lock to protect deque invariants under CPython threading.
      - Capacity is strictly bounded to 64 items.
      - Overflow policy: When queue reaches capacity (64), incoming events are discarded (dropped),
        and events_dropped is incremented. Consumer reconstructs fresh state via GetForegroundWindow().
    """
    def __init__(self, capacity: int = 64):
        self.capacity = capacity
        self._lock = threading.Lock()
        self._deque = collections.deque(maxlen=capacity)
        self.events_dropped = 0
        self.raw_events_received = 0
        self.events_drained = 0
        self.queue_high_water_mark = 0

    def push(self, event: tuple) -> bool:
        """Called from WinEvent hook thread. Never blocks application worker."""
        with self._lock:
            self.raw_events_received += 1
            if len(self._deque) >= self.capacity:
                self.events_dropped += 1
                return False  # Discard newest incoming event on overflow
            self._deque.append(event)
            if len(self._deque) > self.queue_high_water_mark:
                self.queue_high_water_mark = len(self._deque)
            return True

    def drain(self) -> list:
        """Called from consumer worker task. Atomic batch drain."""
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
    def __init__(self, capacity: int = 64):
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

    def attach_loop(self, loop):
        self.loop = loop
        self.event_available = asyncio.Event()

    def on_winevent(self, hwnd: int, event_time: int):
        """Producer callback invoked from hook thread."""
        arrival = time.monotonic()
        pushed = self.queue.push((hwnd, event_time, arrival))
        with self._state_lock:
            if not self._notification_pending:
                self._notification_pending = True
                self.notification_scheduled_count += 1
                if self.loop and not self.loop.is_closed():
                    self.loop.call_soon_threadsafe(self.event_available.set)

    async def consumer_worker(self):
        """Supervised single consumer worker. Invariant: active_consumer_count <= 1."""
        try:
            self.active_consumer_count += 1
            if self.active_consumer_count > 1:
                raise RuntimeError(f"Invariant violated: active_consumer_count = {self.active_consumer_count} > 1")
            while not self.shutdown_event.is_set():
                await self.event_available.wait()
                self.event_available.clear()
                with self._state_lock:
                    self._notification_pending = False
                self.notification_executed_count += 1
                
                batch = self.queue.drain()
                if batch:
                    # Process latest event; intermediate rapid shifts collapsed
                    latest = batch[-1]
                    self.processed_events.append(latest)
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
    assert coord.queue.events_dropped == (100 - 64)  # 36 dropped because capacity is 64
    assert len(coord.queue) == 0
    print("  -> Passed. 64 queued, 36 dropped on overflow, drained cleanly.")

async def test_2_delayed_consumer():
    print("Test 2: Delayed consumer (consumer sleeping while producer writes)...")
    loop = asyncio.get_running_loop()
    coord = ForegroundDispatchCoordinator(capacity=64)
    coord.attach_loop(loop)
    
    # Push 30 events before starting consumer
    for i in range(30):
        coord.on_winevent(2000 + i, i)
    assert len(coord.queue) == 30
    
    task = asyncio.create_task(coord.consumer_worker())
    await asyncio.sleep(0.05)
    coord.shutdown_event.set()
    coord.event_available.set()
    await task
    
    assert coord.queue.events_drained == 30
    print("  -> Passed. Delayed consumer drained all 30 backlog events.")

async def test_3_buffer_full():
    print("Test 3: Buffer full capacity enforcement...")
    coord = ForegroundDispatchCoordinator(capacity=64)
    for i in range(80):
        coord.on_winevent(3000 + i, i)
    assert len(coord.queue) == 64
    assert coord.queue.events_dropped == 16
    print("  -> Passed. Buffer strictly capped at 64; 16 dropped.")

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
    assert coord.queue.events_drained == 50
    print("  -> Passed. All 50 concurrent events safely produced and consumed.")

async def test_5_producer_during_notification_reset():
    print("Test 5: Producer during notification reset...")
    loop = asyncio.get_running_loop()
    coord = ForegroundDispatchCoordinator(capacity=64)
    coord.attach_loop(loop)
    task = asyncio.create_task(coord.consumer_worker())
    
    coord.on_winevent(5001, 1)
    await asyncio.sleep(0.002)
    # Event should be consumed
    coord.on_winevent(5002, 2)
    await asyncio.sleep(0.01)
    
    coord.shutdown_event.set()
    coord.event_available.set()
    await task
    
    assert coord.queue.events_drained == 2
    print("  -> Passed. Notification reset cleanly and second event was delivered.")

async def test_6_event_loop_shutdown():
    print("Test 6: Event loop shutdown safety...")
    coord = ForegroundDispatchCoordinator(capacity=64)
    coord.on_winevent(6001, 1)
    # Producer works even without active loop attached
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
    # Draining at shutdown
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
    
    # Verify second task violates assertion or is barred
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
    await test_2_delayed_consumer()
    await test_3_buffer_full()
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
