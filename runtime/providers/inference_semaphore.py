"""Shared Local Inference Semaphore.
Provides a thread-safe, bounded, cancellation-aware FIFO gate for local LLM inference
(e.g., Ollama Qwen 3.5 9B).

Guarantees:
1. Bounded Concurrency: Limits active model generation streams across all sessions,
   subagents, and callers (default max_concurrent=1 for local GPU).
2. Strict FIFO Queue: Callers are served in strict arrival order.
3. Zero Deadlock: Slots are held ONLY during active token generation streaming,
   never during tool execution, subprocess execution, or subagent waiting.
4. Cancellation Awareness: Waiting callers can be cancelled via CancellationToken.
5. Timeout Detection: Waiting callers time out gracefully if slot is not granted.
6. Re-entrancy Protection: Recursive calls on the same thread do not deadlock.
7. Telemetry & Observability: Comprehensive stats and EventBus event notifications.
"""

import time
import uuid
import logging
import threading
from collections import deque
from contextlib import contextmanager
from typing import Optional, Dict, Any, Deque, Tuple

logger = logging.getLogger("presence.providers.inference_semaphore")


class InferenceSemaphoreError(Exception):
    """Base exception for inference semaphore failures."""
    pass


class InferenceTimeoutError(InferenceSemaphoreError):
    """Raised when waiting to acquire an inference slot exceeds timeout."""
    pass


class InferenceCancelledError(InferenceSemaphoreError):
    """Raised when an inference slot acquisition is cancelled."""
    pass


class InferenceQueueFullError(InferenceSemaphoreError):
    """Raised when the inference wait queue has reached capacity."""
    pass


class ReentrantInferenceError(InferenceSemaphoreError):
    """Raised when unauthorized recursive inference acquisition is detected."""
    pass


class _WaitQueueEntry:
    """Internal entry representing a waiting caller in the FIFO queue."""
    def __init__(
        self,
        entry_id: str,
        caller_id: str,
        event: threading.Event,
        enqueue_time: float,
        cancellation_token: Optional[Any] = None,
    ):
        self.entry_id = entry_id
        self.caller_id = caller_id
        self.event = event
        self.enqueue_time = enqueue_time
        self.cancellation_token = cancellation_token
        self.granted = False


class LocalInferenceSemaphore:
    """Thread-safe FIFO concurrency gate for local model inference."""

    def __init__(
        self,
        provider: str = "ollama",
        model_id: str = "qwen3.5:9b",
        max_concurrent: int = 1,
        max_queue_size: int = 32,
        allow_reentrant: bool = True,
        event_bus: Optional[Any] = None,
    ):
        if max_concurrent < 1:
            raise ValueError(f"max_concurrent must be >= 1, got {max_concurrent}")
        if max_queue_size < 1:
            raise ValueError(f"max_queue_size must be >= 1, got {max_queue_size}")

        self.provider = provider
        self.model_id = model_id
        self._max_concurrent = max_concurrent
        self._max_queue_size = max_queue_size
        self._allow_reentrant = allow_reentrant
        self._event_bus = event_bus

        self._lock = threading.Lock()
        self._active_count = 0
        self._wait_queue: Deque[_WaitQueueEntry] = deque()
        self._holders: Dict[int, int] = {}  # thread_ident -> recursion depth

        # Telemetry metrics
        self._total_acquired = 0
        self._total_timeouts = 0
        self._total_cancelled = 0
        self._total_reentrant = 0
        self._total_wait_seconds = 0.0

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def max_queue_size(self) -> int:
        return self._max_queue_size

    @property
    def active_generations(self) -> int:
        with self._lock:
            return self._active_count

    @property
    def queue_depth(self) -> int:
        with self._lock:
            return len(self._wait_queue)

    @property
    def total_acquired(self) -> int:
        with self._lock:
            return self._total_acquired

    @property
    def total_timeouts(self) -> int:
        with self._lock:
            return self._total_timeouts

    @property
    def total_cancelled(self) -> int:
        with self._lock:
            return self._total_cancelled

    @property
    def total_reentrant(self) -> int:
        with self._lock:
            return self._total_reentrant

    @property
    def average_wait_seconds(self) -> float:
        with self._lock:
            if self._total_acquired == 0:
                return 0.0
            return self._total_wait_seconds / self._total_acquired

    def get_telemetry(self) -> Dict[str, Any]:
        """Returns snapshot of current semaphore state and historical metrics."""
        with self._lock:
            avg_wait = (
                self._total_wait_seconds / self._total_acquired
                if self._total_acquired > 0
                else 0.0
            )
            return {
                "provider": self.provider,
                "model_id": self.model_id,
                "max_concurrent": self._max_concurrent,
                "max_queue_size": self._max_queue_size,
                "active_generations": self._active_count,
                "queue_depth": len(self._wait_queue),
                "total_acquired": self._total_acquired,
                "total_timeouts": self._total_timeouts,
                "total_cancelled": self._total_cancelled,
                "total_reentrant": self._total_reentrant,
                "total_wait_seconds": round(self._total_wait_seconds, 4),
                "average_wait_seconds": round(avg_wait, 4),
            }

    def _emit_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Best-effort notification through EventBus if registered."""
        if not self._event_bus:
            return
        try:
            full_payload = {
                "provider": self.provider,
                "model_id": self.model_id,
                **payload,
            }
            if hasattr(self._event_bus, "emit"):
                self._event_bus.emit(event_type, full_payload)
            elif hasattr(self._event_bus, "publish"):
                self._event_bus.publish(event_type, full_payload)
        except Exception as e:
            logger.debug(f"Failed to emit semaphore event {event_type}: {e}")

    def acquire(
        self,
        caller_id: str = "",
        timeout: Optional[float] = 60.0,
        cancellation_token: Optional[Any] = None,
    ) -> float:
        """Acquires a local inference slot adhering to strict FIFO order.
        
        Returns:
            Wait time in seconds.
            
        Raises:
            InferenceCancelledError: If cancellation_token is cancelled before or during wait.
            InferenceTimeoutError: If timeout expires before slot becomes available.
            InferenceQueueFullError: If maximum wait queue capacity is exceeded.
            ReentrantInferenceError: If re-entrancy is disabled and caller attempts nested acquisition.
        """
        tid = threading.get_ident()
        caller_label = caller_id or f"thread_{tid}"

        # 1. Early cancellation check
        if cancellation_token and getattr(cancellation_token, "is_cancelled", False):
            with self._lock:
                self._total_cancelled += 1
            self._emit_event("LOCAL_INFERENCE_CANCELLED", {"caller_id": caller_label, "wait_time_seconds": 0.0})
            raise InferenceCancelledError(f"Inference acquisition cancelled for caller '{caller_label}'.")

        enqueue_time = time.time()

        with self._lock:
            # 2. Re-entrancy check
            if tid in self._holders:
                if not self._allow_reentrant:
                    raise ReentrantInferenceError(
                        f"Reentrant acquisition not permitted for caller '{caller_label}' on thread {tid}."
                    )
                self._holders[tid] += 1
                self._total_reentrant += 1
                logger.debug(
                    f"Reentrant inference acquisition granted for caller '{caller_label}' (depth {self._holders[tid]})."
                )
                return 0.0

            # 3. Direct acquisition if slot is immediately free and queue is empty
            if self._active_count < self._max_concurrent and len(self._wait_queue) == 0:
                self._active_count += 1
                self._holders[tid] = 1
                self._total_acquired += 1
                self._emit_event(
                    "LOCAL_INFERENCE_ACQUIRED",
                    {"caller_id": caller_label, "wait_time_seconds": 0.0, "active_generations": self._active_count},
                )
                return 0.0

            # 4. Check queue capacity
            if len(self._wait_queue) >= self._max_queue_size:
                raise InferenceQueueFullError(
                    f"Local inference queue full ({len(self._wait_queue)} >= {self._max_queue_size}) "
                    f"for caller '{caller_label}'."
                )

            # 5. Enqueue caller in FIFO queue
            entry = _WaitQueueEntry(
                entry_id=str(uuid.uuid4())[:8],
                caller_id=caller_label,
                event=threading.Event(),
                enqueue_time=enqueue_time,
                cancellation_token=cancellation_token,
            )
            self._wait_queue.append(entry)
            queue_len = len(self._wait_queue)

        self._emit_event(
            "LOCAL_INFERENCE_QUEUED",
            {"caller_id": caller_label, "queue_depth": queue_len},
        )
        logger.info(
            f"Caller '{caller_label}' queued for local inference (position {queue_len}, "
            f"active: {self.active_generations}/{self._max_concurrent})."
        )

        # 6. Wait loop with cancellation and timeout polling
        deadline = (enqueue_time + timeout) if timeout is not None else None

        while True:
            # Check cancellation token
            if cancellation_token and getattr(cancellation_token, "is_cancelled", False):
                self._handle_abandonment(entry, reason="cancelled")
                wait_time = time.time() - enqueue_time
                with self._lock:
                    self._total_cancelled += 1
                self._emit_event(
                    "LOCAL_INFERENCE_CANCELLED",
                    {"caller_id": caller_label, "wait_time_seconds": round(wait_time, 4)},
                )
                raise InferenceCancelledError(
                    f"Inference acquisition cancelled for caller '{caller_label}' after {wait_time:.2f}s."
                )

            # Check timeout
            now = time.time()
            if deadline is not None and now >= deadline:
                self._handle_abandonment(entry, reason="timeout")
                with self._lock:
                    self._total_timeouts += 1
                self._emit_event(
                    "LOCAL_INFERENCE_TIMEOUT",
                    {"caller_id": caller_label, "timeout_seconds": timeout},
                )
                raise InferenceTimeoutError(
                    f"Inference acquisition timed out after {timeout:.2f}s for caller '{caller_label}'."
                )

            # Wait short interval
            remaining = (deadline - now) if deadline is not None else 0.2
            wait_slice = min(0.1, max(0.01, remaining))
            if entry.event.wait(timeout=wait_slice):
                # Granted!
                break

        # 7. Slot successfully granted
        wait_time = time.time() - enqueue_time
        with self._lock:
            self._holders[tid] = 1
            self._total_acquired += 1
            self._total_wait_seconds += wait_time
            active = self._active_count

        self._emit_event(
            "LOCAL_INFERENCE_ACQUIRED",
            {
                "caller_id": caller_label,
                "wait_time_seconds": round(wait_time, 4),
                "active_generations": active,
            },
        )
        logger.debug(
            f"Local inference slot acquired for '{caller_label}' after {wait_time:.3f}s."
        )
        return wait_time

    def _handle_abandonment(self, entry: _WaitQueueEntry, reason: str) -> None:
        """Handles waiter abandonment due to timeout or cancellation."""
        with self._lock:
            if entry.granted:
                # Slot was already granted right before abandonment, pass to next waiter
                self._active_count = max(0, self._active_count - 1)
                self._notify_next_waiter_locked()
            else:
                # Remove from queue if still present
                try:
                    self._wait_queue.remove(entry)
                except ValueError:
                    pass

    def release(self, caller_id: str = "", duration: Optional[float] = None) -> None:
        """Releases an inference slot and immediately grants to the next FIFO waiter."""
        tid = threading.get_ident()
        caller_label = caller_id or f"thread_{tid}"

        with self._lock:
            if tid in self._holders:
                self._holders[tid] -= 1
                if self._holders[tid] > 0:
                    # Still held by outer reentrant caller
                    logger.debug(
                        f"Reentrant release for caller '{caller_label}' (remaining depth {self._holders[tid]})."
                    )
                    return
                del self._holders[tid]

            self._active_count = max(0, self._active_count - 1)
            self._notify_next_waiter_locked()
            active = self._active_count

        self._emit_event(
            "LOCAL_INFERENCE_RELEASED",
            {
                "caller_id": caller_label,
                "active_generations": active,
                "duration_seconds": round(duration, 4) if duration is not None else None,
            },
        )
        logger.debug(f"Local inference slot released by '{caller_label}'. Active: {active}.")

    def _notify_next_waiter_locked(self) -> None:
        """Internal helper to wake the next eligible FIFO waiter. Must be called under self._lock."""
        while self._wait_queue and self._active_count < self._max_concurrent:
            next_entry = self._wait_queue.popleft()
            # If the waiter was already cancelled before being popped, skip it
            if next_entry.cancellation_token and getattr(next_entry.cancellation_token, "is_cancelled", False):
                self._total_cancelled += 1
                continue

            self._active_count += 1
            next_entry.granted = True
            next_entry.event.set()
            break

    @contextmanager
    def acquire_slot(
        self,
        caller_id: str = "",
        timeout: Optional[float] = 60.0,
        cancellation_token: Optional[Any] = None,
    ):
        """Context manager for acquiring and releasing an inference slot.
        
        Usage:
            with semaphore.acquire_slot(caller_id="subagent_1", timeout=30.0):
                for chunk in model.generate_stream(...):
                    yield chunk
        """
        self.acquire(caller_id=caller_id, timeout=timeout, cancellation_token=cancellation_token)
        start_time = time.time()
        try:
            yield
        finally:
            self.release(caller_id=caller_id, duration=time.time() - start_time)


# =============================================================================
# Global Registry for Shared Provider Semaphores
# =============================================================================

_SEMAPHORES: Dict[str, LocalInferenceSemaphore] = {}
_REGISTRY_LOCK = threading.Lock()


def get_shared_inference_semaphore(
    provider: str = "ollama",
    model_id: str = "qwen3.5:9b",
    max_concurrent: int = 1,
    max_queue_size: int = 32,
    event_bus: Optional[Any] = None,
) -> LocalInferenceSemaphore:
    """Retrieves or initializes the shared singleton inference semaphore for a provider/model."""
    key = f"{provider.strip().lower()}:{model_id.strip().lower()}"
    with _REGISTRY_LOCK:
        if key not in _SEMAPHORES:
            _SEMAPHORES[key] = LocalInferenceSemaphore(
                provider=provider,
                model_id=model_id,
                max_concurrent=max_concurrent,
                max_queue_size=max_queue_size,
                event_bus=event_bus,
            )
        return _SEMAPHORES[key]


def reset_shared_inference_semaphores() -> None:
    """Resets all registered semaphores (primarily for isolated test teardown)."""
    with _REGISTRY_LOCK:
        _SEMAPHORES.clear()
