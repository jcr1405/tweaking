"""Process Models and Explicit Lifecycles
Typed data structures for managed OS subprocesses under Windows Job Object supervision.
"""

import time
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List


class ProcessState(str, Enum):
    """Explicit lifecycle states for managed background and foreground processes."""
    QUEUED = "QUEUED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    INTERRUPTED = "INTERRUPTED"

    @property
    def is_terminal(self) -> bool:
        """Returns True if the process has reached a final, immutable state."""
        return self in (
            ProcessState.COMPLETED,
            ProcessState.FAILED,
            ProcessState.CANCELLED,
            ProcessState.TIMED_OUT,
            ProcessState.INTERRUPTED,
        )


@dataclass
class ManagedProcess:
    """Represents a supervised operating system subprocess."""
    task_id: str
    command: str
    args: List[str] = field(default_factory=list)
    cwd: str = ""
    pid: Optional[int] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    state: ProcessState = ProcessState.QUEUED
    exit_code: Optional[int] = None
    stdout_path: str = ""
    stderr_path: str = ""
    is_daemon: bool = False
    timeout_seconds: Optional[float] = None
    conversation_id: Optional[str] = None
    error_message: Optional[str] = None
    
    # Internal runtime references (excluded from serialized dictionaries)
    process_ref: Optional[Any] = field(default=None, repr=False)
    collector: Optional[Any] = field(default=None, repr=False)
    job_ref: Optional[Any] = field(default=None, repr=False)

    @property
    def duration_seconds(self) -> float:
        """Returns elapsed runtime in seconds."""
        if self.started_at is None:
            return 0.0
        end_time = self.completed_at if self.completed_at is not None else time.time()
        return round(max(0.0, end_time - self.started_at), 3)

    def to_dict(self) -> Dict[str, Any]:
        """Returns a sanitized JSON-serializable dictionary representation."""
        return {
            "task_id": self.task_id,
            "command": self.command,
            "args": self.args,
            "cwd": self.cwd,
            "pid": self.pid,
            "state": self.state.value,
            "exit_code": self.exit_code,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
            "is_daemon": self.is_daemon,
            "timeout_seconds": self.timeout_seconds,
            "conversation_id": self.conversation_id,
            "error_message": self.error_message,
        }
