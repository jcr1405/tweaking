"""Local Operations Subsystem
Provides hierarchical execution capabilities for Presence, where the Local Operations
Supervisor (Qwen 3.5 9B via Ollama) manages operational tool-call loops under
delegated authority from the Cloud Principal Model.
"""

from runtime.local_operations.models import (
    ExecutionProfile,
    DelegationOutcome,
    DelegationConstraints,
    TaskDelegationContract,
    TaskDelegationResult,
    VerifiedEvidenceItem,
    OutcomeStatus,
    outcome_to_status,
    VerifiedFinding,
    ModelInterpretation,
    Contradiction,
    FailureRecord,
    SubagentReceipt,
    ArtifactReference,
    EscalationRequirement,
    CompactionMetrics,
    CloudReturnPacket,
)
from runtime.local_operations.supervisor import (
    LocalOperationsSupervisor,
    ScopedPermissionBoundary,
)

__all__ = [
    "ExecutionProfile",
    "DelegationOutcome",
    "DelegationConstraints",
    "TaskDelegationContract",
    "TaskDelegationResult",
    "VerifiedEvidenceItem",
    "OutcomeStatus",
    "outcome_to_status",
    "VerifiedFinding",
    "ModelInterpretation",
    "Contradiction",
    "FailureRecord",
    "SubagentReceipt",
    "ArtifactReference",
    "EscalationRequirement",
    "CompactionMetrics",
    "CloudReturnPacket",
    "LocalOperationsSupervisor",
    "ScopedPermissionBoundary",
]
