"""Bootstrap and Lifecycle Management Subsystem
Orchestrates component registration, startup sequencing, single-instance mutex,
unclean shutdown detection, and controlled reverse termination.
"""

import os
import sys
import time
import uuid
import logging
import threading
from enum import Enum, auto
from typing import Optional, Dict, Any, List

from .platform_win32 import Win32SingleInstanceMutex, get_process_memory_info, register_console_ctrl_handler
from .events import EventBus
from .permissions import PermissionBoundary
from .persistence import PersistenceManager
from .commands import CommandDispatcher
from .logging import setup_logger

logger = logging.getLogger("presence.lifecycle")


class RuntimeState(Enum):
    CREATED = auto()
    STARTING = auto()
    RUNNING = auto()
    STOPPING = auto()
    TERMINATED = auto()


class PresenceRuntime:
    """The central Presence Runtime coordinator."""

    def __init__(
        self,
        mutex_name: str = "Local\\PresenceRuntime_SingleInstanceMutex",
        db_path: Optional[str] = None,
        log_dir: Optional[str] = None,
        enable_context_awareness: bool = False,
    ):
        self.instance_id: str = str(uuid.uuid4())
        self.pid: int = os.getpid()
        self.start_time: float = 0.0
        self.state: RuntimeState = RuntimeState.CREATED
        self._shutdown_event = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None

        # 1. Platform Single-Instance Mutex
        self.mutex = Win32SingleInstanceMutex(mutex_name)

        # 2. Central Logging
        setup_logger(log_dir=db_path.parent if db_path else None)

        # 3. Core Subsystems
        self.events = EventBus()
        self.permissions = PermissionBoundary()
        self.persistence = PersistenceManager(db_path=db_path)
        self.commands = CommandDispatcher(permissions=self.permissions, runtime_ref=self)

        # 4. Context Awareness (Phase 2C)
        self.enable_context_awareness = enable_context_awareness
        self.context_manager: Optional[Any] = None
        self.foreground_source: Optional[Any] = None
        if self.enable_context_awareness:
            from .context.manager import ContextManager
            from .context.sources.foreground_window import Win32ForegroundWindowSource
            self.context_manager = ContextManager(event_bus=self.events)
            self.foreground_source = Win32ForegroundWindowSource(
                event_bus=self.events,
                on_snapshot_created=self.context_manager.set_current_snapshot,
            )
            self.context_manager.register_source(self.foreground_source)

        self._unclean_shutdowns_detected: List[Dict[str, Any]] = []

    def start(self) -> bool:
        """Initializes and starts the runtime. Returns False if another instance is already running."""
        if self.state != RuntimeState.CREATED:
            logger.warning(f"Cannot start runtime: Already in state {self.state.name}")
            return False

        self.state = RuntimeState.STARTING
        logger.info(f"Presence Runtime initializing [Instance: {self.instance_id}, PID: {self.pid}]")

        # 1. Enforce Win32 Single-Instance Mutex
        if not self.mutex.acquire():
            logger.error(f"Duplicate instance detected ({self.mutex.mutex_name}). Terminating this instance.")
            self.state = RuntimeState.TERMINATED
            self.persistence.close()
            return False

        self.start_time = time.time()

        # 2. Register Win32 Console / Signal Handler
        def _on_console_ctrl(ctrl_type: int) -> bool:
            logger.info(f"Received OS control signal ({ctrl_type}). Ordering runtime shutdown...")
            self.stop()
            return True

        register_console_ctrl_handler(_on_console_ctrl)

        # 3. Detect and Log Unclean Shutdowns from previous runs
        self._unclean_shutdowns_detected = self.persistence.detect_unclean_shutdowns(self.instance_id)
        if self._unclean_shutdowns_detected:
            for record in self._unclean_shutdowns_detected:
                logger.warning(
                    f"UNCLEAN SHUTDOWN DETECTED: Previous instance {record['instance_id']} (PID {record['pid']}) "
                    f"died unexpectedly at {time.ctime(record['last_heartbeat_utc'])}"
                )

        # 4. Record Initial Start State
        self.persistence.record_start(self.instance_id, self.pid)

        # 5. Start Heartbeat Background Thread
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="PresenceHeartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

        # 6. Publish Lifecycle Event
        self.state = RuntimeState.RUNNING
        self.events.publish("RuntimeStarted", {
            "instance_id": self.instance_id,
            "pid": self.pid,
            "start_time": self.start_time,
            "unclean_shutdowns_count": len(self._unclean_shutdowns_detected),
        })

        logger.info("Presence Runtime successfully started in RUNNING state.")
        return True

    def stop(self) -> None:
        """Gracefully terminates all subsystems and flushes state."""
        if self.state in (RuntimeState.STOPPING, RuntimeState.TERMINATED):
            return

        self.state = RuntimeState.STOPPING
        logger.info("Initiating graceful Presence Runtime shutdown...")

        # 1. Signal background loops
        self._shutdown_event.set()

        # Stop foreground context source if running
        if self.foreground_source is not None:
            try:
                from .context.contracts import ContextSourceState
                if self.foreground_source.state == ContextSourceState.RUNNING:
                    self.foreground_source.stop_sync(timeout=2.0)
            except Exception as e:
                logger.error(f"Error stopping foreground source during runtime shutdown: {e}")

        # 2. Publish Stopping Event
        try:
            self.events.publish("RuntimeStopped", {
                "instance_id": self.instance_id,
                "uptime_seconds": time.time() - self.start_time if self.start_time else 0,
            })
        except Exception as e:
            logger.error(f"Error publishing RuntimeStopped event: {e}")

        # 3. Record Clean Shutdown in Persistence
        try:
            self.persistence.record_clean_shutdown(self.instance_id)
            self.persistence.close()
        except Exception as e:
            logger.error(f"Error recording clean shutdown in persistence: {e}")

        # 4. Release Mutex
        self.mutex.release()

        self.state = RuntimeState.TERMINATED
        logger.info("Presence Runtime successfully terminated.")

    def _heartbeat_loop(self) -> None:
        """Periodic background thread recording health and heartbeat to SQLite."""
        while not self._shutdown_event.wait(timeout=5.0):
            if self.state == RuntimeState.RUNNING:
                try:
                    self.persistence.record_heartbeat(self.instance_id)
                except Exception as e:
                    logger.error(f"Failed to record runtime heartbeat: {e}")

    def get_health_status(self) -> Dict[str, Any]:
        """Returns structured health and performance metrics."""
        uptime = time.time() - self.start_time if self.start_time else 0.0
        mem_info = get_process_memory_info(self.pid)
        status = {
            "instance_id": self.instance_id,
            "state": self.state.name,
            "pid": self.pid,
            "uptime_seconds": uptime,
            "events_published": self.events.total_events_published,
            "clean_recovery": len(self._unclean_shutdowns_detected) == 0,
            "unclean_shutdowns_detected": len(self._unclean_shutdowns_detected),
            "memory": {
                "working_set_bytes": mem_info.working_set_bytes,
                "working_set_mb": mem_info.working_set_mb,
                "private_bytes": mem_info.private_bytes,
                "private_mb": mem_info.private_mb,
                "peak_working_set_bytes": mem_info.peak_working_set_bytes,
            },
        }
        if self.context_manager is not None:
            snap = self.context_manager.get_current_snapshot()
            status["context_awareness"] = {
                "enabled": True,
                "has_snapshot": snap is not None,
                "snapshot_id": snap.snapshot_id if snap else None,
            }
        return status

    def wait_for_shutdown(self, timeout: Optional[float] = None) -> bool:
        """Blocks until the runtime shutdown event is triggered."""
        return self._shutdown_event.wait(timeout=timeout)

    @property
    def is_running(self) -> bool:
        return self.state == RuntimeState.RUNNING
