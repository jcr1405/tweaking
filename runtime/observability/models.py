"""Observability Data Models and DTOs
Typed data transfer objects and enums for runtime monitoring, task hierarchy,
execution traces, and context inspection across the terminal control center.
"""

from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
import time


class TaskState(Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    INTERRUPTED = "INTERRUPTED"


class TaskType(Enum):
    RUNTIME = "RUNTIME"
    CONVERSATION = "CONVERSATION"
    TURN = "TURN"
    MODEL_INFERENCE = "MODEL_INFERENCE"
    TOOL_EXECUTION = "TOOL_EXECUTION"
    MCP_REQUEST = "MCP_REQUEST"
    SEARCH_REQUEST = "SEARCH_REQUEST"
    PAGE_FETCH = "PAGE_FETCH"
    SUBPROCESS = "SUBPROCESS"
    SUBAGENT = "SUBAGENT"
    OPERATIONAL_SUPERVISOR = "OPERATIONAL_SUPERVISOR"
    WORKFLOW = "WORKFLOW"
    WORKFLOW_PHASE = "WORKFLOW_PHASE"
    WORKFLOW_STEP = "WORKFLOW_STEP"


@dataclass
class TaskNode:
    """Represents a hierarchical trackable task in the Presence runtime."""
    task_id: str
    display_name: str
    task_type: TaskType
    parent_task_id: Optional[str] = None
    conversation_id: Optional[str] = None
    session_id: Optional[str] = None
    state: TaskState = TaskState.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    error_message: Optional[str] = None
    cancellable: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    artifact_id: Optional[str] = None
    children: List["TaskNode"] = field(default_factory=list)

    @property
    def elapsed_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.ended_at if self.ended_at is not None else time.time()
        return max(0.0, end - self.started_at)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "parent_task_id": self.parent_task_id,
            "display_name": self.display_name,
            "task_type": self.task_type.value,
            "conversation_id": self.conversation_id,
            "session_id": self.session_id,
            "state": self.state.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "elapsed_seconds": self.elapsed_seconds,
            "error_message": self.error_message,
            "cancellable": self.cancellable,
            "metadata": self.metadata,
            "artifact_id": self.artifact_id,
            "children_count": len(self.children)
        }


@dataclass
class NormalizedEvent:
    """Standardized event envelope for UI observation and event buffering."""
    event_id: str
    event_type: str
    timestamp_utc: float
    sequence_num: int
    component: str = "runtime"
    conversation_id: Optional[str] = None
    turn_idx: Optional[int] = None
    task_id: Optional[str] = None
    level: str = "INFO"  # INFO, WARNING, ERROR
    message: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp_utc": self.timestamp_utc,
            "sequence_num": self.sequence_num,
            "component": self.component,
            "conversation_id": self.conversation_id,
            "turn_idx": self.turn_idx,
            "task_id": self.task_id,
            "level": self.level,
            "message": self.message,
            "payload": self.payload,
        }


@dataclass
class ConversationSummaryDTO:
    """Metadata view of a conversation for the explorer and chat views."""
    conversation_id: str
    title: str
    step_count: int
    created_at: str
    last_modified_at: str
    status: str = "IDLE"
    workspace_path: str = ""
    preview: str = ""
    parent_conversation_id: str = ""
    nesting_depth: int = 0
    not_fully_idle: bool = False
    killed: bool = False


@dataclass
class TurnDetailDTO:
    """Detailed view of an individual conversation turn."""
    turn_idx: int
    user_step_idx: int
    user_input: str
    created_at: str
    agent_step_idx: Optional[int] = None
    agent_output: Optional[str] = None
    status: str = "DONE"
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    duration_ms: float = 0.0
    model_name: str = ""
    artifacts: List[str] = field(default_factory=list)


@dataclass
class TraceEventDTO:
    """Single chronological step in an execution trace."""
    event_idx: int
    offset_seconds: float
    event_name: str
    component: str
    status: str  # PENDING, RUNNING, COMPLETED, FAILED, CANCELLED
    details: Dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    error: Optional[str] = None
    artifact_id: Optional[str] = None


@dataclass
class ContextInspectionDTO:
    """Structured breakdown of reconstructed model prompt context."""
    conversation_id: str
    system_instructions: str
    conversation_history_steps: int
    messages_count: int
    summary_text: Optional[str]
    tool_declarations: List[str]
    retrieved_data: List[Dict[str, Any]]
    estimated_tokens: int
    is_compacted: bool
    workspace_path: str
    messages_preview: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class MCPToolViewDTO:
    """Metadata for an individual MCP tool in the catalog."""
    tool_name: str
    namespaced_name: str
    description: str
    authority_tier: str
    approved: bool
    registered: bool
    input_schema: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MCPServerViewDTO:
    """Comprehensive observability view of a configured MCP server."""
    server_id: str
    name: str
    configured: bool
    enabled: bool
    executable: str
    args: List[str]
    transport: str
    state: str  # STOPPED, STARTING, READY, FAILED, CRASHED
    process_id: Optional[int] = None
    process_alive: bool = False
    in_job_object: bool = False
    restarts: int = 0
    start_time: Optional[float] = None
    memory_mb: float = 0.0
    cpu_percent: float = 0.0
    tools: List[MCPToolViewDTO] = field(default_factory=list)
    approved_tool_count: int = 0
    discovered_tool_count: int = 0
    last_error: Optional[str] = None


@dataclass
class RuntimeSnapshot:
    """Point-in-time snapshot of the entire Presence runtime."""
    instance_id: str
    pid: int
    state: str
    uptime_seconds: float
    active_sessions_count: int
    active_tasks_count: int
    total_events_published: int
    memory_working_set_mb: float
    memory_private_mb: float
    memory_peak_mb: float
    cpu_percent: float
    thread_count: int
    mcp_servers: List[MCPServerViewDTO] = field(default_factory=list)
    search_telemetry: Dict[str, Any] = field(default_factory=dict)
    recent_events: List[NormalizedEvent] = field(default_factory=list)
    clean_recovery: bool = True
