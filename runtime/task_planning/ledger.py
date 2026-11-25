"""Task Execution Ledger (TDEL)
Authoritative SQLite-backed record of execution plans, progress tracking,
and event synchronization.
"""

import json
import time
import uuid
import hashlib
import logging
from typing import Dict, Any, List, Optional

from runtime.task_planning.models import TaskPlan, TaskPlanNode, PlanStatus, ComplexityCategory
from runtime.task_planning.validator import TaskPlanValidator
from runtime.workflows.models import ExecutionType, FailurePolicy
from runtime.observability.models import TaskType, TaskState

logger = logging.getLogger("presence.task_planning.ledger")


class TaskExecutionLedger:
    """Manages the lifecycle, persistence, and state synchronization of TaskPlans."""

    def __init__(
        self,
        persistence: Optional[Any] = None,
        artifact_store: Optional[Any] = None,
        task_registry: Optional[Any] = None,
        event_bus: Optional[Any] = None,
    ):
        self.persistence = persistence
        self.artifact_store = artifact_store
        self.task_registry = task_registry
        self.event_bus = event_bus

        # Register event listeners if event bus available
        if self.event_bus:
            self._register_event_handlers()

    def _register_event_handlers(self) -> None:
        """Subscribes to runtime workflow and task events to update ledger state."""
        try:
            self.event_bus.subscribe("WORKFLOW_STARTED", self._on_workflow_event)
            self.event_bus.subscribe("WORKFLOW_NODE_STARTED", self._on_workflow_event)
            self.event_bus.subscribe("WORKFLOW_NODE_COMPLETED", self._on_workflow_event)
            self.event_bus.subscribe("WORKFLOW_NODE_FAILED", self._on_workflow_event)
            self.event_bus.subscribe("WORKFLOW_COMPLETED", self._on_workflow_event)
            self.event_bus.subscribe("WORKFLOW_FAILED", self._on_workflow_event)
        except Exception as e:
            logger.debug(f"Failed to subscribe ledger to event bus: {e}")

    def create_plan(self, plan: TaskPlan) -> TaskPlan:
        """Validates, hashes, and commits a new TaskPlan to authoritative SQLite storage."""
        TaskPlanValidator.validate(plan)

        # 1. Compute definition hash for immutable provenance
        plan_dict = plan.to_dict()
        canonical_str = json.dumps(plan_dict, sort_keys=True)
        definition_hash = hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()

        # 2. Persist to SQLite
        if self.persistence:
            self.persistence.save_task_plan(
                plan_id=plan.plan_id,
                conversation_id=plan.originating_conversation_id,
                title=plan.title,
                goal=plan.user_goal,
                complexity=plan.complexity.value,
                plan_version=plan.plan_version,
                definition_hash=definition_hash,
                status=plan.status.value,
                created_at=plan.created_at,
                updated_at=plan.updated_at,
                plan_json=json.dumps(plan_dict),
                workflow_id=plan.workflow_id,
                completed_at=plan.completed_at,
                ledger_artifact_id=plan.ledger_artifact_id,
                metadata_json=json.dumps(plan.metadata) if plan.metadata else None,
            )

        # 3. Register root task in ActiveTaskRegistry
        if self.task_registry:
            try:
                root_t = self.task_registry.register_task(
                    task_type=TaskType.WORKFLOW,
                    display_name=f"Plan: {plan.title}",
                    conversation_id=plan.originating_conversation_id,
                    cancellable=True,
                    metadata={"plan_id": plan.plan_id, "version": plan.plan_version}
                )
                self.task_registry.start_task(root_t.task_id)
            except Exception as e:
                logger.debug(f"Error registering task plan root in registry: {e}")

        # 4. Emit PLAN_CREATED event
        if self.event_bus:
            self.event_bus.publish("PLAN_CREATED", {
                "plan_id": plan.plan_id,
                "title": plan.title,
                "conversation_id": plan.originating_conversation_id,
                "node_count": len(plan.nodes),
            })

        logger.info(f"TaskPlan '{plan.plan_id}' (v{plan.plan_version}) committed to ledger.")
        return plan

    def get_plan(self, plan_id: str) -> Optional[TaskPlan]:
        """Retrieves and deserializes a TaskPlan by plan_id."""
        if not self.persistence:
            return None
        rec = self.persistence.get_task_plan(plan_id)
        if not rec or not rec.get("plan_data"):
            return None
        plan = TaskPlan.from_dict(rec["plan_data"])
        # Override with authoritative database column values
        if rec.get("status"):
            plan.status = PlanStatus(rec["status"])
        if rec.get("workflow_id"):
            plan.workflow_id = rec["workflow_id"]
        if rec.get("ledger_artifact_id"):
            plan.ledger_artifact_id = rec["ledger_artifact_id"]
        if rec.get("completed_at") is not None:
            plan.completed_at = rec["completed_at"]
        if rec.get("updated_at") is not None:
            plan.updated_at = rec["updated_at"]
        return plan

    def update_plan_status(
        self,
        plan_id: str,
        status: PlanStatus,
        workflow_id: Optional[str] = None,
        ledger_artifact_id: Optional[str] = None,
    ) -> None:
        """Updates the status and artifact reference of an active plan."""
        now = time.time()
        completed_at = now if status.is_terminal else None
        if self.persistence:
            self.persistence.update_task_plan_status(
                plan_id=plan_id,
                status=status.value,
                updated_at=now,
                completed_at=completed_at,
                workflow_id=workflow_id,
                ledger_artifact_id=ledger_artifact_id,
            )

        if self.event_bus:
            self.event_bus.publish("PLAN_STATUS_CHANGED", {
                "plan_id": plan_id,
                "status": status.value,
                "timestamp": now,
            })

    def list_plans(self, conversation_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Lists task plans recorded in the ledger."""
        if self.persistence:
            return self.persistence.list_task_plans(conversation_id=conversation_id, limit=limit)
        return []

    def create_plan_revision(
        self,
        original_plan_id: str,
        revised_nodes: List[TaskPlanNode],
        revision_title: Optional[str] = None,
    ) -> TaskPlan:
        """Creates a new immutable revision of an existing plan, preserving history."""
        original = self.get_plan(original_plan_id)
        if not original:
            raise ValueError(f"Original plan '{original_plan_id}' not found.")

        new_version = original.plan_version + 1
        new_plan_id = f"plan_{uuid.uuid4().hex[:8]}"

        revised_plan = TaskPlan(
            plan_id=new_plan_id,
            title=revision_title or f"{original.title} (v{new_version})",
            user_goal=original.user_goal,
            description=original.description,
            complexity=original.complexity,
            originating_conversation_id=original.originating_conversation_id,
            nodes=revised_nodes,
            plan_version=new_version,
            status=PlanStatus.DRAFT,
            failure_policy=original.failure_policy,
            max_parallel_nodes=original.max_parallel_nodes,
            assumptions=list(original.assumptions),
            constraints=list(original.constraints),
            expected_deliverables=list(original.expected_deliverables),
            metadata={"supersedes_plan_id": original_plan_id},
        )

        return self.create_plan(revised_plan)

    def _on_workflow_event(self, event: Any) -> None:
        """Internal callback responding to workflow engine lifecycle events."""
        payload = event.payload if hasattr(event, "payload") else {}
        event_type = event.event_type if hasattr(event, "event_type") else str(event)

        run_id = payload.get("run_id")
        if not run_id:
            return

        # Find plan associated with this run/workflow
        # If terminal workflow event, finalize plan
        if event_type == "WORKFLOW_COMPLETED":
            logger.debug(f"Workflow {run_id} completed. Synchronizing ledger...")
        elif event_type == "WORKFLOW_FAILED":
            logger.debug(f"Workflow {run_id} failed. Synchronizing ledger...")
