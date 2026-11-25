"""Presence Workflow DAG Validator
Validates acyclic dependency graphs, duplicate identifiers, dangling references,
phase barriers, and type-specific execution payload schemas using Kahn's algorithm.
"""

from collections import deque
from typing import Dict, List, Set, Tuple, Any

from runtime.workflows.models import (
    WorkflowDefinition,
    WorkflowNode,
    ExecutionType,
)


class WorkflowValidationError(Exception):
    """Raised when a workflow definition fails structural or semantic validation."""
    pass


class WorkflowValidator:
    """Performs static DAG validation and structural verification on WorkflowDefinitions."""

    @classmethod
    def validate(cls, definition: WorkflowDefinition) -> List[str]:
        """Validates a workflow definition. Returns topological order of node IDs if valid.
        
        Raises:
            WorkflowValidationError: If definition contains cycles, dangling dependencies,
            duplicate IDs, or malformed node payloads.
        """
        if not definition.workflow_id or not isinstance(definition.workflow_id, str):
            raise WorkflowValidationError("Workflow must have a non-empty string 'workflow_id'.")

        if not definition.nodes:
            raise WorkflowValidationError(f"Workflow '{definition.workflow_id}' must define at least one node.")

        if definition.max_parallel_nodes < 1 or definition.max_parallel_nodes > 20:
            raise WorkflowValidationError(
                f"Invalid max_parallel_nodes ({definition.max_parallel_nodes}). Must be between 1 and 20."
            )

        # 1. Uniqueness check
        node_map: Dict[str, WorkflowNode] = {}
        for node in definition.nodes:
            if not node.node_id or not isinstance(node.node_id, str):
                raise WorkflowValidationError("All nodes must have a non-empty string 'node_id'.")
            if node.node_id in node_map:
                raise WorkflowValidationError(
                    f"Duplicate node_id '{node.node_id}' detected in workflow '{definition.workflow_id}'."
                )
            node_map[node.node_id] = node

        # 2. Dangling and self-dependency checks
        for node in definition.nodes:
            if node.node_id in node.depends_on:
                raise WorkflowValidationError(
                    f"Node '{node.node_id}' cannot depend on itself (self-dependency detected)."
                )
            for dep_id in node.depends_on:
                if dep_id not in node_map:
                    raise WorkflowValidationError(
                        f"Dangling dependency: Node '{node.node_id}' depends on non-existent node '{dep_id}'."
                    )
                # Phase consistency check: dependency cannot belong to a strictly later phase
                dep_node = node_map[dep_id]
                if dep_node.phase > node.phase:
                    raise WorkflowValidationError(
                        f"Phase barrier violation: Node '{node.node_id}' (Phase {node.phase}) "
                        f"cannot depend on node '{dep_id}' which belongs to later Phase {dep_node.phase}."
                    )

            # 3. Execution type & payload schema checks
            cls._validate_node_payload(node)

        # 4. Kahn's Algorithm for Topological Sort & Cycle Detection
        in_degree: Dict[str, int] = {nid: 0 for nid in node_map}
        adjacency: Dict[str, List[str]] = {nid: [] for nid in node_map}

        for node in definition.nodes:
            for dep_id in node.depends_on:
                adjacency[dep_id].append(node.node_id)
                in_degree[node.node_id] += 1

        queue: deque[str] = deque([nid for nid, deg in in_degree.items() if deg == 0])
        topological_order: List[str] = []

        while queue:
            curr = queue.popleft()
            topological_order.append(curr)

            for neighbor in adjacency[curr]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(topological_order) != len(node_map):
            cycle_nodes = [nid for nid, deg in in_degree.items() if deg > 0]
            raise WorkflowValidationError(
                f"Cyclic dependency detected in workflow '{definition.workflow_id}'. "
                f"Involved nodes: {cycle_nodes}."
            )

        return topological_order

    @classmethod
    def _validate_node_payload(cls, node: WorkflowNode) -> None:
        """Validates that node payload conforms to its specified ExecutionType."""
        payload = node.payload or {}
        etype = node.execution_type

        if etype == ExecutionType.TOOL:
            if "tool_name" not in payload or not isinstance(payload["tool_name"], str):
                raise WorkflowValidationError(
                    f"TOOL node '{node.node_id}' requires string 'tool_name' in payload."
                )
            if "args" in payload and not isinstance(payload["args"], dict):
                raise WorkflowValidationError(
                    f"TOOL node '{node.node_id}' field 'args' must be a dictionary."
                )
            if payload.get("tool_name") == "presence.delegate_local_work":
                raw_args = payload.get("args", {})
                from runtime.tools.delegation_tools import validate_delegation_arguments
                try:
                    validate_delegation_arguments(raw_args)
                except Exception as val_err:
                    raise WorkflowValidationError(
                        f"Argument schema validation failed for 'presence.delegate_local_work' in node '{node.node_id}': {val_err}"
                    )

        elif etype == ExecutionType.SUBAGENT:
            if "goal" not in payload or not isinstance(payload["goal"], str):
                raise WorkflowValidationError(
                    f"SUBAGENT node '{node.node_id}' requires string 'goal' in payload."
                )
            if "role" in payload and not isinstance(payload["role"], str):
                raise WorkflowValidationError(
                    f"SUBAGENT node '{node.node_id}' field 'role' must be a string."
                )

        elif etype == ExecutionType.PROCESS:
            if "command" not in payload or not isinstance(payload["command"], str):
                raise WorkflowValidationError(
                    f"PROCESS node '{node.node_id}' requires string 'command' in payload."
                )

        elif etype == ExecutionType.AGGREGATE:
            if "source_nodes" in payload and not isinstance(payload["source_nodes"], list):
                raise WorkflowValidationError(
                    f"AGGREGATE node '{node.node_id}' field 'source_nodes' must be a list of node IDs."
                )

        elif etype == ExecutionType.VERIFY:
            if "assertion_type" not in payload or not isinstance(payload["assertion_type"], str):
                # Also accept assertions list if provided
                if "assertions" not in payload:
                    raise WorkflowValidationError(
                        f"VERIFY node '{node.node_id}' requires string 'assertion_type' or 'assertions' list in payload."
                    )

        elif etype == ExecutionType.LOCAL_OPERATION:
            if "objective" in payload and not isinstance(payload["objective"], str):
                raise WorkflowValidationError(
                    f"LOCAL_OPERATION node '{node.node_id}' field 'objective' must be a string."
                )

        elif etype == ExecutionType.DESKTOP_OPERATION:
            if "action_type" in payload and not isinstance(payload["action_type"], str):
                raise WorkflowValidationError(
                    f"DESKTOP_OPERATION node '{node.node_id}' field 'action_type' must be a string."
                )
