"""Native Workflow Management Tools
Exposes deterministic DAG orchestration (presence.run_workflow) and lifecycle management
(presence.manage_workflow) to the AgentExecutor and conversational sessions.
"""

import logging
from typing import Dict, Any, List, Optional

from runtime.permissions import PermissionTier
from runtime.tools.registry import ToolDefinition
from runtime.workflows.models import (
    WorkflowDefinition,
    WorkflowNode,
    WorkflowStatus,
    FailurePolicy,
)
from runtime.workflows.engine import WorkflowEngine
from runtime.agent_executor import CancellationToken

logger = logging.getLogger("presence.tools.workflow")


def build_workflow_tools(
    workflow_engine: WorkflowEngine,
    current_conversation_id: str,
) -> List[ToolDefinition]:
    """Constructs ToolDefinitions for presence.run_workflow and presence.manage_workflow."""

    def _handle_run_workflow(args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            nodes_data = args.get("nodes", [])
            if not nodes_data or not isinstance(nodes_data, list):
                return {
                    "status": "ERROR",
                    "error": "Parameter 'nodes' must be a non-empty list of node definitions."
                }

            wf_id = args.get("workflow_id") or f"wf_{args.get('name', 'unnamed').lower().replace(' ', '_')}"
            defn = WorkflowDefinition.from_dict({
                "workflow_id": wf_id,
                "name": args.get("name", wf_id),
                "description": args.get("description", ""),
                "nodes": nodes_data,
                "failure_policy": args.get("failure_policy", FailurePolicy.FAIL_FAST.value),
                "max_parallel_nodes": int(args.get("max_parallel_nodes", 5)),
            })

            # Execute workflow
            run = workflow_engine.execute_workflow(
                definition=defn,
                conversation_id=current_conversation_id,
            )

            node_summaries = {}
            for nid, ne in run.node_executions.items():
                node_summaries[nid] = {
                    "state": ne.state.value,
                    "duration_seconds": round(ne.duration_seconds, 2),
                    "artifact_id": ne.artifact_id,
                    "error": ne.error_message,
                }

            return {
                "status": "SUCCESS" if run.status == WorkflowStatus.COMPLETED else "FAILED",
                "workflow_status": run.status.value,
                "run_id": run.run_id,
                "workflow_id": run.workflow_id,
                "duration_seconds": round(run.duration_seconds, 2),
                "final_artifact_id": run.final_artifact_id,
                "node_results": node_summaries,
                "error": run.error_message,
            }

        except Exception as e:
            logger.error(f"Error executing run_workflow: {e}", exc_info=True)
            return {
                "status": "ERROR",
                "error": str(e)
            }

    def _handle_manage_workflow(args: Dict[str, Any]) -> Dict[str, Any]:
        action = args.get("action", "list_runs").lower()
        run_id = args.get("run_id")

        if action == "list_runs":
            runs = workflow_engine.list_runs(conversation_id=current_conversation_id)
            return {"status": "SUCCESS", "runs": runs, "count": len(runs)}

        elif action == "get_status":
            if not run_id:
                return {"status": "ERROR", "error": "Missing 'run_id' parameter for get_status."}
            run = workflow_engine.get_run(run_id)
            if not run:
                return {"status": "ERROR", "error": f"Workflow run '{run_id}' not found."}
            return {"status": "SUCCESS", "run": run.to_dict()}

        elif action == "cancel":
            if not run_id:
                return {"status": "ERROR", "error": "Missing 'run_id' parameter for cancel."}
            reason = args.get("reason", "Cancelled by user tool request")
            success = workflow_engine.cancel_workflow(run_id, reason)
            return {"status": "SUCCESS" if success else "NOT_FOUND", "cancelled": success, "run_id": run_id}

        else:
            return {"status": "ERROR", "error": f"Unknown action '{action}'. Valid actions: list_runs, get_status, cancel."}

    run_workflow_tool = ToolDefinition(
        tool_id="presence.run_workflow",
        name="run_workflow",
        description=(
            "Executes a deterministic multi-stage Directed Acyclic Graph (DAG) workflow with bounded concurrency, "
            "branch dependencies, and artifact aggregation. Independent branches execute in parallel with ZERO "
            "conversational LLM overhead. Ideal for multi-step audits, batch searches, multi-file inspection, and report generation."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Human-readable name of the workflow."
                },
                "description": {
                    "type": "string",
                    "description": "Summary of workflow purpose."
                },
                "nodes": {
                    "type": "array",
                    "description": "List of DAG node definitions.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "node_id": {"type": "string", "description": "Unique identifier for this node."},
                            "name": {"type": "string", "description": "Display name of the step."},
                            "execution_type": {
                                "type": "string",
                                "enum": ["TOOL", "SUBAGENT", "PROCESS", "AGGREGATE", "VERIFY"],
                                "description": "Type of execution adapter to invoke."
                            },
                            "payload": {
                                "type": "object",
                                "description": "Adapter-specific configuration (e.g. {'command': '...'}, {'tool_name': '...', 'args': {...}}, {'goal': '...', 'role': '...'})."
                            },
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Node IDs that must complete before this node can run."
                            },
                            "phase": {
                                "type": "integer",
                                "description": "Phase barrier number (default 1). Nodes in phase K only run after phase < K completes."
                            },
                            "optional": {
                                "type": "boolean",
                                "description": "If true, failure of this node does not abort the workflow."
                            },
                            "timeout_seconds": {
                                "type": "number",
                                "description": "Maximum execution duration before node timeout."
                            }
                        },
                        "required": ["node_id", "execution_type", "payload"],
                        "additionalProperties": False
                    }
                },
                "failure_policy": {
                    "type": "string",
                    "enum": ["FAIL_FAST", "CONTINUE_INDEPENDENT", "ALLOW_OPTIONAL"],
                    "description": "Policy governing behavior when a node fails."
                },
                "max_parallel_nodes": {
                    "type": "integer",
                    "description": "Maximum concurrent branches (default 5)."
                }
            },
            "required": ["name", "nodes"],
            "additionalProperties": False
        },
        authority_tier=PermissionTier.L2_EXECUTE_REVERSIBLE,
        handler=_handle_run_workflow,
        timeout_seconds=3600.0,
    )

    manage_workflow_tool = ToolDefinition(
        tool_id="presence.manage_workflow",
        name="manage_workflow",
        description="Inspects status, history, or cancels active deterministic workflow executions.",
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list_runs", "get_status", "cancel"],
                    "description": "Management action to perform."
                },
                "run_id": {
                    "type": "string",
                    "description": "Unique workflow run ID (required for get_status and cancel)."
                },
                "reason": {
                    "type": "string",
                    "description": "Cancellation reason (optional)."
                }
            },
            "required": ["action"],
            "additionalProperties": False
        },
        authority_tier=PermissionTier.L0_OBSERVE,
        handler=_handle_manage_workflow,
        timeout_seconds=30.0,
    )

    return [run_workflow_tool, manage_workflow_tool]
