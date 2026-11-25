"""Subagent Execution Adapter for Workflow Nodes
Spawns isolated ephemeral subagent sessions through SubagentManager.
"""

import time
import logging
from typing import Dict, Any

from runtime.workflows.adapters.base import BaseWorkflowAdapter, NodeExecutionContext, NodeExecutionResult
from runtime.workflows.models import WorkflowNode, NodeState
from runtime.subagents.models import SubagentConfig, SubagentRole

logger = logging.getLogger("presence.workflows.adapters.subagent")


class SubagentWorkflowAdapter(BaseWorkflowAdapter):
    """Executes a subagent in an isolated trajectory."""

    def execute(self, node: WorkflowNode, context: NodeExecutionContext) -> NodeExecutionResult:
        if not context.subagent_manager:
            return NodeExecutionResult(
                state=NodeState.FAILED,
                error_message="SubagentManager not available in NodeExecutionContext."
            )

        payload = node.payload or {}
        raw_goal = payload.get("goal") or payload.get("prompt", "")
        goal = context.resolve_template_variables(raw_goal)
        role = payload.get("role", SubagentRole.GENERAL.value)
        timeout = node.timeout_seconds or payload.get("timeout_seconds", 300.0)
        tools_whitelist = payload.get("tool_whitelist") or payload.get("tools_whitelist")
        max_iterations = payload.get("max_iterations", 15)

        if context.cancellation_token and context.cancellation_token.is_cancelled:
            return NodeExecutionResult(
                state=NodeState.CANCELLED,
                error_message="Execution cancelled before spawning subagent."
            )

        config = SubagentConfig(
            role=role,
            prompt=goal,
            tools_whitelist=tools_whitelist,
            timeout_seconds=timeout,
            max_iterations=max_iterations,
        )

        try:
            logger.info(f"Spawning subagent node '{node.node_id}' with role '{role}'")
            res = context.subagent_manager.spawn_subagent(
                parent_conversation_id=context.conversation_id,
                config=config,
                wait_ms=int(min(10000, timeout * 1000))
            )

            status = res.get("status")
            subagent_id = res.get("subagent_id")

            # If backgrounded, poll until terminal or timeout/cancellation
            if status == "BACKGROUNDED" and subagent_id:
                start_poll = time.time()
                poll_interval = 0.5
                while True:
                    if context.cancellation_token and context.cancellation_token.is_cancelled:
                        context.subagent_manager.kill_subagent(subagent_id, reason="Workflow cancelled")
                        return NodeExecutionResult(
                            state=NodeState.CANCELLED,
                            error_message="Subagent cancelled by workflow cancellation token."
                        )

                    st = context.subagent_manager.get_subagent_status(subagent_id)
                    if not st:
                        break

                    sub_state = st.get("status")
                    if sub_state in ("COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED"):
                        res = st
                        status = sub_state
                        break

                    if (time.time() - start_poll) > timeout:
                        context.subagent_manager.kill_subagent(subagent_id, reason="Workflow subagent timeout exceeded")
                        return NodeExecutionResult(
                            state=NodeState.FAILED,
                            result_payload=st,
                            error_message=f"Subagent exceeded timeout limit of {timeout}s."
                        )

                    time.sleep(poll_interval)

            if status in ("SUCCESS", "COMPLETED"):
                artifact_id = res.get("artifact_id")
                # If the summary is substantial and no artifact exists, optionally store it
                summary_text = res.get("summary", "")
                if not artifact_id and len(summary_text) > 1000 and context.artifact_store:
                    meta = context.artifact_store.store_text_artifact(
                        conversation_id=context.conversation_id,
                        tool_call_id=node.node_id,
                        tool_name="subagent_summary",
                        content_str=summary_text
                    )
                    artifact_id = meta.artifact_id
                    res["artifact_id"] = artifact_id

                return NodeExecutionResult(
                    state=NodeState.COMPLETED,
                    result_payload=res,
                    artifact_id=artifact_id
                )
            elif status == "CANCELLED":
                return NodeExecutionResult(
                    state=NodeState.CANCELLED,
                    result_payload=res,
                    error_message=res.get("error", "Subagent was cancelled.")
                )
            else:
                return NodeExecutionResult(
                    state=NodeState.FAILED,
                    result_payload=res,
                    error_message=res.get("error", f"Subagent failed with status '{status}'.")
                )

        except Exception as e:
            logger.error(f"Error in subagent adapter node '{node.node_id}': {e}", exc_info=True)
            return NodeExecutionResult(
                state=NodeState.FAILED,
                error_message=str(e)
            )
