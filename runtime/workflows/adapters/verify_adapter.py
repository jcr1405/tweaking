"""Verify Execution Adapter for Workflow Nodes
Executes deterministic assertions and gatekeeper checks on upstream outputs.
"""

import os
import re
import json
import logging
from typing import Dict, Any

from runtime.workflows.adapters.base import BaseWorkflowAdapter, NodeExecutionContext, NodeExecutionResult
from runtime.workflows.models import WorkflowNode, NodeState

logger = logging.getLogger("presence.workflows.adapters.verify")


class VerifyWorkflowAdapter(BaseWorkflowAdapter):
    """Evaluates deterministic assertions and condition gates on branch outputs."""

    def execute(self, node: WorkflowNode, context: NodeExecutionContext) -> NodeExecutionResult:
        payload = node.payload or {}
        assertion_type = payload.get("assertion_type", "NON_EMPTY").upper()
        target_node_id = payload.get("target_node_id") or (node.depends_on[0] if node.depends_on else None)

        if context.cancellation_token and context.cancellation_token.is_cancelled:
            return NodeExecutionResult(
                state=NodeState.CANCELLED,
                error_message="Execution cancelled before verification."
            )

        target_ne = context.node_executions.get(target_node_id) if target_node_id else None

        passed = False
        message = ""

        try:
            if assertion_type == "EXIT_CODE_ZERO":
                if not target_ne or not target_ne.result_payload:
                    passed = False
                    message = f"Target node '{target_node_id}' has no execution payload."
                else:
                    exit_code = target_ne.result_payload.get("exit_code")
                    passed = (exit_code == 0)
                    message = f"Exit code is {exit_code} (expected 0)."

            elif assertion_type == "NON_EMPTY":
                if not target_ne:
                    passed = False
                    message = f"Target node '{target_node_id}' not found."
                elif target_ne.artifact_id:
                    passed = True
                    message = f"Target produced non-empty artifact '{target_ne.artifact_id}'."
                elif target_ne.result_payload:
                    passed = bool(target_ne.result_payload)
                    message = f"Target produced payload with {len(target_ne.result_payload)} keys."
                else:
                    passed = False
                    message = "Target produced empty result."

            elif assertion_type == "JSON_FIELD_EQUALS":
                field_name = payload.get("field")
                expected_val = payload.get("expected")
                if not target_ne or not target_ne.result_payload:
                    passed = False
                    message = f"Target node '{target_node_id}' has no result payload."
                else:
                    actual_val = target_ne.result_payload.get(field_name)
                    passed = (actual_val == expected_val)
                    message = f"Field '{field_name}' is '{actual_val}' (expected '{expected_val}')."

            elif assertion_type == "REGEX_MATCH":
                pattern = payload.get("pattern", "")
                text_to_check = ""
                if target_ne and target_ne.result_payload:
                    text_to_check = str(target_ne.result_payload)
                match = re.search(pattern, text_to_check)
                passed = (match is not None)
                message = f"Pattern '{pattern}' {'matched' if passed else 'did not match'} target payload."

            elif assertion_type == "FILE_EXISTS":
                raw_path = payload.get("path", "")
                path = context.resolve_template_variables(raw_path)
                passed = os.path.exists(path)
                message = f"File '{path}' {'exists' if passed else 'does not exist'} on disk."

            else:
                passed = False
                message = f"Unknown assertion_type '{assertion_type}'."

        except Exception as e:
            passed = False
            message = f"Error evaluating verification: {e}"

        logger.info(f"Verify node '{node.node_id}' assertion {assertion_type}: {'PASSED' if passed else 'FAILED'} - {message}")

        if passed:
            return NodeExecutionResult(
                state=NodeState.COMPLETED,
                result_payload={"assertion": assertion_type, "passed": True, "message": message}
            )
        else:
            return NodeExecutionResult(
                state=NodeState.FAILED,
                result_payload={"assertion": assertion_type, "passed": False, "message": message},
                error_message=f"Verification failed: {message}"
            )
