"""Cloud Principal Local Delegation Tool: presence.delegate_local_work
Provides the controlled, trusted bridge allowing the Cloud Principal (Gemini / FreeLLMAPI)
to delegate operational investigations and multi-turn execution tasks to the
Local Operations Supervisor (Qwen 3.5 9B via Ollama).
"""

import json
import logging
import uuid
import contextvars
from typing import Dict, Any, List, Optional, Set, Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator

from runtime.permissions import PermissionTier, PermissionBoundary
from runtime.tools.registry import ToolDefinition
from runtime.local_operations.models import (
    TaskDelegationContract,
    TaskDelegationResult,
    DelegationConstraints,
    ExecutionProfile,
    OutcomeStatus,
    outcome_to_status,
)
from runtime.local_operations.validator import recursive_sanitize_packet
from runtime.agent_executor import CancellationToken

logger = logging.getLogger("presence.tools.delegation")

# Thread-safe and async-safe context variables populated during AgentExecutor turn execution
current_tool_call_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_tool_call_id", default=None
)
current_conversation_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_conversation_id", default=None
)
current_cancellation_token: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar(
    "current_cancellation_token", default=None
)


# Canonical set of forbidden authority fields that untrusted models must never override
FORBIDDEN_AUTHORITY_FIELDS: Set[str] = {
    "permission_tier",
    "allowed_capabilities",
    "execution_profile",
    "provider",
    "model",
    "read_only",
    "subagents_allowed",
    "max_subagents",
    "parent_conversation_id",
    "task_id",
}


# Canonical model-facing tool description
DELEGATE_LOCAL_WORK_DESCRIPTION = (
    "Delegate a well-defined operational investigation or execution task "
    "to Presence's local operations supervisor.\n\n"
    "The local supervisor uses Qwen to perform authorized MCP and process "
    "operations, coordinate local subagents when permitted, collect and "
    "validate evidence, and return a compact CloudReturnPacket.\n\n"
    "Use this tool when a request requires local operational work such as "
    "filesystem inspection, project investigation, configuration analysis, "
    "authorized diagnostics, test execution, or other operations supported "
    "by the current runtime permissions.\n\n"
    "Do not use this tool for ordinary conversation, high-level reasoning "
    "that does not require local execution, or tasks outside the authorized "
    "local operational scope.\n\n"
    "The result contains validated findings, interpretations, failures, "
    "contradictions, unresolved questions, and references to locally "
    "preserved artifacts where applicable."
)


# Canonical JSON Schema for model function declarations
DELEGATE_LOCAL_WORK_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "objective": {
            "type": "string",
            "description": "Concrete description of the operational work to be performed locally.",
        },
        "context_summary": {
            "type": "string",
            "description": "Compact background context or technical constraints supplied by the cloud principal.",
            "default": "",
        },
        "expected_deliverable": {
            "type": "string",
            "description": "Description of the requested deliverable (e.g., diagnosis, test failure list, configuration diff).",
            "default": "",
        },
        "completion_criteria": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Concise list of deterministic criteria required to evaluate operational completion.",
            "default": [],
        },
        "supervisor": {
            "type": "string",
            "enum": ["LocalOperationsSupervisor"],
            "description": "Optional supervisor target (strictly constrained to 'LocalOperationsSupervisor').",
            "default": "LocalOperationsSupervisor",
        },
    },
    "required": ["objective"],
    "additionalProperties": False,
}


class DelegateLocalWorkArgs(BaseModel):
    """Strongly validated model for presence.delegate_local_work arguments.
    Enforces extra='forbid' to reject any attempt by the model to override authority fields.
    """
    model_config = ConfigDict(extra="forbid")

    objective: str = Field(
        ...,
        min_length=5,
        max_length=2000,
        description="Concrete description of the operational work to be performed locally.",
    )
    context_summary: str = Field(
        default="",
        max_length=5000,
        description="Compact background context supplied by the cloud principal.",
    )
    expected_deliverable: str = Field(
        default="",
        max_length=1000,
        description="Description of the requested deliverable.",
    )
    completion_criteria: List[str] = Field(
        default_factory=list,
        description="Concise list of deterministic criteria required to evaluate completion.",
    )
    supervisor: Optional[Literal["LocalOperationsSupervisor"]] = Field(
        default="LocalOperationsSupervisor",
        description="Optional supervisor target, strictly constrained to 'LocalOperationsSupervisor'.",
    )

    @field_validator("objective")
    @classmethod
    def validate_objective_not_blank(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped or len(stripped) < 5:
            raise ValueError("objective must contain at least 5 non-whitespace characters.")
        return stripped

    @field_validator("completion_criteria")
    @classmethod
    def validate_criteria(cls, v: List[str]) -> List[str]:
        cleaned = []
        for item in v:
            s = str(item).strip()
            if s:
                cleaned.append(s[:200])
        return cleaned


def validate_delegation_arguments(args: Dict[str, Any]) -> DelegateLocalWorkArgs:
    """Validates delegation arguments against authority rules and DelegateLocalWorkArgs schema.
    Strictly rejects forbidden authority fields and unknown extra properties (fail-closed).
    """
    if not isinstance(args, dict):
        raise ValueError("Delegation arguments must be a dictionary.")

    # 1. Reject forbidden authority override fields immediately (fail-closed)
    detected_forbidden = set(args.keys()) & FORBIDDEN_AUTHORITY_FIELDS
    if detected_forbidden:
        raise ValueError(
            f"Security rejection: model attempted authority override with forbidden field(s): {sorted(detected_forbidden)}"
        )

    # 2. Enforce strict Pydantic model validation (extra='forbid', Literal supervisor)
    clean = dict(args)
    clean.pop("_tool_call_id", None)
    return DelegateLocalWorkArgs.model_validate(clean)


def build_delegate_local_work_tool(
    supervisor: Any,
    parent_session: Any,
    allowed_capabilities: Optional[List[str]] = None,
    read_only: bool = True,
    subagents_allowed: bool = True,
    max_subagents: int = 2,
    timeout_seconds: float = 180.0,
    context_budget_tokens: int = 2000,
) -> ToolDefinition:
    """Factory creating a single canonical ToolDefinition for presence.delegate_local_work.
    Binds the executing Cloud Principal session and local supervisor with:
    - Server-side caller role & identity enforcement
    - Trusted TaskDelegationContract assembly (no model-controlled authority)
    - Idempotency caching per (parent_conversation_id, tool_call_id)
    - Re-entrancy protection
    - Cancellation propagation
    - Context-budget safety gating
    """
    # Track completed tool call results to ensure idempotency across provider retries
    idempotency_cache: Dict[str, Dict[str, Any]] = {}
    active_call_ids: Set[str] = set()

    def _handle_delegate_local_work(args: Dict[str, Any]) -> Dict[str, Any]:
        # 1. Authoritative Server-Side Caller Verification (Strict Fail-Closed)
        # Verify explicit presence of session_role, is_cloud_principal, and metadata/nesting_depth
        if not hasattr(parent_session, "session_role") or getattr(parent_session, "session_role", None) is None:
            err_msg = "Execution blocked: Session metadata missing required 'session_role'."
            logger.warning(f"SECURITY GATE: {err_msg}")
            return {"status": "ERROR", "outcome": "blocked", "error": err_msg}

        if not hasattr(parent_session, "is_cloud_principal") or getattr(parent_session, "is_cloud_principal", None) is None:
            err_msg = "Execution blocked: Session metadata missing required 'is_cloud_principal' (must be is_cloud_principal=True)."
            logger.warning(f"SECURITY GATE: {err_msg}")
            return {"status": "ERROR", "outcome": "blocked", "error": err_msg}

        # Verify nesting depth is explicitly present
        nesting = None
        if hasattr(parent_session, "metadata") and parent_session.metadata is not None:
            nesting = getattr(parent_session.metadata, "nesting_depth", None)
        elif hasattr(parent_session, "nesting_depth"):
            nesting = getattr(parent_session, "nesting_depth", None)

        if nesting is None:
            err_msg = "Execution blocked: Session metadata missing required 'nesting_depth'."
            logger.warning(f"SECURITY GATE: {err_msg}")
            return {"status": "ERROR", "outcome": "blocked", "error": err_msg}

        # Verify nesting depth is strictly 0 (top-level only)
        if nesting != 0:
            err_msg = (
                f"Execution blocked: 'presence.delegate_local_work' can only be invoked "
                f"by the top-level Cloud Principal session (nesting_depth=0), got nesting_depth={nesting}."
            )
            logger.warning(f"SECURITY GATE: {err_msg}")
            return {"status": "ERROR", "outcome": "blocked", "error": err_msg}

        # Verify authorized session role
        if getattr(parent_session, "session_role") not in ("cloud_principal", "main_interactive"):
            err_msg = (
                f"Execution blocked: Session role '{getattr(parent_session, 'session_role')}' "
                "is unauthorized to invoke local delegation."
            )
            logger.warning(f"SECURITY GATE: {err_msg}")
            return {"status": "ERROR", "outcome": "blocked", "error": err_msg}

        # Verify is_cloud_principal is strictly True
        if getattr(parent_session, "is_cloud_principal") is not True:
            err_msg = "Execution blocked: Session must explicitly have is_cloud_principal=True."
            logger.warning(f"SECURITY GATE: {err_msg}")
            return {"status": "ERROR", "outcome": "blocked", "error": err_msg}

        # Clean args for schema validation and extract internal tool call id if present
        clean_args = dict(args)
        internal_call_id = clean_args.pop("_tool_call_id", None)

        # 2. Strict Input Schema & Authority Validation
        try:
            validated_args = validate_delegation_arguments(clean_args)
        except Exception as val_err:
            err_msg = f"Argument validation failed for 'presence.delegate_local_work': {val_err}"
            logger.warning(err_msg)
            return {"status": "ERROR", "outcome": "failed", "error": err_msg}

        if validated_args.supervisor != "LocalOperationsSupervisor":
            err_msg = (
                f"Argument validation failed for 'presence.delegate_local_work': "
                f"Unauthorized supervisor '{validated_args.supervisor}'. "
                "Only 'LocalOperationsSupervisor' is permitted."
            )
            logger.warning(err_msg)
            return {"status": "ERROR", "outcome": "failed", "error": err_msg}

        # 3. Resolve Tool Call Identity & Idempotency Key
        active_call_id = (
            internal_call_id
            or current_tool_call_id.get()
            or f"call_del_{uuid.uuid4().hex[:8]}"
        )
        parent_cid = parent_session.conversation_id
        idempotency_key = f"{parent_cid}:{active_call_id}"

        # 3a. Return cached result if this exact tool call already executed
        if idempotency_key in idempotency_cache:
            logger.info(
                f"Idempotent replay detected for tool call '{active_call_id}' in conversation '{parent_cid}'. "
                "Returning cached result."
            )
            return idempotency_cache[idempotency_key]

        # 3b. Concurrency guard: prevent duplicate simultaneous execution of the same tool call
        if idempotency_key in active_call_ids:
            err_msg = f"Tool call '{active_call_id}' is already actively executing."
            logger.warning(err_msg)
            return {"status": "ERROR", "outcome": "blocked", "error": err_msg}

        active_call_ids.add(idempotency_key)

        # 4. Resolve Cancellation Token from Runtime Context
        active_cancel_token = current_cancellation_token.get()
        if active_cancel_token and active_cancel_token.is_cancelled:
            active_call_ids.discard(idempotency_key)
            return {
                "status": "CANCELLED",
                "outcome": "failed",
                "task_id": "cancelled_before_execution",
                "error": "Delegation cancelled by parent conversation before execution.",
            }

        # 5. Trusted TaskDelegationContract Assembly
        # Runtime-controlled identity, permissions, and execution profile
        task_id = f"task_del_{uuid.uuid4().hex[:12]}"
        effective_capabilities = list(allowed_capabilities) if allowed_capabilities else [
            "filesystem",
            "code_search",
            "all_read_only",
        ]
        if subagents_allowed and "subagents" not in effective_capabilities:
            effective_capabilities.append("subagents")

        contract = TaskDelegationContract(
            task_id=task_id,
            parent_conversation_id=parent_cid,
            objective=validated_args.objective,
            context_summary=validated_args.context_summary,
            execution_profile=ExecutionProfile.LOCAL_SUPERVISOR_ROUTINE,
            constraints=DelegationConstraints(
                read_only=read_only,
                subagents_allowed=subagents_allowed,
                max_subagents=max_subagents,
                timeout_seconds=timeout_seconds,
                context_budget_tokens=context_budget_tokens,
            ),
            allowed_capabilities=effective_capabilities,
            expected_deliverable={
                "type": "operational_report",
                "description": validated_args.expected_deliverable,
            },
            completion_criteria=validated_args.completion_criteria,
        )

        # 6. Execute Local Operations Supervisor (100% Local Execution)
        try:
            logger.info(
                f"Delegating operational task '{task_id}' to LocalOperationsSupervisor "
                f"for parent conversation '{parent_cid}'."
            )
            result: TaskDelegationResult = supervisor.execute(
                contract=contract,
                parent_session=parent_session,
                cancellation_token=active_cancel_token,
            )
        except Exception as exec_err:
            active_call_ids.discard(idempotency_key)
            logger.error(f"LocalOperationsSupervisor exception for task '{task_id}': {exec_err}", exc_info=True)
            err_payload = {
                "status": "ERROR",
                "outcome": "failed",
                "task_id": task_id,
                "error": f"Local operational supervisor execution error: {str(exec_err)}",
                "summary": "The delegated task encountered an unexpected internal error during local execution.",
                "failures": [
                    {
                        "failure_type": "SUPERVISOR_EXCEPTION",
                        "error_message": str(exec_err),
                        "source_tool_or_subagent": "LocalOperationsSupervisor",
                        "resolved": False,
                    }
                ],
            }
            return err_payload
        finally:
            active_call_ids.discard(idempotency_key)

        # 7. Check for Cancellation Post-Execution
        if active_cancel_token and active_cancel_token.is_cancelled:
            return {
                "status": "CANCELLED",
                "outcome": "failed",
                "task_id": task_id,
                "error": "Delegation cancelled by parent conversation.",
                "summary": "Operational execution was cancelled before final synthesis.",
            }

        # 8. Extract & Validate CloudReturnPacket Handoff
        packet = result.return_packet
        if packet is None:
            err_payload = {
                "status": "ERROR",
                "outcome": outcome_to_status(result.outcome).value,
                "task_id": task_id,
                "error": "LocalOperationsSupervisor did not produce a CloudReturnPacket.",
                "summary": result.summary,
            }
            return err_payload

        # Ensure recursive whole-packet credential sanitization pass
        recursive_sanitize_packet(packet)
        packet_dict = packet.model_dump(mode="json")

        # 9. Context-Budget Feasibility Gate & Unsafe Handoff Protection (Phase 2C Compliance)
        is_ready = packet.compaction_metrics.is_handoff_ready
        if not is_ready:
            blocked_reason = packet.compaction_metrics.handoff_blocked_reason or "Context budget constraint violated"
            logger.warning(
                f"CloudReturnPacket for task '{task_id}' is marked not handoff-ready ({blocked_reason}). "
                "Returning compact blocked tool result to prevent receiver context overflow."
            )
            blocked_payload = {
                "status": "BLOCKED",
                "outcome": OutcomeStatus.BLOCKED.value,
                "task_id": task_id,
                "parent_conversation_id": parent_cid,
                "error": f"Handoff blocked: {blocked_reason}. Operational evidence preserved locally.",
                "summary": (
                    packet.summary[:250].strip() + "..."
                    if len(packet.summary) > 250
                    else packet.summary
                ),
                "failures": packet_dict.get("failures", []),
                "contradictions": packet_dict.get("contradictions", []),
                "escalation_requirements": packet_dict.get("escalation_requirements", []),
                "compaction_metrics": packet_dict.get("compaction_metrics", {}),
            }
            # Cache blocked result for idempotency
            idempotency_cache[idempotency_key] = blocked_payload
            return blocked_payload

        # 10. Assemble Clean Model-Facing Handoff Envelope
        handoff_payload = {
            "status": "SUCCESS",
            "task_id": packet.task_id,
            "parent_conversation_id": packet.parent_conversation_id,
            "objective": packet.objective,
            "outcome": packet.outcome if isinstance(packet.outcome, str) else packet.outcome.value,
            "summary": packet.summary,
            "verified_findings": packet_dict.get("verified_findings", []),
            "interpretations": packet_dict.get("interpretations", []),
            "contradictions": packet_dict.get("contradictions", []),
            "failures": packet_dict.get("failures", []),
            "subagent_receipts": packet_dict.get("subagent_receipts", []),
            "artifact_references": packet_dict.get("artifact_references", []),
            "unresolved_questions": packet_dict.get("unresolved_questions", []),
            "escalation_requirements": packet_dict.get("escalation_requirements", []),
            "compaction_metrics": packet_dict.get("compaction_metrics", {}),
        }

        # Cache completed result for idempotency replay
        idempotency_cache[idempotency_key] = handoff_payload
        return handoff_payload

    # Construct and return ToolDefinition
    return ToolDefinition(
        tool_id="presence.delegate_local_work",
        name="delegate_local_work",
        description=DELEGATE_LOCAL_WORK_DESCRIPTION,
        parameters=DELEGATE_LOCAL_WORK_SCHEMA,
        authority_tier=PermissionTier.L0_OBSERVE if read_only else PermissionTier.L2_EXECUTE_REVERSIBLE,
        handler=_handle_delegate_local_work,
        timeout_seconds=timeout_seconds,
        read_only=read_only,
        idempotent=True,
    )
