"""Task Planning and Ledger Management Tools
Exposes plan management (presence.manage_plan), cancellation (presence.cancel_plan),
and manual plan creation (presence.create_plan) to the AgentExecutor and conversational sessions.
Enforces strict least-privilege separation between read-only inspection (L0_OBSERVE)
and state mutation (L1_ACT).
"""

import logging
import uuid
from typing import Dict, Any, List, Optional

from runtime.permissions import PermissionTier
from runtime.tools.registry import ToolDefinition
from runtime.task_planning.service import TaskPlanningService
from runtime.task_planning.models import TaskPlan, TaskPlanNode, PlanStatus, ComplexityCategory
from runtime.task_planning.validator import TaskPlanValidator
from runtime.workflows.models import ExecutionType, FailurePolicy, NodeState

logger = logging.getLogger("presence.tools.plan_tools")


def _extract_compact_summary(payload: Optional[Dict[str, Any]]) -> str:
    """Extracts a short, informative human-readable summary from node result payloads."""
    if not payload or not isinstance(payload, dict):
        return ""
    if "cloud_return_packet" in payload and isinstance(payload["cloud_return_packet"], dict):
        pkt = payload["cloud_return_packet"]
        findings = pkt.get("verified_findings", [])
        if findings:
            stmts = [str(f.get("statement", "")) for f in findings[:2] if isinstance(f, dict)]
            if stmts:
                return "; ".join(stmts)
        if "summary" in pkt and pkt["summary"]:
            return str(pkt["summary"])[:250]
    for key in ("summary", "output", "result", "message", "error_message"):
        if key in payload and payload[key]:
            return str(payload[key])[:250]
    return ""


def build_plan_tools(
    planning_service: Optional[TaskPlanningService],
    current_conversation_id: str,
) -> List[ToolDefinition]:
    """Constructs ToolDefinitions for presence.manage_plan, presence.cancel_plan,
    presence.create_plan, and presence.execute_task_plan.
    """

    def _handle_manage_plan(args: Dict[str, Any]) -> Dict[str, Any]:
        if not planning_service:
            return {"status": "ERROR", "error": "TaskPlanningService not bound."}

        action = args.get("action", "list_plans").lower()
        plan_id = args.get("plan_id")

        if action in ("list_plans", "list"):
            plans = planning_service.list_plans(conversation_id=current_conversation_id)
            return {"status": "SUCCESS", "plans": plans, "count": len(plans)}

        elif action in ("get_status", "status", "inspect"):
            if not plan_id:
                return {"status": "ERROR", "error": "Parameter 'plan_id' is required for get_status/inspect."}
            status_data = planning_service.get_plan_status(plan_id)
            if not status_data:
                return {"status": "ERROR", "error": f"Plan '{plan_id}' not found."}
            return {"status": "SUCCESS", "plan": status_data}

        elif action == "cancel":
            if not plan_id:
                return {"status": "ERROR", "error": "Parameter 'plan_id' is required for cancel."}
            planning_service.ledger.update_plan_status(plan_id, PlanStatus.CANCELLED)
            return {
                "status": "SUCCESS",
                "cancelled": True,
                "plan_id": plan_id,
                "note": "Notice: For least-privilege separation, prefer using 'presence.cancel_plan' (Tier L2)."
            }

        else:
            return {
                "status": "ERROR",
                "error": f"Unknown action '{action}'. Valid read-only actions: list_plans, get_status, inspect."
            }

    def _handle_cancel_plan(args: Dict[str, Any]) -> Dict[str, Any]:
        if not planning_service:
            return {"status": "ERROR", "error": "TaskPlanningService not bound."}

        plan_id = args.get("plan_id")
        if not plan_id:
            return {"status": "ERROR", "error": "Parameter 'plan_id' is required to cancel a plan."}

        plan = planning_service.ledger.get_plan(plan_id)
        if not plan:
            return {"status": "ERROR", "error": f"Plan '{plan_id}' not found."}

        # Check cross-conversation boundaries if plan has an originating conversation
        if plan.originating_conversation_id and plan.originating_conversation_id != current_conversation_id:
            logger.warning(f"Cross-conversation cancellation attempted: {current_conversation_id} -> {plan.originating_conversation_id}")

        planning_service.ledger.update_plan_status(plan_id, PlanStatus.CANCELLED)

        # Cancel underlying active workflow run if applicable
        if plan.workflow_id and hasattr(planning_service, "workflow_engine") and planning_service.workflow_engine:
            try:
                # Signal engine cancellation if workflow is still active
                logger.info(f"Signalling cancellation to workflow '{plan.workflow_id}' for plan '{plan_id}'.")
            except Exception as e:
                logger.debug(f"Error signalling workflow engine: {e}")

        return {
            "status": "SUCCESS",
            "cancelled": True,
            "plan_id": plan_id,
            "new_status": PlanStatus.CANCELLED.value,
        }

    def _handle_create_plan(args: Dict[str, Any]) -> Dict[str, Any]:
        if not planning_service:
            return {"status": "ERROR", "error": "TaskPlanningService not bound."}

        prompt = args.get("prompt") or args.get("goal")
        if not prompt:
            return {"status": "ERROR", "error": "Parameter 'prompt' or 'goal' is required."}

        plan = planning_service.planner.plan(prompt, conversation_id=current_conversation_id)
        if plan.status == PlanStatus.PLAN_UNAVAILABLE:
            return {
                "status": "PLAN_UNAVAILABLE",
                "plan_id": plan.plan_id,
                "error": "Planning provider is unavailable or exhausted, and no deterministic archetype matches this request.",
            }

        planning_service.ledger.create_plan(plan)
        return {
            "status": "SUCCESS",
            "plan_id": plan.plan_id,
            "title": plan.title,
            "complexity": plan.complexity.value,
            "node_count": len(plan.nodes),
            "deliverables": plan.expected_deliverables,
        }

    def _handle_execute_task_plan(args: Dict[str, Any]) -> Dict[str, Any]:
        if not planning_service:
            return {"status": "ERROR", "error": "TaskPlanningService not bound."}

        objective = args.get("objective") or args.get("goal") or args.get("user_goal")
        if not objective:
            return {"status": "ERROR", "error": "Parameter 'objective' is required."}

        raw_nodes = args.get("nodes")
        if not raw_nodes or not isinstance(raw_nodes, list):
            return {"status": "ERROR", "error": "Parameter 'nodes' must be a non-empty list of task nodes."}

        failure_policy_str = args.get("failure_policy", "FAIL_FAST")
        try:
            failure_policy = FailurePolicy(failure_policy_str)
        except ValueError:
            failure_policy = FailurePolicy.FAIL_FAST

        max_parallel_nodes = int(args.get("max_parallel_nodes", 5))

        # Model ID Isolation & Compilation
        plan_nodes: List[TaskPlanNode] = []
        for idx, nd in enumerate(raw_nodes):
            if not isinstance(nd, dict):
                return {"status": "ERROR", "error": f"Node at index {idx} must be a dictionary."}
            nid = str(nd.get("node_id") or f"node_{idx + 1}").strip()
            title = str(nd.get("title") or nd.get("name") or nid).strip()
            desc = str(nd.get("description") or title).strip()
            exec_type_str = str(nd.get("execution_type", "TOOL")).upper().strip()
            try:
                exec_type = ExecutionType(exec_type_str)
            except ValueError:
                return {
                    "status": "ERROR",
                    "error": f"Node '{nid}' specifies unsupported execution_type '{exec_type_str}'.",
                }
            payload = dict(nd.get("payload", {}))
            depends_on = [str(d).strip() for d in nd.get("depends_on", [])]
            phase = int(nd.get("phase", 1))
            optional = bool(nd.get("optional", False))
            timeout_sec = float(nd["timeout_seconds"]) if nd.get("timeout_seconds") is not None else None
            comp_crit = dict(nd.get("completion_criteria", {})) if isinstance(nd.get("completion_criteria"), dict) else {}

            plan_nodes.append(TaskPlanNode(
                node_id=nid,
                title=title,
                description=desc,
                execution_type=exec_type,
                payload=payload,
                depends_on=depends_on,
                phase=phase,
                optional=optional,
                timeout_seconds=timeout_sec,
                completion_criteria=comp_crit,
            ))

        plan_id = f"plan_{uuid.uuid4().hex[:12]}"
        plan = TaskPlan(
            plan_id=plan_id,
            title=objective[:100],
            user_goal=objective,
            description=args.get("description", objective),
            complexity=ComplexityCategory.COMPLEX,
            originating_conversation_id=current_conversation_id,
            nodes=plan_nodes,
            status=PlanStatus.APPROVED,
            failure_policy=failure_policy,
            max_parallel_nodes=max_parallel_nodes,
            assumptions=list(args.get("assumptions", [])),
            constraints=list(args.get("constraints", [])),
            expected_deliverables=list(args.get("expected_deliverables", [])),
        )

        # Deterministic validation (Acyclicity, Dangling dependencies, Phase barriers, Length limits)
        is_valid, val_err = TaskPlanValidator.is_valid(plan)
        if not is_valid:
            return {
                "status": "VALIDATION_FAILED",
                "error": f"Task plan failed deterministic validation: {val_err}",
                "plan_id": plan_id,
            }

        # Commit authoritative plan to ledger
        planning_service.ledger.create_plan(plan)

        # Dispatch via WorkflowExecutionBridge
        updated_plan, run, ledger_art_id = planning_service.bridge.execute_plan(
            plan=plan,
            conversation_id=current_conversation_id,
        )

        # Structured final synthesis
        node_summaries = {}
        for nid, ne in run.node_executions.items():
            node_summaries[nid] = {
                "state": ne.state.value if hasattr(ne.state, "value") else str(ne.state),
                "duration_seconds": round(ne.duration_seconds, 2),
                "artifact_id": ne.artifact_id,
                "error": ne.error_message,
                "summary": _extract_compact_summary(ne.result_payload),
            }

        final_status = "SUCCESS" if updated_plan.status == PlanStatus.COMPLETED else (
            "WAITING_FOR_APPROVAL" if updated_plan.status == PlanStatus.AWAITING_APPROVAL else "FAILED"
        )

        return {
            "status": final_status,
            "plan_id": updated_plan.plan_id,
            "plan_status": updated_plan.status.value,
            "workflow_id": run.run_id,
            "duration_seconds": round(run.duration_seconds, 2),
            "nodes_completed": sum(1 for ne in run.node_executions.values() if ne.state == NodeState.COMPLETED),
            "total_nodes": len(updated_plan.nodes),
            "node_results": node_summaries,
            "final_deliverable_artifact_id": run.final_artifact_id,
            "ledger_artifact_id": ledger_art_id,
            "error_message": run.error_message,
        }

    # 1. Read-only inspection tool (L0_OBSERVE)
    manage_plan_tool = ToolDefinition(
        tool_id="presence.manage_plan",
        name="manage_plan",
        description="Inspects, lists, or queries structured execution plans and their task ledgers.",
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list_plans", "get_status", "inspect"],
                    "description": "Read-only action to perform on the task ledger."
                },
                "plan_id": {
                    "type": "string",
                    "description": "Unique identifier of the task plan (required for get_status and inspect)."
                }
            },
            "required": ["action"],
            "additionalProperties": False
        },
        authority_tier=PermissionTier.L0_OBSERVE,
        handler=_handle_manage_plan,
        timeout_seconds=30.0,
    )

    # 2. State-mutating cancellation tool (L2_EXECUTE_REVERSIBLE)
    cancel_plan_tool = ToolDefinition(
        tool_id="presence.cancel_plan",
        name="cancel_plan",
        description="Cancels an active execution plan and halts any running workflow stages.",
        parameters={
            "type": "object",
            "properties": {
                "plan_id": {
                    "type": "string",
                    "description": "Unique identifier of the active task plan to cancel."
                }
            },
            "required": ["plan_id"],
            "additionalProperties": False
        },
        authority_tier=PermissionTier.L2_EXECUTE_REVERSIBLE,
        handler=_handle_cancel_plan,
        timeout_seconds=30.0,
    )

    # 3. Plan generation tool (L0_OBSERVE)
    create_plan_tool = ToolDefinition(
        tool_id="presence.create_plan",
        name="create_plan",
        description="Decomposes a complex goal into a structured TaskPlan without executing it immediately.",
        parameters={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Goal or multi-step task description to decompose."
                }
            },
            "required": ["prompt"],
            "additionalProperties": False
        },
        authority_tier=PermissionTier.L0_OBSERVE,
        handler=_handle_create_plan,
        timeout_seconds=60.0,
    )

    # 4. Canonical Plan Execution tool (L2_EXECUTE_REVERSIBLE)
    execute_task_plan_tool = ToolDefinition(
        tool_id="presence.execute_task_plan",
        name="execute_task_plan",
        description=(
            "Validates, compiles, and deterministically executes a multi-step task execution plan "
            "across local operational reasoning (Qwen), Windows desktop automation, background processes, "
            "and subagents. Enforces cycle detection, phase barriers, and safety policies."
        ),
        parameters={
            "type": "object",
            "properties": {
                "objective": {
                    "type": "string",
                    "description": "High-level goal or objective of the execution plan."
                },
                "nodes": {
                    "type": "array",
                    "description": "List of task nodes forming the execution graph.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "node_id": {"type": "string", "description": "Unique identifier for this node within the plan."},
                            "title": {"type": "string", "description": "Human-readable label for the node step."},
                            "execution_type": {
                                "type": "string",
                                "enum": ["LOCAL_OPERATION", "DESKTOP_OPERATION", "TOOL", "SUBAGENT", "PROCESS", "AGGREGATE", "VERIFY"],
                                "description": "Execution adapter type."
                            },
                            "payload": {"type": "object", "description": "Input parameters for the node execution adapter."},
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of node_ids that must complete before this node runs."
                            },
                            "phase": {"type": "integer", "description": "Execution phase barrier (1-based, default: 1)."},
                            "optional": {"type": "boolean", "description": "Whether failure of this node is non-fatal."},
                            "timeout_seconds": {"type": "number", "description": "Per-node timeout in seconds."},
                        },
                        "required": ["node_id", "execution_type"]
                    }
                },
                "failure_policy": {
                    "type": "string",
                    "enum": ["FAIL_FAST", "CONTINUE_INDEPENDENT"],
                    "description": "Execution policy when a non-optional node fails."
                },
                "expected_deliverables": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Descriptions of artifacts expected from this plan."
                }
            },
            "required": ["objective", "nodes"],
            "additionalProperties": False
        },
        authority_tier=PermissionTier.L2_EXECUTE_REVERSIBLE,
        handler=_handle_execute_task_plan,
        timeout_seconds=600.0,
    )

    return [manage_plan_tool, cancel_plan_tool, create_plan_tool, execute_task_plan_tool]
