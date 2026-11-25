"""Tool Execution Adapter for Workflow Nodes
Executes registered tools through ToolRegistry with permission checking and artifact offloading.
"""

import json
import logging
from typing import Dict, Any

from runtime.workflows.adapters.base import BaseWorkflowAdapter, NodeExecutionContext, NodeExecutionResult
from runtime.workflows.models import WorkflowNode, NodeState

logger = logging.getLogger("presence.workflows.adapters.tool")


class ToolWorkflowAdapter(BaseWorkflowAdapter):
    """Executes a single tool registered in the session ToolRegistry."""

    def execute(self, node: WorkflowNode, context: NodeExecutionContext) -> NodeExecutionResult:
        if not context.tool_registry:
            return NodeExecutionResult(
                state=NodeState.FAILED,
                error_message="ToolRegistry not available in NodeExecutionContext."
            )

        payload = node.payload or {}
        tool_name = payload.get("tool_name")
        tool_def = context.tool_registry.get_tool(tool_name)

        if not tool_def:
            return NodeExecutionResult(
                state=NodeState.FAILED,
                error_message=f"Tool '{tool_name}' not found in ToolRegistry."
            )

        # 1. Resolve arguments and template variables
        raw_args = dict(payload.get("args", {}))
        resolved_args: Dict[str, Any] = {}
        for k, v in raw_args.items():
            if isinstance(v, str):
                resolved_args[k] = context.resolve_template_variables(v)
            else:
                resolved_args[k] = v

        # 2. Check cooperative cancellation
        if context.cancellation_token and context.cancellation_token.is_cancelled:
            return NodeExecutionResult(
                state=NodeState.CANCELLED,
                error_message="Execution cancelled before tool invocation."
            )

        # 3. Execute tool via ToolRegistry.execute (validates schema, enforces boundary & policies)
        try:
            logger.info(f"Executing workflow tool node '{node.node_id}' ({tool_name})")
            from runtime.permissions import PermissionBoundary
            boundary = context.permission_boundary or PermissionBoundary()
            exec_envelope = context.tool_registry.execute(
                tool_id=tool_name,
                args=resolved_args,
                permission_boundary=boundary,
                conversation_id=context.conversation_id,
            )

            if exec_envelope.get("status") == "ERROR":
                return NodeExecutionResult(
                    state=NodeState.FAILED,
                    error_message=exec_envelope.get("error", "Tool execution failed")
                )

            result = exec_envelope.get("result")
            if not isinstance(result, dict):
                result = {"output": result}

            artifact_id = result.get("artifact_id")

            # Check if output is large and should be offloaded
            if not artifact_id and context.artifact_store:
                res_str = json.dumps(result)
                if context.artifact_store.should_offload(res_str):
                    meta = context.artifact_store.store_text_artifact(
                        conversation_id=context.conversation_id,
                        tool_call_id=node.node_id,
                        tool_name=tool_name,
                        content_str=res_str
                    )
                    artifact_id = meta.artifact_id
                    result["artifact_id"] = artifact_id

            return NodeExecutionResult(
                state=NodeState.COMPLETED,
                result_payload=result,
                artifact_id=artifact_id
            )
        except Exception as e:
            logger.error(f"Error executing tool '{tool_name}' in node '{node.node_id}': {e}", exc_info=True)
            return NodeExecutionResult(
                state=NodeState.FAILED,
                error_message=str(e)
            )
