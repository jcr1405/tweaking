"""Task Plan Schema and DAG Validator
Validates structural limits, dependency graphs, cycle avoidance, and completion criteria
for generated TaskPlans before committing to SQLite or WorkflowEngine.
"""

from typing import Dict, Any, List, Set, Tuple, Optional
from collections import deque

from runtime.task_planning.models import TaskPlan, TaskPlanNode, PlanStatus
from runtime.workflows.models import ExecutionType


class TaskPlanValidationError(Exception):
    """Raised when a TaskPlan violates structural or dependency invariants."""
    pass


class TaskPlanValidator:
    """Enforces strict structural and graph constraints on TaskPlans."""

    MAX_NODES_PER_PLAN = 30
    MAX_DEPENDENCY_EDGES = 100
    MAX_TITLE_LENGTH = 150
    MAX_DESCRIPTION_LENGTH = 2000

    @classmethod
    def validate(cls, plan: TaskPlan) -> None:
        """Validates all aspects of a TaskPlan, raising TaskPlanValidationError on failure."""
        if plan.status == PlanStatus.PLAN_UNAVAILABLE:
            return

        if not plan.plan_id or not isinstance(plan.plan_id, str):
            raise TaskPlanValidationError("TaskPlan must have a non-empty string 'plan_id'.")

        if not plan.title or not isinstance(plan.title, str):
            raise TaskPlanValidationError("TaskPlan must have a non-empty string 'title'.")

        if len(plan.title) > cls.MAX_TITLE_LENGTH:
            raise TaskPlanValidationError(f"Plan title exceeds maximum length of {cls.MAX_TITLE_LENGTH} chars.")

        if not plan.nodes or not isinstance(plan.nodes, list):
            raise TaskPlanValidationError("TaskPlan must contain a non-empty list of nodes.")

        if len(plan.nodes) > cls.MAX_NODES_PER_PLAN:
            raise TaskPlanValidationError(
                f"TaskPlan exceeds maximum node count of {cls.MAX_NODES_PER_PLAN} (received {len(plan.nodes)})."
            )

        node_ids: Set[str] = set()
        node_phases: Dict[str, int] = {}
        total_edges = 0

        # 1. Individual node checks and uniqueness
        for node in plan.nodes:
            if not isinstance(node, TaskPlanNode):
                raise TaskPlanValidationError(f"Expected TaskPlanNode instance, got {type(node).__name__}.")

            if not node.node_id or not isinstance(node.node_id, str):
                raise TaskPlanValidationError("Each plan node must have a non-empty string 'node_id'.")

            if node.node_id in node_ids:
                raise TaskPlanValidationError(f"Duplicate node_id '{node.node_id}' detected in TaskPlan.")
            node_ids.add(node.node_id)

            if not node.title:
                raise TaskPlanValidationError(f"Node '{node.node_id}' must have a non-empty title.")

            if not isinstance(node.execution_type, ExecutionType):
                raise TaskPlanValidationError(
                    f"Node '{node.node_id}' has invalid execution_type '{node.execution_type}'."
                )

            if node.phase < 1:
                raise TaskPlanValidationError(f"Node '{node.node_id}' phase must be >= 1 (got {node.phase}).")
            node_phases[node.node_id] = node.phase

            total_edges += len(node.depends_on)

        if total_edges > cls.MAX_DEPENDENCY_EDGES:
            raise TaskPlanValidationError(
                f"Total dependency edges ({total_edges}) exceeds limit of {cls.MAX_DEPENDENCY_EDGES}."
            )

        # 2. Dependency integrity and phase barriers
        adjacency: Dict[str, List[str]] = {nid: [] for nid in node_ids}
        in_degree: Dict[str, int] = {nid: 0 for nid in node_ids}

        for node in plan.nodes:
            for dep_id in node.depends_on:
                if dep_id not in node_ids:
                    raise TaskPlanValidationError(
                        f"Node '{node.node_id}' depends on non-existent node '{dep_id}' (dangling reference)."
                    )
                if dep_id == node.node_id:
                    raise TaskPlanValidationError(
                        f"Node '{node.node_id}' has a self-dependency on itself."
                    )
                # Phase barrier check: dependency must have phase <= node.phase
                if node_phases[dep_id] > node.phase:
                    raise TaskPlanValidationError(
                        f"Phase barrier violation: Node '{node.node_id}' (Phase {node.phase}) depends on "
                        f"'{dep_id}' (Phase {node_phases[dep_id]}). Dependencies must belong to phase <= current phase."
                    )

                adjacency[dep_id].append(node.node_id)
                in_degree[node.node_id] += 1

        # 3. Cycle Detection via Kahn's Algorithm
        queue = deque([nid for nid, deg in in_degree.items() if deg == 0])
        visited_count = 0

        while queue:
            curr = queue.popleft()
            visited_count += 1
            for neighbor in adjacency[curr]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if visited_count != len(node_ids):
            unresolved = [nid for nid, deg in in_degree.items() if deg > 0]
            raise TaskPlanValidationError(
                f"Cyclic dependency detected in TaskPlan among nodes: {unresolved}"
            )

    @classmethod
    def is_valid(cls, plan: TaskPlan) -> Tuple[bool, Optional[str]]:
        """Non-throwing validation check returning (is_valid, error_message)."""
        try:
            cls.validate(plan)
            return True, None
        except TaskPlanValidationError as e:
            return False, str(e)
