"""Presence Deterministic Workflow Data Models
Defines immutable node definitions, DAG state transitions, failure policies, and execution runs.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Any, List


class ExecutionType(str, Enum):
    """Execution adapter type for an individual workflow node."""
    TOOL = "TOOL"              # Synchronous/asynchronous tool execution via ToolRegistry
    SUBAGENT = "SUBAGENT"      # Ephemeral child agent session via SubagentManager
    PROCESS = "PROCESS"        # Direct Windows Job Object process execution via ProcessManager
    AGGREGATE = "AGGREGATE"    # Deterministic artifact and output aggregation
    VERIFY = "VERIFY"          # Deterministic condition and assertion evaluator
    LOCAL_OPERATION = "LOCAL_OPERATION"      # Local operational reasoning/execution via LocalOperationsSupervisor
    DESKTOP_OPERATION = "DESKTOP_OPERATION"  # Desktop UI automation/interaction via DesktopController


class NodeState(str, Enum):
    """Lifecycle state of an individual workflow node."""
    PENDING = "PENDING"
    WAITING_DEPENDENCIES = "WAITING_DEPENDENCIES"
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"

    @property
    def is_terminal(self) -> bool:
        return self in (
            NodeState.COMPLETED,
            NodeState.FAILED,
            NodeState.CANCELLED,
            NodeState.SKIPPED,
        )


class WorkflowStatus(str, Enum):
    """Aggregate lifecycle state of the entire workflow run."""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"

    @property
    def is_terminal(self) -> bool:
        return self in (
            WorkflowStatus.COMPLETED,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
        )


class FailurePolicy(str, Enum):
    """Policy governing workflow execution when a node fails."""
    FAIL_FAST = "FAIL_FAST"                                  # Immediately terminate all pending/running nodes
    CONTINUE_INDEPENDENT = "CONTINUE_INDEPENDENT"            # Skip dependent nodes; continue unrelated branches
    CONTINUE_INDEPENDENT_BRANCHES = "CONTINUE_INDEPENDENT"   # Alias
    ALLOW_OPTIONAL = "ALLOW_OPTIONAL"                        # Allow nodes with optional=True to fail without aborting
    ALLOW_OPTIONAL_FAILURES = "ALLOW_OPTIONAL"               # Alias


class DependencyCondition(str, Enum):
    """Condition required from upstream dependencies for a node to become ready."""
    ALL_SUCCESS = "ALL_SUCCESS"  # All dependency nodes must be COMPLETED
    ANY_SUCCESS = "ANY_SUCCESS"  # At least one dependency node must be COMPLETED
    OPTIONAL = "OPTIONAL"        # Node executes regardless of upstream dependency status


@dataclass
class WorkflowNode:
    """Definition of an individual step within a Directed Acyclic Graph."""
    node_id: str
    name: str
    execution_type: ExecutionType
    payload: Dict[str, Any]
    depends_on: List[str] = field(default_factory=list)
    dependency_condition: DependencyCondition = DependencyCondition.ALL_SUCCESS
    phase: int = 1
    optional: bool = False
    timeout_seconds: Optional[float] = None
    max_retries: int = 0
    retry_backoff_seconds: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "execution_type": self.execution_type.value,
            "payload": self.payload,
            "depends_on": list(self.depends_on),
            "dependency_condition": self.dependency_condition.value,
            "phase": self.phase,
            "optional": self.optional,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "retry_backoff_seconds": self.retry_backoff_seconds,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkflowNode":
        return cls(
            node_id=str(data["node_id"]),
            name=str(data.get("name", data["node_id"])),
            execution_type=ExecutionType(data["execution_type"]),
            payload=dict(data.get("payload", {})),
            depends_on=list(data.get("depends_on", [])),
            dependency_condition=DependencyCondition(
                data.get("dependency_condition", DependencyCondition.ALL_SUCCESS.value)
            ),
            phase=int(data.get("phase", 1)),
            optional=bool(data.get("optional", False)),
            timeout_seconds=float(data["timeout_seconds"]) if data.get("timeout_seconds") is not None else None,
            max_retries=int(data.get("max_retries", 0)),
            retry_backoff_seconds=float(data.get("retry_backoff_seconds", 1.0)),
        )


@dataclass
class WorkflowDefinition:
    """Immutable blueprint of a complete deterministic workflow DAG."""
    workflow_id: str
    name: str
    nodes: List[WorkflowNode]
    description: str = ""
    failure_policy: FailurePolicy = FailurePolicy.FAIL_FAST
    max_parallel_nodes: int = 5
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "name": self.name,
            "description": self.description,
            "nodes": [n.to_dict() for n in self.nodes],
            "failure_policy": self.failure_policy.value,
            "max_parallel_nodes": self.max_parallel_nodes,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkflowDefinition":
        nodes = [WorkflowNode.from_dict(n) for n in data.get("nodes", [])]
        return cls(
            workflow_id=str(data["workflow_id"]),
            name=str(data.get("name", data["workflow_id"])),
            description=str(data.get("description", "")),
            nodes=nodes,
            failure_policy=FailurePolicy(data.get("failure_policy", FailurePolicy.FAIL_FAST.value)),
            max_parallel_nodes=int(data.get("max_parallel_nodes", 5)),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class WorkflowNodeExecution:
    """Runtime execution record of a single node within an active workflow run."""
    node_id: str
    workflow_run_id: str
    state: NodeState = NodeState.PENDING
    task_id: Optional[str] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    duration_seconds: float = 0.0
    retry_count: int = 0
    result_payload: Optional[Dict[str, Any]] = None
    artifact_id: Optional[str] = None
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "workflow_run_id": self.workflow_run_id,
            "state": self.state.value,
            "task_id": self.task_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_seconds": self.duration_seconds,
            "retry_count": self.retry_count,
            "result_payload": self.result_payload,
            "artifact_id": self.artifact_id,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkflowNodeExecution":
        return cls(
            node_id=str(data["node_id"]),
            workflow_run_id=str(data["workflow_run_id"]),
            state=NodeState(data.get("state", NodeState.PENDING.value)),
            task_id=data.get("task_id"),
            start_time=data.get("start_time"),
            end_time=data.get("end_time"),
            duration_seconds=float(data.get("duration_seconds", 0.0)),
            retry_count=int(data.get("retry_count", 0)),
            result_payload=data.get("result_payload"),
            artifact_id=data.get("artifact_id"),
            error_message=data.get("error_message"),
        )


@dataclass
class WorkflowRun:
    """Active execution instance of a WorkflowDefinition."""
    run_id: str
    workflow_id: str
    conversation_id: str
    status: WorkflowStatus = WorkflowStatus.PENDING
    root_task_id: Optional[str] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    duration_seconds: float = 0.0
    node_executions: Dict[str, WorkflowNodeExecution] = field(default_factory=dict)
    final_artifact_id: Optional[str] = None
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "conversation_id": self.conversation_id,
            "status": self.status.value,
            "root_task_id": self.root_task_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_seconds": self.duration_seconds,
            "node_executions": {k: v.to_dict() for k, v in self.node_executions.items()},
            "final_artifact_id": self.final_artifact_id,
            "error_message": self.error_message,
        }
