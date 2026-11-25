"""Data Models and Contracts for the Local Operations Supervisor.
Defines typed, validated delegation contracts, constraints, outcomes,
evidence structures, and execution results.
"""

from enum import Enum
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field, model_validator


class ExecutionProfile(str, Enum):
    """Execution profiles supported by the Local Operations Supervisor."""
    LOCAL_SUPERVISOR_ROUTINE = "local_supervisor_routine"  # Qwen 3.5 9B (Mode A: think=False)
    LOCAL_SUPERVISOR_DEEP = "local_supervisor_deep"        # Qwen 3.5 9B (Mode B: think=True)


class DelegationOutcome(str, Enum):
    """Deterministic outcome states for a delegated task."""
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    ESCALATION_REQUIRED = "ESCALATION_REQUIRED"
    CANCELLED = "CANCELLED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


class DelegationConstraints(BaseModel):
    """Operational boundaries and execution ceilings enforced on the local supervisor."""
    read_only: bool = Field(True, description="Strictly forbid any L1/L2/L3 state modifications.")
    source_modification_allowed: bool = Field(False, description="Whether code file edits are permitted.")
    require_verified_evidence: bool = Field(True, description="Claims must be backed by actual execution evidence.")
    max_tool_cycles: int = Field(15, ge=1, le=50, description="Max local iterations before forced termination.")
    timeout_seconds: float = Field(180.0, ge=0.01, le=600.0, description="Overall wall-clock timeout in seconds.")
    subagents_allowed: bool = Field(False, description="Whether the supervisor is allowed to spawn operational subagents.")
    max_subagents: int = Field(0, ge=0, le=4, description="Maximum concurrent subagents.")
    context_budget_tokens: int = Field(2000, ge=500, le=8000, description="Target token budget for compact return packet.")
    max_tokens_budget: int = Field(8192, ge=1, le=100000, description="Maximum total token budget for local execution.")

    @model_validator(mode="after")
    def validate_constraint_coherence(self) -> "DelegationConstraints":
        """Ensures conflicting permission and modification constraints cannot coexist."""
        if self.read_only and self.source_modification_allowed:
            raise ValueError(
                "Conflicting constraints: read_only=True and source_modification_allowed=True cannot coexist."
            )
        if not self.subagents_allowed and self.max_subagents > 0:
            raise ValueError(
                f"max_subagents must be 0 when subagents_allowed=False, got {self.max_subagents}."
            )
        if self.subagents_allowed and self.max_subagents == 0:
            # Default to 2 when subagents_allowed is True
            object.__setattr__(self, "max_subagents", 2)
        elif self.subagents_allowed and self.max_subagents < 1:
            raise ValueError(
                f"max_subagents must be >= 1 when subagents_allowed=True, got {self.max_subagents}."
            )
        return self


VALID_CAPABILITY_IDENTIFIERS = {
    "mcp.filesystem",
    "mcp.code_search",
    "mcp.system_diagnostics",
    "process_execution",
    "process_inspection",
    "system_diagnostics",
    "filesystem",
    "code_search",
    "subagents",
    "desktop",
    "all_read_only",
    "mcp.*",
}


class TaskDelegationContract(BaseModel):
    """Typed, validated contract transferring operational work from the Cloud Principal
    to the Local Operations Supervisor.
    """
    task_id: str = Field(..., min_length=1, max_length=128, description="Unique deterministic task identifier.")
    parent_conversation_id: str = Field(..., min_length=1, max_length=128, description="Main cloud conversation ID.")
    objective: str = Field(..., min_length=5, max_length=2000, description="Clear operational objective to achieve.")
    context_summary: str = Field("", description="Concise background context needed for the task.")
    execution_profile: ExecutionProfile = Field(
        default=ExecutionProfile.LOCAL_SUPERVISOR_ROUTINE,
        description="Local model thinking profile."
    )
    constraints: DelegationConstraints = Field(default_factory=DelegationConstraints)
    allowed_capabilities: List[str] = Field(
        ...,
        min_length=1,
        description="Allowed capability categories (e.g. ['mcp.filesystem', 'mcp.code_search'])."
    )
    expected_deliverable: Dict[str, Any] = Field(
        default_factory=lambda: {"type": "diagnostic_report", "fields": ["findings", "evidence"]},
        description="Expected structure of the operational deliverable."
    )
    completion_criteria: List[str] = Field(
        default_factory=list,
        description="Deterministic criteria required to declare completion (e.g. ['files_inspected'])."
    )
    escalation_policy: str = Field(
        default="escalate_on_missing_tool_or_contradiction",
        description="Behavior when local execution cannot proceed."
    )

    @model_validator(mode="after")
    def validate_capabilities(self) -> "TaskDelegationContract":
        """Validates that all requested capability identifiers are recognized."""
        for cap in self.allowed_capabilities:
            cap_lower = cap.strip().lower()
            if cap_lower == "subagents":
                if not self.constraints.subagents_allowed:
                    raise ValueError(
                        "Capability 'subagents' is deferred to Phase 2B unless constraints.subagents_allowed is True."
                    )
            elif (
                cap_lower not in VALID_CAPABILITY_IDENTIFIERS
                and not cap_lower.startswith("mcp.")
                and not cap_lower.startswith("filesystem.")
                and not cap_lower.startswith("desktop")
            ):
                raise ValueError(f"Invalid capability identifier: '{cap}'.")
        return self


class VerifiedEvidenceItem(BaseModel):
    """A factual, verified piece of evidence collected during local operational execution."""
    source: str = Field(..., description="Tool name, file path, or execution command.")
    evidence_type: str = Field(..., description="'stdout', 'diff', 'exit_code', 'file_content', 'schema'.")
    finding: str = Field(..., description="Factual finding discovered from this evidence.")
    artifact_id: Optional[str] = Field(None, description="ArtifactStore pointer if output was offloaded.")
    raw_snippet: Optional[str] = Field(None, description="Short factual excerpt (max 600 chars).")
    exit_code: Optional[int] = Field(None, description="Process exit code where applicable.")


class TaskDelegationResult(BaseModel):
    """Structured result returned by the Local Operations Supervisor to the caller."""
    task_id: str
    parent_conversation_id: str
    outcome: DelegationOutcome
    summary: str
    evidence: List[VerifiedEvidenceItem] = Field(default_factory=list)
    subagent_results: List[Dict[str, Any]] = Field(default_factory=list)
    artifacts_created: List[str] = Field(default_factory=list)
    unresolved_questions: List[str] = Field(default_factory=list)
    escalation_details: Optional[Dict[str, Any]] = None
    metrics: Dict[str, Any] = Field(default_factory=dict)
    return_packet: Optional["CloudReturnPacket"] = Field(
        None, description="Compacted, validated CloudReturnPacket for Cloud Principal handoff."
    )


class OutcomeStatus(str, Enum):
    """Normalized outcome status for cloud-facing consumption."""
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    BLOCKED = "blocked"
    NEEDS_ESCALATION = "needs_escalation"


def outcome_to_status(outcome: DelegationOutcome) -> OutcomeStatus:
    """Deterministically maps internal DelegationOutcome to CloudReturnPacket OutcomeStatus."""
    mapping = {
        DelegationOutcome.COMPLETED: OutcomeStatus.COMPLETED,
        DelegationOutcome.PARTIALLY_COMPLETED: OutcomeStatus.PARTIAL,
        DelegationOutcome.FAILED: OutcomeStatus.FAILED,
        DelegationOutcome.BLOCKED: OutcomeStatus.BLOCKED,
        DelegationOutcome.ESCALATION_REQUIRED: OutcomeStatus.NEEDS_ESCALATION,
        DelegationOutcome.CANCELLED: OutcomeStatus.FAILED,
        DelegationOutcome.OUTCOME_UNKNOWN: OutcomeStatus.FAILED,
    }
    return mapping.get(outcome, OutcomeStatus.FAILED)


class VerifiedFinding(BaseModel):
    """A factual statement backed by deterministic execution evidence."""
    finding_id: str = Field(..., description="Stable deterministic finding identifier (e.g. 'find_1').")
    statement: str = Field(..., description="Concise, verified factual statement.")
    evidence_refs: List[str] = Field(default_factory=list, description="References to supporting tool calls, steps, artifacts, or files.")
    source_location: Optional[str] = Field(None, description="File path, command line, or endpoint where finding was observed.")
    extraction_method: str = Field("deterministic", description="Extraction method used ('deterministic', 'log_scan', 'test_parser', etc.).")
    verified: bool = Field(True, description="Strict boolean verification status.")
    verification_basis: Optional[str] = Field(None, description="Deterministic basis confirming factual support (e.g., 'exit_code', 'test_result', 'json_key_value', 'literal_match', 'traceback_scan').")
    extracted_value: Optional[str] = Field(None, description="Exact value or substring extracted from source.")


class ModelInterpretation(BaseModel):
    """A reasoned interpretation or hypothesis generated by local Qwen."""
    statement: str = Field(..., description="Reasoned interpretation of observations.")
    evidence_refs: List[str] = Field(default_factory=list, description="Supporting evidence items backing this interpretation.")
    uncertainty: str = Field("low", description="Uncertainty level or explanation ('low', 'medium', 'high').")
    assumptions: List[str] = Field(default_factory=list, description="Explicit assumptions made by the model.")


class Contradiction(BaseModel):
    """A preserved disagreement between different tools, files, or subagents."""
    claim_a: str = Field(..., description="First competing statement or finding.")
    claim_b: str = Field(..., description="Second competing statement or finding.")
    source_a: str = Field(..., description="Identifier or tool/subagent of claim A.")
    source_b: str = Field(..., description="Identifier or tool/subagent of claim B.")
    evidence_refs_a: List[str] = Field(default_factory=list, description="Evidence references supporting claim A.")
    evidence_refs_b: List[str] = Field(default_factory=list, description="Evidence references supporting claim B.")
    nature: str = Field(..., description="Nature or category of disagreement.")
    resolved: bool = Field(False, description="Whether the contradiction was resolved.")
    resolution_details: Optional[str] = Field(None, description="Explanation of resolution if resolved.")
    is_contextual_divergence: bool = Field(False, description="True if claims describe different environments/scopes rather than conflicting facts.")
    resolution_provenance: Optional[str] = Field(None, description="Deterministic source or rule establishing resolution.")


class FailureRecord(BaseModel):
    """Structured record of an operational failure, failed test, or exception."""
    failure_type: str = Field(..., description="Type of failure ('TEST_FAILURE', 'NON_ZERO_EXIT', 'EXCEPTION', 'PERMISSION_DENIED', 'TIMEOUT', etc.).")
    error_message: str = Field(..., description="Concise error message or exception trace.")
    source_tool_or_subagent: str = Field(..., description="Tool name or subagent ID where failure occurred.")
    exit_code: Optional[int] = Field(None, description="Process or command exit code.")
    evidence_refs: List[str] = Field(default_factory=list, description="Supporting evidence references.")
    resolved: bool = Field(False, description="Whether this failure was mitigated or resolved during execution.")


class SubagentReceipt(BaseModel):
    """Compact structured receipt representing a child subagent execution."""
    subagent_id: str = Field(..., description="Child subagent session or task identifier.")
    role: str = Field(..., description="Operational role of the subagent.")
    status: str = Field(..., description="Final terminal status (e.g. 'COMPLETED', 'FAILED', 'TIMED_OUT').")
    summary: str = Field(..., description="Compact summary of subagent results.")
    findings_count: int = Field(0, description="Number of distinct findings extracted.")
    failures: List[str] = Field(default_factory=list, description="Failures encountered by this child.")
    evidence_refs: List[str] = Field(default_factory=list, description="Evidence and artifact references produced by child.")


class ArtifactReference(BaseModel):
    """Pointer to a persistent artifact stored in ArtifactStore with cryptographic integrity."""
    artifact_id: str = Field(..., description="Real artifact identifier in ArtifactStore.")
    content_type: str = Field("text/plain", description="MIME content type.")
    byte_size: int = Field(..., ge=0, description="Total size in bytes.")
    line_count: int = Field(0, ge=0, description="Total lines in payload.")
    sha256: str = Field(..., min_length=64, max_length=64, description="Cryptographic SHA-256 digest of original bytes.")
    tool_name: str = Field(..., description="Tool or component that created the artifact.")
    description: str = Field("", description="Brief human/model readable description of artifact contents.")


class EscalationRequirement(BaseModel):
    """Explicit requirement necessitating intervention from Cloud Principal or user."""
    reason: str = Field(..., description="Reason escalation to Cloud Principal or user is needed.")
    severity: str = Field("HIGH", description="Severity level ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL').")
    required_action: str = Field(..., description="Action required from cloud or user.")
    context: Dict[str, Any] = Field(default_factory=dict, description="Diagnostic context for escalation.")


class CompactionMetrics(BaseModel):
    """Token accounting, reduction ratios, and compaction metrics."""
    original_bytes: int = Field(0, ge=0, description="Total raw bytes across all tool outputs and child receipts.")
    original_estimated_tokens: int = Field(0, ge=0, description="Heuristic estimated tokens for raw outputs.")
    final_estimated_tokens: int = Field(0, ge=0, description="Heuristic estimated tokens for finalized packet.")
    reduction_ratio: float = Field(0.0, ge=0.0, description="1.0 - (final / original) reduction percentage.")
    tool_outputs_compacted: int = Field(0, ge=0, description="Count of raw tool outputs compacted.")
    deterministic_extractions: int = Field(0, ge=0, description="Count of deterministic facts extracted.")
    semantic_summaries: int = Field(0, ge=0, description="Count of semantic summary passes executed.")
    subagent_receipts_count: int = Field(0, ge=0, description="Count of subagent receipts processed.")
    duplicate_findings_merged: int = Field(0, ge=0, description="Count of duplicate findings merged.")
    contradictions_count: int = Field(0, ge=0, description="Count of contradictions detected and preserved.")
    artifacts_referenced: int = Field(0, ge=0, description="Count of distinct ArtifactStore artifacts referenced.")
    budget_exceeded: bool = Field(False, description="Whether the context budget was exceeded after compression.")
    estimator_used: str = Field("heuristic_chars_div_4", description="Token estimator used.")
    target_provider: Optional[str] = Field(None, description="Intended receiving cloud provider.")
    target_model: Optional[str] = Field(None, description="Intended receiving cloud model.")
    effective_context_ceiling: int = Field(0, description="Effective context ceiling calculated for receiving model.")
    reserved_context_budget: int = Field(0, description="Context tokens reserved for instructions and reasoning.")
    is_estimated: bool = Field(True, description="Whether token counts are approximate estimates.")
    is_handoff_ready: bool = Field(True, description="Whether the packet fits safely within receiving model remaining capacity.")
    handoff_blocked_reason: Optional[str] = Field(None, description="Reason why handoff is unsafe or blocked.")
    tool_schema_overhead_tokens: int = Field(0, description="Estimated token overhead for tool schemas.")
    capacity_verified: bool = Field(True, description="Whether model context capacity was authoritatively known vs estimated.")


class CloudReturnPacket(BaseModel):
    """Compact, structured, verifiable operational packet returned to Cloud Principal."""
    schema_version: str = Field("1.0", description="Contract schema version.")
    task_id: str = Field(..., min_length=1, description="Delegated task ID.")
    parent_conversation_id: str = Field(..., min_length=1, description="Parent cloud conversation ID.")
    objective: str = Field(..., description="Original delegated objective.")
    outcome: OutcomeStatus = Field(..., description="Structured execution outcome.")
    summary: str = Field(..., description="Executive operational summary for cloud consumption.")
    verified_findings: List[VerifiedFinding] = Field(default_factory=list, description="Facts verified against execution evidence.")
    interpretations: List[ModelInterpretation] = Field(default_factory=list, description="Model interpretations and reasoned inferences.")
    contradictions: List[Contradiction] = Field(default_factory=list, description="Preserved operational disagreements and contradictions.")
    failures: List[FailureRecord] = Field(default_factory=list, description="Execution errors, failed tests, and exceptions.")
    subagent_receipts: List[SubagentReceipt] = Field(default_factory=list, description="Receipts from child subagents.")
    artifact_references: List[ArtifactReference] = Field(default_factory=list, description="Pointers to offloaded persistent artifacts.")
    unresolved_questions: List[str] = Field(default_factory=list, description="Questions or criteria remaining unresolved.")
    escalation_requirements: List[EscalationRequirement] = Field(default_factory=list, description="Explicit escalation requests.")
    compaction_metrics: CompactionMetrics = Field(default_factory=CompactionMetrics, description="Compaction and token efficiency metrics.")


# Resolve forward references in Pydantic models
TaskDelegationResult.model_rebuild()

