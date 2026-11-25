"""Task Planning Service Façade
Main operational entry point coordinating request classification, decomposition planning,
ledger persistence, workflow bridge execution, and recovery reconciliation.
"""

import logging
from typing import Dict, Any, List, Optional, Tuple

from runtime.task_planning.models import TaskPlan, PlanStatus, ComplexityCategory
from runtime.task_planning.complexity import ComplexityClassifier
from runtime.task_planning.planner import TaskDecompositionPlanner
from runtime.task_planning.ledger import TaskExecutionLedger
from runtime.task_planning.bridge import WorkflowExecutionBridge
from runtime.task_planning.recovery import TaskPlanRecoveryManager
from runtime.workflows.engine import WorkflowEngine
from runtime.agent_executor import CancellationToken

logger = logging.getLogger("presence.task_planning.service")


class TaskPlanningService:
    """High-level service coordinating the Task Decomposition & Execution Ledger (TDEL)."""

    def __init__(
        self,
        workflow_engine: WorkflowEngine,
        persistence: Optional[Any] = None,
        artifact_store: Optional[Any] = None,
        task_registry: Optional[Any] = None,
        event_bus: Optional[Any] = None,
        model_provider: Optional[Any] = None,
        local_supervisor: Optional[Any] = None,
        desktop_controller: Optional[Any] = None,
    ):
        self.workflow_engine = workflow_engine
        self.persistence = persistence
        self.artifact_store = artifact_store
        self.task_registry = task_registry
        self.event_bus = event_bus
        self.local_supervisor = local_supervisor
        self.desktop_controller = desktop_controller

        if local_supervisor and hasattr(workflow_engine, "local_supervisor") and not workflow_engine.local_supervisor:
            workflow_engine.local_supervisor = local_supervisor
        if desktop_controller and hasattr(workflow_engine, "desktop_controller") and not workflow_engine.desktop_controller:
            workflow_engine.desktop_controller = desktop_controller

        self.classifier = ComplexityClassifier()
        self.planner = TaskDecompositionPlanner(model_provider=model_provider)
        self.ledger = TaskExecutionLedger(
            persistence=persistence,
            artifact_store=artifact_store,
            task_registry=task_registry,
            event_bus=event_bus,
        )
        self.bridge = WorkflowExecutionBridge(
            workflow_engine=workflow_engine,
            ledger=self.ledger,
            artifact_store=artifact_store,
        )
        self.recovery = TaskPlanRecoveryManager(
            ledger=self.ledger,
            persistence=persistence,
            artifact_store=artifact_store,
        )

    def evaluate_and_execute(
        self,
        user_prompt: str,
        conversation_id: str,
        cancellation_token: Optional[CancellationToken] = None,
        force_plan: bool = False,
        execute_dag: bool = True,
    ) -> Dict[str, Any]:
        """Classifies prompt complexity, constructs plan if needed, and executes via workflow bridge."""
        complexity = self.classifier.classify(user_prompt)

        # 1. Bypass planning for SIMPLE requests unless explicitly forced
        if not force_plan and not self.classifier.requires_decomposition(complexity):
            logger.info(
                f"Routing conversation turn directly in '{conversation_id}' (complexity={complexity.value}, execute_dag={execute_dag})."
            )
            return {
                "handled_by_workflow": False,
                "complexity": complexity.value,
                "reason": "Request is conversational or single-step; routing to standard turn.",
            }

        # 2. Generate structured TaskPlan
        logger.info(f"Generating TaskPlan for '{complexity.value}' request in conversation '{conversation_id}'.")
        plan = self.planner.plan(user_prompt, conversation_id, complexity=complexity)

        # 2b. Handle provider unavailable state
        if plan.status == PlanStatus.PLAN_UNAVAILABLE:
            self.ledger.create_plan(plan)
            return {
                "handled_by_workflow": False,
                "status": "PLAN_UNAVAILABLE",
                "plan_id": plan.plan_id,
                "plan_status": PlanStatus.PLAN_UNAVAILABLE.value,
                "complexity": complexity.value,
                "reason": "Planning provider is unavailable or exhausted, and no deterministic archetype matches this request.",
            }

        # 3. Commit initial plan to authoritative SQLite ledger
        self.ledger.create_plan(plan)

        # 4. If plan-only mode, export plan deliverable without executing DAG
        if not execute_dag:
            logger.info(f"Plan '{plan.plan_id}' generated in plan-only mode. Skipping execution bridge.")
            ledger_art_id = self.ledger.export_ledger_artifact(plan.plan_id, conversation_id)
            return {
                "handled_by_workflow": True,
                "status": "SUCCESS",
                "plan_id": plan.plan_id,
                "plan_status": plan.status.value,
                "complexity": complexity.value,
                "workflow_id": None,
                "duration_seconds": 0.0,
                "final_deliverable_artifact_id": None,
                "ledger_artifact_id": ledger_art_id,
                "nodes_completed": 0,
                "total_nodes": len(plan.nodes),
                "plan_only": True,
            }

        # 5. Execute through WorkflowExecutionBridge
        updated_plan, run, ledger_art_id = self.bridge.execute_plan(
            plan=plan,
            conversation_id=conversation_id,
            cancellation_token=cancellation_token,
        )

        return {
            "handled_by_workflow": True,
            "status": "SUCCESS" if updated_plan.status == PlanStatus.COMPLETED else "FAILED",
            "plan_id": updated_plan.plan_id,
            "plan_status": updated_plan.status.value,
            "complexity": complexity.value,
            "workflow_id": run.run_id,
            "duration_seconds": round(run.duration_seconds, 2),
            "final_deliverable_artifact_id": run.final_artifact_id,
            "ledger_artifact_id": ledger_art_id,
            "nodes_completed": sum(1 for ne in run.node_executions.values() if ne.state.value == "COMPLETED"),
            "total_nodes": len(updated_plan.nodes),
        }

    def create_plan(self, prompt: str, conversation_id: str) -> TaskPlan:
        """Generates, validates, and records a structured TaskPlan without executing immediately."""
        plan = self.planner.plan(prompt, conversation_id=conversation_id)
        if plan.status != PlanStatus.PLAN_UNAVAILABLE:
            self.ledger.create_plan(plan)
        return plan

    def get_plan_status(self, plan_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves plan status, node checklist, and ledger artifact reference."""
        plan = self.ledger.get_plan(plan_id)
        if not plan:
            return None
        return plan.to_dict()

    def list_plans(self, conversation_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        return self.ledger.list_plans(conversation_id=conversation_id, limit=limit)

    def reconcile_startup(self) -> List[Dict[str, Any]]:
        return self.recovery.reconcile_on_startup()
