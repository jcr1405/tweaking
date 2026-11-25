"""Task Decomposition & Execution Ledger (TDEL) Models
Defines typed data structures for complexity classification, task plan specifications,
and plan node configurations.
"""

import time
import uuid
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional

from runtime.workflows.models import ExecutionType, DependencyCondition, FailurePolicy


class ComplexityCategory(str, Enum):
    """Classification of user request operational complexity."""
    SIMPLE = "SIMPLE"                  # Conversational Q&A, single lookup, file read
    MULTI_STEP = "MULTI_STEP"          # Multiple tool calls, comparisons, batch lookups
    COMPLEX = "COMPLEX"                # Multi-stage diagnostic, audit, installation, verification
    LONG_RUNNING = "LONG_RUNNING"      # Background build, large file transformation, test suite
    RESEARCH_HEAVY = "RESEARCH_HEAVY"  # Deep multi-repository exploration, subagent delegation


class PlanStatus(str, Enum):
    """Lifecycle status of a TaskPlan."""
    DRAFT = "DRAFT"
    PENDING = "PENDING"
    PLANNING = "PLANNING"
    PLAN_READY = "PLAN_READY"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    AWAITING_CLARIFICATION = "AWAITING_CLARIFICATION"
    APPROVED = "APPROVED"
    IN_PROGRESS = "IN_PROGRESS"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"
    PLAN_UNAVAILABLE = "PLAN_UNAVAILABLE"
    VALIDATION_FAILED = "VALIDATION_FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in (
            PlanStatus.COMPLETED,
            PlanStatus.FAILED,
            PlanStatus.CANCELLED,
            PlanStatus.INTERRUPTED,
            PlanStatus.PLAN_UNAVAILABLE,
            PlanStatus.VALIDATION_FAILED,
        )


@dataclass
class TaskPlanNode:
    """A single verifiable micro-task within a TaskPlan."""
    node_id: str
    title: str
    description: str
    execution_type: ExecutionType
    payload: Dict[str, Any] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)
    phase: int = 1
    completion_criteria: Dict[str, Any] = field(default_factory=dict)
    optional: bool = False
    timeout_seconds: Optional[float] = None
    required_permission: Optional[str] = None
    estimated_duration_seconds: float = 5.0
    name: Optional[str] = None

    def __post_init__(self):
        if not self.title and self.name:
            self.title = self.name
        elif not self.name and self.title:
            self.name = self.title

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "title": self.title,
            "description": self.description,
            "execution_type": self.execution_type.value,
            "payload": dict(self.payload),
            "depends_on": list(self.depends_on),
            "phase": self.phase,
            "completion_criteria": dict(self.completion_criteria),
            "optional": self.optional,
            "timeout_seconds": self.timeout_seconds,
            "required_permission": self.required_permission,
            "estimated_duration_seconds": self.estimated_duration_seconds,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskPlanNode":
        exec_type = data.get("execution_type", ExecutionType.TOOL.value)
        if isinstance(exec_type, str):
            exec_type = ExecutionType(exec_type)

        return cls(
            node_id=data["node_id"],
            title=data.get("title", data["node_id"]),
            description=data.get("description", ""),
            execution_type=exec_type,
            payload=dict(data.get("payload", {})),
            depends_on=list(data.get("depends_on", [])),
            phase=int(data.get("phase", 1)),
            completion_criteria=dict(data.get("completion_criteria", {})),
            optional=bool(data.get("optional", False)),
            timeout_seconds=float(data["timeout_seconds"]) if data.get("timeout_seconds") is not None else None,
            required_permission=data.get("required_permission"),
            estimated_duration_seconds=float(data.get("estimated_duration_seconds", 5.0)),
        )


@dataclass
class TaskPlan:
    """Structured, verifiable execution blueprint decomposed from a user prompt."""
    plan_id: str
    title: str
    user_goal: str
    description: str
    complexity: ComplexityCategory
    originating_conversation_id: str
    nodes: List[TaskPlanNode] = field(default_factory=list)
    plan_version: int = 1
    status: PlanStatus = PlanStatus.DRAFT
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    workflow_id: Optional[str] = None
    failure_policy: FailurePolicy = FailurePolicy.FAIL_FAST
    max_parallel_nodes: int = 5
    assumptions: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    expected_deliverables: List[str] = field(default_factory=list)
    approval_requirements: List[str] = field(default_factory=list)
    ledger_artifact_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "title": self.title,
            "user_goal": self.user_goal,
            "description": self.description,
            "complexity": self.complexity.value,
            "originating_conversation_id": self.originating_conversation_id,
            "nodes": [n.to_dict() for n in self.nodes],
            "plan_version": self.plan_version,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "workflow_id": self.workflow_id,
            "failure_policy": self.failure_policy.value,
            "max_parallel_nodes": self.max_parallel_nodes,
            "assumptions": list(self.assumptions),
            "constraints": list(self.constraints),
            "expected_deliverables": list(self.expected_deliverables),
            "approval_requirements": list(self.approval_requirements),
            "ledger_artifact_id": self.ledger_artifact_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskPlan":
        complexity = data.get("complexity", ComplexityCategory.COMPLEX.value)
        if isinstance(complexity, str):
            complexity = ComplexityCategory(complexity)

        status = data.get("status", PlanStatus.DRAFT.value)
        if isinstance(status, str):
            status = PlanStatus(status)

        failure_policy = data.get("failure_policy", FailurePolicy.FAIL_FAST.value)
        if isinstance(failure_policy, str):
            failure_policy = FailurePolicy(failure_policy)

        nodes = [TaskPlanNode.from_dict(n) for n in data.get("nodes", [])]

        return cls(
            plan_id=data["plan_id"],
            title=data.get("title", "Untitled Task Plan"),
            user_goal=data.get("user_goal", ""),
            description=data.get("description", ""),
            complexity=complexity,
            originating_conversation_id=data.get("originating_conversation_id", ""),
            nodes=nodes,
            plan_version=int(data.get("plan_version", 1)),
            status=status,
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
            completed_at=float(data["completed_at"]) if data.get("completed_at") is not None else None,
            workflow_id=data.get("workflow_id"),
            failure_policy=failure_policy,
            max_parallel_nodes=int(data.get("max_parallel_nodes", 5)),
            assumptions=list(data.get("assumptions", [])),
            constraints=list(data.get("constraints", [])),
            expected_deliverables=list(data.get("expected_deliverables", [])),
            approval_requirements=list(data.get("approval_requirements", [])),
            ledger_artifact_id=data.get("ledger_artifact_id"),
            metadata=dict(data.get("metadata", {})),
        )
