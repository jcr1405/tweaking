"""Workflow Execution Bridge
Converts TaskPlan definitions into executable Phase 5 WorkflowDefinitions
and coordinates execution through WorkflowEngine, updating the authoritative ledger.
"""

import time
import logging
from typing import Tuple, Dict, Any, Optional

from runtime.task_planning.models import TaskPlan, TaskPlanNode, PlanStatus
from runtime.task_planning.ledger import TaskExecutionLedger
from runtime.task_planning.projection import LedgerProjectionRenderer
from runtime.workflows.models import (
    WorkflowDefinition,
    WorkflowNode,
    WorkflowRun,
    WorkflowStatus,
    NodeState,
    ExecutionType,
)
from runtime.workflows.validator import WorkflowValidationError
from runtime.workflows.engine import WorkflowEngine
from runtime.agent_executor import CancellationToken

logger = logging.getLogger("presence.task_planning.bridge")


class WorkflowExecutionBridge:
    """Bridges high-level TaskPlans with the deterministic WorkflowEngine runtime."""

    def __init__(
        self,
        workflow_engine: WorkflowEngine,
        ledger: TaskExecutionLedger,
        artifact_store: Optional[Any] = None,
    ):
        self.workflow_engine = workflow_engine
        self.ledger = ledger
        self.artifact_store = artifact_store

    def convert_plan_to_workflow(self, plan: TaskPlan) -> WorkflowDefinition:
        """Transforms a validated TaskPlan into an immutable WorkflowDefinition."""
        wf_nodes: list[WorkflowNode] = []
        for n in plan.nodes:
            payload = dict(n.payload)
            etype = n.execution_type

            # Defensive payload normalization to guarantee WorkflowValidator acceptance
            if etype == ExecutionType.TOOL and ("tool_name" not in payload or not isinstance(payload["tool_name"], str)):
                alt_name = payload.get("action") or payload.get("name") or payload.get("tool")
                if alt_name and isinstance(alt_name, str) and ("." in alt_name or "_" in alt_name):
                    payload["tool_name"] = alt_name
                    payload.setdefault("args", {})
                else:
                    etype = ExecutionType.LOCAL_OPERATION
                    payload = {"objective": n.description or str(alt_name or n.title or "Perform operation")}

            # Strict validation for presence.delegate_local_work (fail-closed, no authority overrides, no extra args)
            if etype == ExecutionType.TOOL and payload.get("tool_name") == "presence.delegate_local_work":
                raw_args = payload.get("args")
                if not isinstance(raw_args, dict):
                    raise WorkflowValidationError(
                        f"TOOL node '{n.node_id}' ('presence.delegate_local_work') requires a dictionary 'args' payload."
                    )
                from runtime.tools.delegation_tools import validate_delegation_arguments
                try:
                    validate_delegation_arguments(raw_args)
                except Exception as val_err:
                    raise WorkflowValidationError(
                        f"Argument schema validation failed for 'presence.delegate_local_work' in node '{n.node_id}': {val_err}"
                    )

            elif etype == ExecutionType.PROCESS and ("command" not in payload or not isinstance(payload["command"], str)):
                alt_cmd = payload.get("command") or payload.get("action") or payload.get("script")
                if alt_cmd and isinstance(alt_cmd, str):
                    payload["command"] = f"powershell -Command \"{alt_cmd}\""
                else:
                    etype = ExecutionType.LOCAL_OPERATION
                    payload = {"objective": n.description or n.title or "Run process operation"}

            elif etype == ExecutionType.SUBAGENT and ("goal" not in payload or not isinstance(payload["goal"], str)):
                payload["goal"] = n.description or n.title or "Subagent goal"
                if "role" not in payload:
                    payload["role"] = "Specialist"

            elif etype == ExecutionType.VERIFY and "assertion_type" not in payload and "assertions" not in payload:
                payload["assertion_type"] = "NON_EMPTY"

            elif etype == ExecutionType.AGGREGATE and ("source_nodes" not in payload or not isinstance(payload["source_nodes"], list)):
                payload["source_nodes"] = [prev.node_id for prev in plan.nodes if prev.node_id != n.node_id]
                if "title" not in payload:
                    payload["title"] = n.title or "Synthesized Deliverable"

            wf_nodes.append(WorkflowNode(
                node_id=n.node_id,
                name=n.title,
                execution_type=etype,
                payload=payload,
                depends_on=list(n.depends_on),
                phase=n.phase,
                optional=n.optional,
                timeout_seconds=n.timeout_seconds,
            ))

        return WorkflowDefinition(
            workflow_id=f"wf_{plan.plan_id}",
            name=plan.title,
            nodes=wf_nodes,
            description=plan.description,
            failure_policy=plan.failure_policy,
            max_parallel_nodes=plan.max_parallel_nodes,
            metadata={"plan_id": plan.plan_id, "plan_version": plan.plan_version},
        )

    def execute_plan(
        self,
        plan: TaskPlan,
        conversation_id: str,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> Tuple[TaskPlan, WorkflowRun, Optional[str]]:
        """Executes a TaskPlan through the Phase 5 WorkflowEngine with ledger synchronization."""
        # 1. Update plan state to IN_PROGRESS
        plan.status = PlanStatus.IN_PROGRESS
        self.ledger.update_plan_status(plan.plan_id, PlanStatus.IN_PROGRESS)

        # 2. Convert to WorkflowDefinition
        workflow_def = self.convert_plan_to_workflow(plan)

        # 3. Initial Markdown projection
        ledger_art_id = self._update_projection(plan, {})
        plan.ledger_artifact_id = ledger_art_id

        # 4. Dispatch workflow execution
        logger.info(f"Dispatching TaskPlan '{plan.plan_id}' through WorkflowEngine ({len(workflow_def.nodes)} nodes).")
        run = self.workflow_engine.execute_workflow(
            definition=workflow_def,
            conversation_id=conversation_id,
            cancellation_token=cancellation_token,
        )

        plan.workflow_id = run.run_id

        # 5. Extract completed node states
        node_states: Dict[str, Dict[str, Any]] = {}
        for nid, ne in run.node_executions.items():
            node_states[nid] = {
                "state": ne.state.value,
                "duration_seconds": ne.duration_seconds,
                "artifact_id": ne.artifact_id,
                "error": ne.error_message,
            }

        # 6. Map final status
        if run.status == WorkflowStatus.COMPLETED:
            plan.status = PlanStatus.COMPLETED
            plan.completed_at = time.time()
        elif run.status == WorkflowStatus.CANCELLED:
            plan.status = PlanStatus.CANCELLED
            plan.completed_at = time.time()
        elif run.status == WorkflowStatus.AWAITING_APPROVAL:
            plan.status = PlanStatus.AWAITING_APPROVAL
            plan.completed_at = None
        else:
            plan.status = PlanStatus.FAILED
            plan.completed_at = time.time()

        # 7. Refresh final Markdown projection with actual node metrics
        final_ledger_art = self._update_projection(plan, node_states)
        plan.ledger_artifact_id = final_ledger_art

        # 8. Commit final status to ledger
        self.ledger.update_plan_status(
            plan_id=plan.plan_id,
            status=plan.status,
            workflow_id=run.run_id,
            ledger_artifact_id=final_ledger_art,
        )

        logger.info(
            f"TaskPlan '{plan.plan_id}' completed with status {plan.status.value} "
            f"(Workflow: {run.run_id}, Deliverable: {run.final_artifact_id})"
        )

        return plan, run, final_ledger_art

    def _update_projection(
        self,
        plan: TaskPlan,
        node_states: Dict[str, Dict[str, Any]],
    ) -> Optional[str]:
        """Renders and persists the TASK_LEDGER.md projection artifact."""
        if not self.artifact_store:
            return None

        try:
            md_text = LedgerProjectionRenderer.render_markdown(plan, node_states)
            meta = self.artifact_store.store_text_artifact(
                conversation_id=plan.originating_conversation_id,
                tool_call_id=plan.plan_id,
                tool_name="presence.task_ledger",
                content_str=md_text,
            )
            return meta.artifact_id
        except Exception as e:
            logger.error(f"Failed to persist ledger projection artifact: {e}")
            return None
