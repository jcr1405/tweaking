"""Strongly-typed data models for Ephemeral Subagents
Defines roles, configurations, runtime lifecycle states, and execution receipts.
"""

import time
import threading
from enum import Enum
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any, List


class SubagentRole(str, Enum):
    """Standardized roles for ephemeral subagents with tailored capabilities."""
    RESEARCH = "research"
    CODE_ANALYSIS = "code_analysis"
    VERIFIER = "verifier"
    GENERAL = "general"


class SubagentStatus(str, Enum):
    """Explicit lifecycle states for subagent execution."""
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"

    @property
    def is_terminal(self) -> bool:
        return self in (
            SubagentStatus.COMPLETED,
            SubagentStatus.FAILED,
            SubagentStatus.CANCELLED,
            SubagentStatus.TIMED_OUT,
        )


@dataclass
class SubagentConfig:
    """Execution configuration for spawning an ephemeral subagent."""
    role: str = SubagentRole.GENERAL.value
    prompt: str = ""
    model_id: Optional[str] = None
    tools_whitelist: Optional[List[str]] = None
    workspace_mode: str = "inherit"  # 'inherit' | 'branch' | 'share'
    workspace_path: Optional[str] = None
    timeout_seconds: float = 300.0
    max_iterations: Optional[int] = 15
    allow_subagents: bool = False
    system_prompt: Optional[str] = None
    request_config: Optional[Any] = None
    pin_local_ollama: bool = False
    max_parent_subagents: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SubagentExecutionResult:
    """Receipt returned to the parent agent summarizing subagent execution."""
    subagent_id: str
    conversation_id: str
    parent_conversation_id: str
    role: str
    status: str
    summary: str
    step_count: int
    duration_seconds: float
    error: Optional[str] = None
    task_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ManagedSubagent:
    """Runtime tracking model for an active or completed subagent."""
    subagent_id: str
    conversation_id: str
    parent_conversation_id: str
    role: str
    prompt: str
    status: SubagentStatus = SubagentStatus.STARTING
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    task_id: Optional[str] = None
    summary: str = ""
    error_message: Optional[str] = None
    step_count: int = 0
    duration_seconds: float = 0.0
    session_ref: Optional[Any] = None
    cancellation_token: Optional[Any] = None
    thread_ref: Optional[Any] = None
    _request_config: Optional[Any] = None
    _turn_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "subagent_id": self.subagent_id,
            "conversation_id": self.conversation_id,
            "parent_conversation_id": self.parent_conversation_id,
            "role": self.role,
            "prompt": self.prompt[:200],
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "task_id": self.task_id,
            "summary": self.summary,
            "error": self.error_message,
            "step_count": self.step_count,
            "duration_seconds": self.duration_seconds,
        }
