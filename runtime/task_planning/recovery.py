"""Task Plan Interruption Recovery Subsystem
Reconciles task plans across unexpected shutdowns, preserves completed work,
and isolates ambiguous side-effecting operations.
"""

import time
import logging
from typing import Dict, Any, List, Optional, Tuple

from runtime.task_planning.models import TaskPlan, PlanStatus
from runtime.task_planning.ledger import TaskExecutionLedger
from runtime.task_planning.projection import LedgerProjectionRenderer
from runtime.workflows.models import ExecutionType

logger = logging.getLogger("presence.task_planning.recovery")


class TaskPlanRecoveryManager:
    """Manages the reconciliation and resumption of interrupted task plans."""

    def __init__(
        self,
        ledger: TaskExecutionLedger,
        persistence: Optional[Any] = None,
        artifact_store: Optional[Any] = None,
    ):
        self.ledger = ledger
        self.persistence = persistence
        self.artifact_store = artifact_store

    def reconcile_on_startup(self) -> List[Dict[str, Any]]:
        """Scans for unclosed plans from previous crashed sessions and safely reconciles them."""
        if not self.persistence:
            return []

        # Ensure interrupted workflows are also reconciled
        try:
            self.persistence.reconcile_interrupted_workflows()
        except Exception:
            pass

        interrupted_plan_ids = self.persistence.reconcile_interrupted_plans()
        reconciled_reports: List[Dict[str, Any]] = []

        for pid in interrupted_plan_ids:
            plan = self.ledger.get_plan(pid)
            if not plan:
                continue

            # Inspect workflow nodes associated with this plan
            workflow_id = plan.workflow_id or f"wf_{plan.plan_id}"
            wf_rec = self.persistence.get_workflow_run(workflow_id)

            node_states: Dict[str, Dict[str, Any]] = {}
            safe_to_resume_nodes = []
            uncertain_nodes = []

            if wf_rec and wf_rec.get("nodes"):
                for nd in wf_rec["nodes"]:
                    node_id = nd["node_id"]
                    st = nd["state"]
                    exec_type = nd.get("execution_type")
                    eff_state = st
                    if st in ("RUNNING", "INTERRUPTED"):
                        eff_state = "OUTCOME_UNKNOWN" if exec_type in ("PROCESS", "SUBAGENT") else "INTERRUPTED"

                    node_states[node_id] = {
                        "state": eff_state,
                        "duration_seconds": nd.get("duration_seconds"),
                        "artifact_id": nd.get("artifact_id"),
                        "error": nd.get("error_message") or "Interrupted by runtime restart.",
                    }
                    if st == "COMPLETED":
                        continue
                    elif st in ("RUNNING", "INTERRUPTED"):
                        # If process or external write, mark uncertain
                        if exec_type in ("PROCESS", "SUBAGENT"):
                            uncertain_nodes.append(node_id)
                        else:
                            safe_to_resume_nodes.append(node_id)
                    elif st in ("PENDING", "READY", "WAITING_DEPENDENCIES"):
                        safe_to_resume_nodes.append(node_id)

            # Regenerate Markdown projection
            if self.artifact_store:
                try:
                    plan.status = PlanStatus.INTERRUPTED
                    md = LedgerProjectionRenderer.render_markdown(plan, node_states)
                    meta = self.artifact_store.store_text_artifact(
                        conversation_id=plan.originating_conversation_id,
                        tool_call_id=plan.plan_id,
                        tool_name="presence.task_ledger_recovered",
                        content_str=md,
                    )
                    plan.ledger_artifact_id = meta.artifact_id
                    self.ledger.update_plan_status(
                        plan_id=plan.plan_id,
                        status=PlanStatus.INTERRUPTED,
                        ledger_artifact_id=meta.artifact_id,
                    )
                except Exception as e:
                    logger.debug(f"Error rebuilding ledger projection during recovery: {e}")

            report = {
                "plan_id": plan.plan_id,
                "title": plan.title,
                "status": "INTERRUPTED",
                "completed_nodes": [nid for nid, ns in node_states.items() if ns.get("state") == "COMPLETED"],
                "safe_to_resume": safe_to_resume_nodes,
                "uncertain_outcomes": uncertain_nodes,
            }
            reconciled_reports.append(report)
            logger.info(f"Reconciled task plan '{plan.plan_id}': {len(report['completed_nodes'])} completed, {len(safe_to_resume_nodes)} safe to resume.")

        return reconciled_reports
