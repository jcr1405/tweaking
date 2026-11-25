"""Local Operation Workflow Adapter
Executes autonomous operational investigations via LocalOperationsSupervisor.
Enforces local Qwen pinning, deterministic completion, and compact CloudReturnPacket output.
"""

import json
import logging
import uuid
from typing import Dict, Any, Optional

from runtime.workflows.models import WorkflowNode, NodeState
from runtime.workflows.adapters.base import BaseWorkflowAdapter, NodeExecutionContext, NodeExecutionResult
from runtime.local_operations.models import (
    TaskDelegationContract,
    DelegationConstraints,
    ExecutionProfile,
    DelegationOutcome,
    OutcomeStatus,
)

logger = logging.getLogger("presence.workflows.adapters.local_operation")


class LocalOperationWorkflowAdapter(BaseWorkflowAdapter):
    """Adapter executing LOCAL_OPERATION nodes through LocalOperationsSupervisor."""

    def execute(self, node: WorkflowNode, context: NodeExecutionContext) -> NodeExecutionResult:
        supervisor = context.local_supervisor
        if not supervisor:
            return NodeExecutionResult(
                state=NodeState.FAILED,
                error_message="LocalOperationsSupervisor is required in NodeExecutionContext for LOCAL_OPERATION node.",
            )

        if context.cancellation_token and context.cancellation_token.is_cancelled:
            return NodeExecutionResult(
                state=NodeState.CANCELLED,
                error_message="Execution cancelled before node started.",
            )

        payload = node.payload or {}
        objective = str(payload.get("objective", node.name or "Local operational task"))
        context_summary = str(payload.get("context_summary", ""))
        expected_deliverable = str(payload.get("expected_deliverable", ""))
        completion_criteria = list(payload.get("completion_criteria", []))
        constraints_dict = dict(payload.get("constraints", {}))

        read_only = bool(payload.get("read_only", constraints_dict.get("read_only", True)))
        max_turns = int(payload.get("max_turns", constraints_dict.get("max_turns", 15)))
        timeout_seconds = float(node.timeout_seconds or payload.get("timeout_seconds", 300.0))
        allowed_caps = list(payload.get("allowed_capabilities", constraints_dict.get("allowed_capabilities", [])))
        profile_str = str(payload.get("execution_profile", "local_supervisor_routine")).lower()

        # 1. Resolve template variables from context with strict dependency authorization
        objective = context.resolve_template_variables(objective, authorized_dependencies=node.depends_on)
        context_summary = context.resolve_template_variables(context_summary, authorized_dependencies=node.depends_on)
        expected_deliverable = context.resolve_template_variables(expected_deliverable, authorized_dependencies=node.depends_on)

        # 2. Predecessor data-flow integration
        if node.depends_on:
            pred_results = context.get_predecessor_results(node.depends_on)
            pred_lines = []
            for dep_id, dep_data in pred_results.items():
                res_payload = dep_data.get("result_payload", {})
                # Extract compact summary
                summary_snip = ""
                if isinstance(res_payload, dict):
                    if "cloud_return_packet" in res_payload and isinstance(res_payload["cloud_return_packet"], dict):
                        packet = res_payload["cloud_return_packet"]
                        findings = packet.get("verified_findings", [])
                        if findings:
                            summary_snip = f"Findings: {[f.get('statement') for f in findings[:2]]}"
                    elif "summary" in res_payload:
                        summary_snip = str(res_payload["summary"])[:200]
                    elif "output" in res_payload:
                        summary_snip = str(res_payload["output"])[:200]
                art_id = dep_data.get("artifact_id")
                pred_lines.append(f"- Step [{dep_id}] ({dep_data.get('state')}): {summary_snip} (Artifact: {art_id})")

            if pred_lines:
                context_summary += "\n\nPredecessor Step Results:\n" + "\n".join(pred_lines)

        # 3. Construct strongly typed TaskDelegationContract
        if "deep" in profile_str:
            profile = ExecutionProfile.LOCAL_SUPERVISOR_DEEP
        else:
            profile = ExecutionProfile.LOCAL_SUPERVISOR_ROUTINE

        effective_allowed_caps = allowed_caps if allowed_caps else ["mcp.filesystem", "mcp.code_search"]
        if isinstance(expected_deliverable, dict):
            expected_deliverable_dict = expected_deliverable
        else:
            expected_deliverable_dict = {"type": "diagnostic_report", "description": str(expected_deliverable or "operational report")}

        constraints = DelegationConstraints(
            read_only=read_only,
            max_turns=max_turns,
            max_duration_seconds=timeout_seconds,
            allowed_capabilities=effective_allowed_caps,
        )

        task_id = f"task_{node.node_id}_{uuid.uuid4().hex[:6]}"

        contract = TaskDelegationContract(
            task_id=task_id,
            parent_conversation_id=context.conversation_id,
            objective=objective,
            context_summary=context_summary,
            expected_deliverable=expected_deliverable_dict,
            constraints=constraints,
            allowed_capabilities=effective_allowed_caps,
            execution_profile=profile,
        )

        # 4. Dispatch execution to supervisor
        try:
            delegation_result = supervisor.execute(
                contract=contract,
                cancellation_token=context.cancellation_token,
            )
        except Exception as exc:
            logger.error(f"LocalOperationsSupervisor unhandled exception for node '{node.node_id}': {exc}", exc_info=True)
            return NodeExecutionResult(
                state=NodeState.FAILED,
                error_message=f"Supervisor execution failed: {exc}",
            )

        # 5. Extract results and artifacts
        result_payload = delegation_result.model_dump() if hasattr(delegation_result, "model_dump") else (
            delegation_result.to_dict() if hasattr(delegation_result, "to_dict") else {}
        )
        packet = getattr(delegation_result, "return_packet", None) or getattr(delegation_result, "cloud_return_packet", None)
        primary_artifact_id = None
        if packet and hasattr(packet, "artifacts") and packet.artifacts:
            primary_artifact_id = packet.artifacts[0].artifact_id

        outcome = getattr(delegation_result, "outcome", None)
        if outcome == DelegationOutcome.COMPLETED or outcome == "COMPLETED":
            return NodeExecutionResult(
                state=NodeState.COMPLETED,
                result_payload=result_payload,
                artifact_id=primary_artifact_id,
            )
        elif outcome == DelegationOutcome.CANCELLED or outcome == "CANCELLED":
            return NodeExecutionResult(
                state=NodeState.CANCELLED,
                result_payload=result_payload,
                error_message="Local operation was cancelled.",
            )
        else:
            err_msg = getattr(delegation_result, "summary", None) or getattr(delegation_result, "error_message", None) or "Local operation failed"
            return NodeExecutionResult(
                state=NodeState.FAILED,
                result_payload=result_payload,
                artifact_id=primary_artifact_id,
                error_message=str(err_msg),
            )
