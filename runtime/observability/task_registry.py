"""Active Task Registry & Hierarchy Subsystem
Maintains an application-wide, thread-safe tree of active and recent tasks across
all conversations, agent turns, tool invocations, search queries, and MCP calls.
"""

import time
import uuid
import logging
import threading
from collections import deque
from typing import Optional, Dict, Any, List, Callable

from runtime.observability.models import TaskNode, TaskState, TaskType

logger = logging.getLogger("presence.observability.tasks")


class ActiveTaskRegistry:
    """Thread-safe, bounded hierarchical task tracking for the Presence runtime.
    Optionally backed by the central PersistenceManager for SQLite persistence and recovery.
    """

    def __init__(
        self,
        max_completed_history: int = 500,
        persistence_manager: Optional[Any] = None,
        event_bus: Optional[Any] = None,
    ):
        self._lock = threading.RLock()
        self._active_tasks: Dict[str, TaskNode] = {}
        self._completed_history: deque[TaskNode] = deque(maxlen=max_completed_history)
        self._cancellation_hooks: Dict[str, Callable[[], bool]] = {}
        self.persistence_manager = persistence_manager
        self.event_bus = event_bus

        # Perform startup recovery reconciliation if persistence is configured
        if self.persistence_manager and hasattr(self.persistence_manager, "reconcile_interrupted_tasks"):
            try:
                reconciled = self.persistence_manager.reconcile_interrupted_tasks()
                if reconciled > 0:
                    logger.info(f"Reconciled {reconciled} interrupted tasks from previous unclean run.")
            except Exception as e:
                logger.warning(f"Error reconciling tasks: {e}")

    def register_task(
        self,
        task_type: TaskType,
        display_name: str,
        parent_task_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        session_id: Optional[str] = None,
        cancellable: bool = False,
        cancellation_hook: Optional[Callable[[], bool]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None
    ) -> TaskNode:
        """Registers a new task into the active registry.
        Associates with parent task if specified.
        """
        tid = task_id or str(uuid.uuid4())
        node = TaskNode(
            task_id=tid,
            display_name=display_name,
            task_type=task_type,
            parent_task_id=parent_task_id,
            conversation_id=conversation_id,
            session_id=session_id,
            state=TaskState.QUEUED,
            created_at=time.time(),
            cancellable=cancellable,
            metadata=metadata or {}
        )

        with self._lock:
            self._active_tasks[tid] = node
            if cancellation_hook:
                self._cancellation_hooks[tid] = cancellation_hook

            if parent_task_id and parent_task_id in self._active_tasks:
                parent = self._active_tasks[parent_task_id]
                parent.children.append(node)

        if self.persistence_manager and hasattr(self.persistence_manager, "save_task"):
            try:
                self.persistence_manager.save_task({
                    "task_id": node.task_id,
                    "task_type": node.task_type.value,
                    "display_name": node.display_name,
                    "state": node.state.value,
                    "created_at": node.created_at,
                    "parent_task_id": node.parent_task_id,
                    "conversation_id": node.conversation_id,
                    "metadata": node.metadata
                })
            except Exception as e:
                logger.debug(f"Error persisting task {tid}: {e}")

        return node

    def start_task(self, task_id: str) -> Optional[TaskNode]:
        """Transitions a task from QUEUED to RUNNING."""
        with self._lock:
            task = self._active_tasks.get(task_id)
            if task:
                task.state = TaskState.RUNNING
                task.started_at = time.time()
                if self.persistence_manager and hasattr(self.persistence_manager, "update_task_state"):
                    try:
                        self.persistence_manager.update_task_state(task_id, state="RUNNING", started_at=task.started_at)
                    except Exception:
                        pass
                return task
        return None

    def complete_task(
        self,
        task_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        artifact_id: Optional[str] = None
    ) -> Optional[TaskNode]:
        """Marks a task as COMPLETED and archives to bounded history."""
        with self._lock:
            task = self._active_tasks.pop(task_id, None)
            self._cancellation_hooks.pop(task_id, None)
            if task:
                task.state = TaskState.COMPLETED
                task.ended_at = time.time()
                if metadata:
                    task.metadata.update(metadata)
                if artifact_id:
                    task.artifact_id = artifact_id
                self._completed_history.append(task)
                if self.persistence_manager and hasattr(self.persistence_manager, "update_task_state"):
                    try:
                        self.persistence_manager.update_task_state(
                            task_id,
                            state="COMPLETED",
                            completed_at=task.ended_at,
                            artifact_ref=artifact_id,
                            metadata=task.metadata
                        )
                    except Exception:
                        pass

                if self.event_bus and hasattr(self.event_bus, "publish"):
                    try:
                        self.event_bus.publish("TASK_COMPLETED", {
                            "task_id": task_id,
                            "display_name": task.display_name,
                            "conversation_id": task.conversation_id,
                            "artifact_id": artifact_id,
                            "ended_at": task.ended_at
                        })
                    except Exception:
                        pass

                return task
        return None

    def fail_task(
        self,
        task_id: str,
        error_message: str,
        metadata: Optional[Dict[str, Any]] = None,
        state: TaskState = TaskState.FAILED
    ) -> Optional[TaskNode]:
        """Marks a task as FAILED or TIMED_OUT and archives to bounded history."""
        with self._lock:
            task = self._active_tasks.pop(task_id, None)
            self._cancellation_hooks.pop(task_id, None)
            if task:
                task.state = state
                task.ended_at = time.time()
                task.error_message = error_message
                if metadata:
                    task.metadata.update(metadata)
                self._completed_history.append(task)
                if self.persistence_manager and hasattr(self.persistence_manager, "update_task_state"):
                    try:
                        self.persistence_manager.update_task_state(
                            task_id,
                            state=state.value,
                            completed_at=task.ended_at,
                            error_message=error_message,
                            metadata=task.metadata
                        )
                    except Exception:
                        pass

                if self.event_bus and hasattr(self.event_bus, "publish"):
                    try:
                        self.event_bus.publish(f"TASK_{state.value}", {
                            "task_id": task_id,
                            "display_name": task.display_name,
                            "conversation_id": task.conversation_id,
                            "error": error_message,
                            "ended_at": task.ended_at
                        })
                    except Exception:
                        pass

                return task
        return None

    def timeout_task(
        self,
        task_id: str,
        error_message: str = "Task timed out",
        metadata: Optional[Dict[str, Any]] = None
    ) -> Optional[TaskNode]:
        """Marks a task as TIMED_OUT."""
        return self.fail_task(task_id, error_message=error_message, metadata=metadata, state=TaskState.TIMED_OUT)

    def cancel_task(self, task_id: str, reason: Optional[str] = None) -> bool:
        """Attempts to cancel an active task via its registered cancellation hook.
        Safely signals cancellation without forcibly killing OS threads.
        """
        with self._lock:
            task = self._active_tasks.get(task_id)
            if not task:
                return False

            if not task.cancellable:
                logger.warning(f"Task '{task_id}' does not support cancellation.")
                return False

            hook = self._cancellation_hooks.get(task_id)
            cancelled = True
            if hook:
                try:
                    res = hook()
                    if res is False:
                        cancelled = False
                except Exception as e:
                    logger.error(f"Error calling cancellation hook for task '{task_id}': {e}")
                    cancelled = False

            if cancelled:
                task.state = TaskState.CANCELLED
                task.ended_at = time.time()
                task.error_message = reason or "Cancelled by user."
                self._active_tasks.pop(task_id, None)
                self._cancellation_hooks.pop(task_id, None)
                self._completed_history.append(task)

                if self.persistence_manager and hasattr(self.persistence_manager, "update_task_state"):
                    try:
                        self.persistence_manager.update_task_state(
                            task_id,
                            state="CANCELLED",
                            completed_at=task.ended_at,
                            error_message=task.error_message
                        )
                    except Exception:
                        pass

                if self.event_bus and hasattr(self.event_bus, "publish"):
                    try:
                        self.event_bus.publish("TASK_CANCELLED", {
                            "task_id": task_id,
                            "display_name": task.display_name,
                            "conversation_id": task.conversation_id,
                            "reason": task.error_message,
                            "ended_at": task.ended_at
                        })
                    except Exception:
                        pass

                # Also recursively cancel any running child tasks
                for child in list(task.children):
                    if child.task_id in self._active_tasks:
                        self.cancel_task(child.task_id, reason="Parent task cancelled.")

                return True
            return False

    def get_task(self, task_id: str) -> Optional[Any]:
        """Retrieves a task by ID from active, completed memory history, or persistent store."""
        with self._lock:
            if task_id in self._active_tasks:
                return self._active_tasks[task_id]
            for t in self._completed_history:
                if t.task_id == task_id:
                    return t

        if self.persistence_manager and hasattr(self.persistence_manager, "get_task"):
            try:
                row = self.persistence_manager.get_task(task_id)
                if row:
                    # Construct a TaskNode representation from SQLite row
                    return TaskNode(
                        task_id=row["task_id"],
                        display_name=row["display_name"],
                        task_type=TaskType(row["task_type"]) if row["task_type"] in TaskType.__members__ else TaskType.TOOL_EXECUTION,
                        parent_task_id=row["parent_task_id"],
                        conversation_id=row["conversation_id"],
                        state=TaskState(row["state"]) if row["state"] in TaskState.__members__ else TaskState.COMPLETED,
                        created_at=row["created_at"],
                        started_at=row["started_at"],
                        ended_at=row["completed_at"],
                        error_message=row["error_message"],
                        artifact_id=row["artifact_ref"],
                        metadata=row.get("metadata", {})
                    )
            except Exception as e:
                logger.debug(f"Error querying persistent task {task_id}: {e}")

        return None

    def get_active_tasks(self) -> List[TaskNode]:
        """Returns a snapshot list of currently active tasks."""
        with self._lock:
            return list(self._active_tasks.values())

    def get_active_roots(self) -> List[TaskNode]:
        """Returns root tasks (no parent) among active tasks."""
        with self._lock:
            return [t for t in self._active_tasks.values() if not t.parent_task_id or t.parent_task_id not in self._active_tasks]

    def get_completed_tasks(self, limit: int = 50) -> List[TaskNode]:
        """Returns the most recent completed/failed/cancelled tasks."""
        with self._lock:
            tasks = list(self._completed_history)
            tasks.reverse()
            return tasks[:limit]

    def clear(self) -> None:
        """Clears all active and history tasks."""
        with self._lock:
            self._active_tasks.clear()
            self._completed_history.clear()
            self._cancellation_hooks.clear()
