"""Presence Workflow Deterministic Scheduler
Tracks node dependency resolution, phase barriers, concurrency caps, and failure propagation.
"""

import time
import logging
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

from runtime.workflows.models import (
    WorkflowDefinition,
    WorkflowNode,
    WorkflowNodeExecution,
    NodeState,
    WorkflowStatus,
    FailurePolicy,
    DependencyCondition,
)

logger = logging.getLogger("presence.workflows.scheduler")


class WorkflowScheduler:
    """Coordinates DAG state transitions and dependency resolution for a single workflow run."""

    def __init__(self, definition: WorkflowDefinition, run_id: str):
        self.definition = definition
        self.run_id = run_id
        self._lock = threading.RLock()

        self.node_map: Dict[str, WorkflowNode] = {n.node_id: n for n in definition.nodes}
        self.node_executions: Dict[str, WorkflowNodeExecution] = {}

        # Initialize all node executions in PENDING state
        for node in definition.nodes:
            self.node_executions[node.node_id] = WorkflowNodeExecution(
                node_id=node.node_id,
                workflow_run_id=run_id,
                state=NodeState.PENDING,
            )

        self._cancelled = False
        self._has_fatal_failure = False

    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def request_cancellation(self) -> None:
        """Flags workflow for cooperative cancellation and marks unstarted nodes cancelled."""
        with self._lock:
            self._cancelled = True
            for nid, ne in self.node_executions.items():
                if ne.state in (NodeState.PENDING, NodeState.WAITING_DEPENDENCIES, NodeState.READY):
                    ne.state = NodeState.CANCELLED
                    ne.error_message = "Workflow run was cancelled."
                    ne.end_time = time.time()

    def get_ready_nodes(self) -> List[str]:
        """Returns list of node IDs that are ready for immediate dispatch."""
        with self._lock:
            if self._cancelled:
                return []

            if self._has_fatal_failure and self.definition.failure_policy == FailurePolicy.FAIL_FAST:
                return []

            ready: List[str] = []

            # Determine currently lowest active phase
            lowest_incomplete_phase = min(
                (self.node_map[nid].phase for nid, ne in self.node_executions.items() if not ne.state.is_terminal),
                default=1
            )

            for nid, node in self.node_map.items():
                ne = self.node_executions[nid]

                # Only examine nodes that haven't started or are waiting
                if ne.state not in (NodeState.PENDING, NodeState.WAITING_DEPENDENCIES):
                    continue

                # 1. Phase Barrier Check: Can only run if in the lowest incomplete phase
                if node.phase > lowest_incomplete_phase:
                    ne.state = NodeState.WAITING_DEPENDENCIES
                    continue

                # 2. Dependency Resolution Check
                is_ready, should_skip, skip_reason = self._evaluate_dependencies(node)

                if should_skip:
                    ne.state = NodeState.SKIPPED
                    ne.error_message = skip_reason
                    ne.end_time = time.time()
                    logger.info(f"Node '{nid}' SKIPPED: {skip_reason}")
                    continue

                if is_ready:
                    ne.state = NodeState.READY
                    ready.append(nid)
                else:
                    ne.state = NodeState.WAITING_DEPENDENCIES

            return ready

    def _evaluate_dependencies(self, node: WorkflowNode) -> Tuple[bool, bool, Optional[str]]:
        """Evaluates upstream dependency nodes.
        Returns: (is_ready: bool, should_skip: bool, skip_reason: Optional[str])
        """
        if not node.depends_on:
            return True, False, None

        # Check if all dependencies are terminal
        dep_executions = [self.node_executions[dep_id] for dep_id in node.depends_on]
        all_terminal = all(d.state.is_terminal for d in dep_executions)

        if not all_terminal:
            return False, False, None

        # All upstream dependencies are terminal. Now apply DependencyCondition
        if node.dependency_condition == DependencyCondition.ALL_SUCCESS:
            failed_deps = [d.node_id for d in dep_executions if d.state != NodeState.COMPLETED]
            if failed_deps:
                return False, True, f"Required upstream dependencies failed or skipped: {failed_deps}"
            return True, False, None

        elif node.dependency_condition == DependencyCondition.ANY_SUCCESS:
            any_success = any(d.state == NodeState.COMPLETED for d in dep_executions)
            if not any_success:
                return False, True, f"None of the alternative dependencies succeeded: {node.depends_on}"
            return True, False, None

        elif node.dependency_condition == DependencyCondition.OPTIONAL:
            return True, False, None

        return False, False, None

    def mark_running(self, node_id: str, task_id: Optional[str] = None) -> None:
        with self._lock:
            ne = self.node_executions[node_id]
            ne.state = NodeState.RUNNING
            ne.task_id = task_id
            ne.start_time = time.time()

    def mark_completed(
        self,
        node_id: str,
        result_payload: Optional[Dict[str, Any]] = None,
        artifact_id: Optional[str] = None,
    ) -> None:
        with self._lock:
            ne = self.node_executions[node_id]
            ne.state = NodeState.COMPLETED
            ne.end_time = time.time()
            ne.duration_seconds = max(0.0, ne.end_time - (ne.start_time or ne.end_time))
            ne.result_payload = result_payload
            ne.artifact_id = artifact_id
            logger.info(f"Node '{node_id}' COMPLETED in {ne.duration_seconds:.2f}s (Artifact: {artifact_id})")

    def mark_failed(
        self,
        node_id: str,
        error_message: str,
        result_payload: Optional[Dict[str, Any]] = None,
        artifact_id: Optional[str] = None,
    ) -> None:
        with self._lock:
            node = self.node_map[node_id]
            ne = self.node_executions[node_id]
            ne.state = NodeState.FAILED
            ne.end_time = time.time()
            ne.duration_seconds = max(0.0, ne.end_time - (ne.start_time or ne.end_time))
            ne.error_message = error_message
            ne.result_payload = result_payload
            ne.artifact_id = artifact_id
            logger.warning(f"Node '{node_id}' FAILED: {error_message}")

            if not node.optional:
                self._has_fatal_failure = True
                if self.definition.failure_policy == FailurePolicy.FAIL_FAST:
                    # Cancel all pending/waiting nodes
                    for other_id, other_ne in self.node_executions.items():
                        if other_ne.state in (NodeState.PENDING, NodeState.WAITING_DEPENDENCIES, NodeState.READY):
                            other_ne.state = NodeState.CANCELLED
                            other_ne.error_message = f"Aborted due to failure in node '{node_id}'."
                            other_ne.end_time = time.time()

    def mark_waiting_approval(
        self,
        node_id: str,
        error_message: str,
        result_payload: Optional[Dict[str, Any]] = None,
        artifact_id: Optional[str] = None,
    ) -> None:
        with self._lock:
            ne = self.node_executions[node_id]
            ne.state = NodeState.WAITING_FOR_APPROVAL
            ne.end_time = time.time()
            ne.duration_seconds = max(0.0, ne.end_time - (ne.start_time or ne.end_time))
            ne.error_message = error_message
            ne.result_payload = result_payload
            ne.artifact_id = artifact_id
            logger.info(f"Node '{node_id}' WAITING_FOR_APPROVAL: {error_message}")

    def resume_node_approval(self, node_id: str, consent_token: str) -> None:
        """Resumes a node that was in WAITING_FOR_APPROVAL state with an action-bound consent token."""
        with self._lock:
            ne = self.node_executions[node_id]
            node = self.node_map[node_id]
            node.payload = dict(node.payload or {})
            node.payload["consent_token"] = consent_token
            ne.state = NodeState.PENDING
            ne.error_message = None

    def cancel_node_approval(self, node_id: str, reason: str = "User rejected consent") -> None:
        """Cancels a node awaiting approval when user rejects consent."""
        with self._lock:
            ne = self.node_executions[node_id]
            ne.state = NodeState.CANCELLED
            ne.error_message = reason
            ne.end_time = time.time()
            self._cancelled = True
            if not self.node_map[node_id].optional and self.definition.failure_policy == FailurePolicy.FAIL_FAST:
                self._has_fatal_failure = True

    def is_workflow_terminal(self) -> bool:
        """Returns True if every node in the workflow has reached a terminal state."""
        with self._lock:
            return all(ne.state.is_terminal for ne in self.node_executions.values())

    def get_final_status(self) -> WorkflowStatus:
        """Calculates aggregate workflow status from node execution states."""
        with self._lock:
            if self._cancelled or any(ne.state == NodeState.CANCELLED for ne in self.node_executions.values()):
                return WorkflowStatus.CANCELLED

            if any(ne.state == NodeState.WAITING_FOR_APPROVAL for ne in self.node_executions.values()):
                return WorkflowStatus.AWAITING_APPROVAL

            has_failure = any(
                ne.state == NodeState.FAILED and not self.node_map[nid].optional
                for nid, ne in self.node_executions.items()
            )
            if has_failure:
                return WorkflowStatus.FAILED

            return WorkflowStatus.COMPLETED
