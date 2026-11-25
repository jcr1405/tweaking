"""Process Execution Adapter for Workflow Nodes
Executes OS CLI commands under Windows Job Object supervision via ProcessManager.
"""

import time
import logging
from typing import Dict, Any

from runtime.workflows.adapters.base import BaseWorkflowAdapter, NodeExecutionContext, NodeExecutionResult
from runtime.workflows.models import WorkflowNode, NodeState

logger = logging.getLogger("presence.workflows.adapters.process")


class ProcessWorkflowAdapter(BaseWorkflowAdapter):
    """Executes a subprocess under Windows Job Object governance."""

    def execute(self, node: WorkflowNode, context: NodeExecutionContext) -> NodeExecutionResult:
        if not context.process_manager:
            return NodeExecutionResult(
                state=NodeState.FAILED,
                error_message="ProcessManager not available in NodeExecutionContext."
            )

        payload = node.payload or {}
        raw_cmd = payload.get("command", "")
        cmd = context.resolve_template_variables(raw_cmd)
        cwd = payload.get("cwd")
        if cwd:
            cwd = context.resolve_template_variables(cwd)

        timeout = node.timeout_seconds or payload.get("timeout_seconds", 60.0)
        is_daemon = bool(payload.get("is_daemon", False))

        if context.cancellation_token and context.cancellation_token.is_cancelled:
            return NodeExecutionResult(
                state=NodeState.CANCELLED,
                error_message="Execution cancelled before process launch."
            )

        try:
            logger.info(f"Executing workflow process node '{node.node_id}': {cmd[:60]}")
            # Launch command with initial synchronous wait
            res = context.process_manager.run_command(
                command=cmd,
                cwd=cwd,
                timeout_seconds=timeout,
                wait_ms=int(min(5000, timeout * 1000)),
                is_daemon=is_daemon,
                conversation_id=context.conversation_id
            )

            task_id = res.get("task_id")

            # If backgrounded, poll until terminal or timeout/cancellation
            status = res.get("status")
            if status == "BACKGROUNDED" and task_id:
                start_poll = time.time()
                poll_interval = 0.5
                while True:
                    if context.cancellation_token and context.cancellation_token.is_cancelled:
                        context.process_manager.kill_process(task_id, reason="Workflow cancelled")
                        return NodeExecutionResult(
                            state=NodeState.CANCELLED,
                            error_message="Process cancelled by workflow cancellation token."
                        )

                    st = context.process_manager.get_process_status(task_id)
                    if not st:
                        break

                    proc_state = st.get("state")
                    if proc_state in ("COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED"):
                        res = st
                        status = proc_state
                        break

                    if (time.time() - start_poll) > timeout:
                        context.process_manager.kill_process(task_id, reason="Workflow node timeout exceeded")
                        return NodeExecutionResult(
                            state=NodeState.FAILED,
                            result_payload=st,
                            error_message=f"Process exceeded timeout limit of {timeout}s."
                        )

                    time.sleep(poll_interval)

            exit_code = res.get("exit_code")
            stdout_art = res.get("stdout_artifact_id")

            if status in ("SUCCESS", "COMPLETED") and exit_code == 0:
                return NodeExecutionResult(
                    state=NodeState.COMPLETED,
                    result_payload=res,
                    artifact_id=stdout_art
                )
            elif status == "CANCELLED":
                return NodeExecutionResult(
                    state=NodeState.CANCELLED,
                    result_payload=res,
                    error_message=res.get("error", "Process was cancelled.")
                )
            else:
                err_msg = res.get("error") or res.get("stderr") or f"Process failed with exit code {exit_code}."
                return NodeExecutionResult(
                    state=NodeState.FAILED,
                    result_payload=res,
                    artifact_id=stdout_art,
                    error_message=str(err_msg)
                )

        except Exception as e:
            logger.error(f"Error in process adapter node '{node.node_id}': {e}", exc_info=True)
            return NodeExecutionResult(
                state=NodeState.FAILED,
                error_message=str(e)
            )
