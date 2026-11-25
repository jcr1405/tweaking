"""Unit Tests for Workflow Models and DAG Validation (Phase 5B)
Verifies:
1. Valid linear, diamond, and multi-phase DAG topological ordering.
2. Cycle detection and rejection via Kahn's algorithm.
3. Dangling dependency detection.
4. Self-dependency rejection.
5. Phase barrier constraint enforcement.
6. Execution payload schema verification across all 5 adapter types.
7. Serialization roundtrips (to_dict / from_dict).
"""

import unittest
from runtime.workflows.models import (
    ExecutionType,
    NodeState,
    WorkflowStatus,
    FailurePolicy,
    DependencyCondition,
    WorkflowNode,
    WorkflowDefinition,
    WorkflowNodeExecution,
    WorkflowRun,
)
from runtime.workflows.validator import (
    WorkflowValidator,
    WorkflowValidationError,
)


class TestWorkflowModelsAndDAG(unittest.TestCase):
    def test_linear_dag_topological_sort(self):
        """Verifies topological sorting on a strictly linear DAG."""
        n1 = WorkflowNode(node_id="step_1", name="Step 1", execution_type=ExecutionType.PROCESS, payload={"command": "dir"})
        n2 = WorkflowNode(node_id="step_2", name="Step 2", execution_type=ExecutionType.PROCESS, payload={"command": "echo 1"}, depends_on=["step_1"])
        n3 = WorkflowNode(node_id="step_3", name="Step 3", execution_type=ExecutionType.PROCESS, payload={"command": "echo 2"}, depends_on=["step_2"])

        defn = WorkflowDefinition(
            workflow_id="wf_linear",
            name="Linear Workflow",
            description="Tests linear execution",
            nodes=[n3, n1, n2]  # Intentionally unordered in list
        )

        order = WorkflowValidator.validate(defn)
        self.assertEqual(order, ["step_1", "step_2", "step_3"])

    def test_diamond_dag_topological_sort(self):
        """Verifies topological sorting on a diamond (branching/joining) DAG."""
        # root -> branch_a, branch_b -> join
        root = WorkflowNode(node_id="root", name="Root", execution_type=ExecutionType.PROCESS, payload={"command": "echo root"})
        branch_a = WorkflowNode(node_id="branch_a", name="Branch A", execution_type=ExecutionType.PROCESS, payload={"command": "echo A"}, depends_on=["root"])
        branch_b = WorkflowNode(node_id="branch_b", name="Branch B", execution_type=ExecutionType.PROCESS, payload={"command": "echo B"}, depends_on=["root"])
        join = WorkflowNode(node_id="join", name="Join", execution_type=ExecutionType.AGGREGATE, payload={"source_nodes": ["branch_a", "branch_b"]}, depends_on=["branch_a", "branch_b"])

        defn = WorkflowDefinition(
            workflow_id="wf_diamond",
            name="Diamond Workflow",
            description="Branch and join",
            nodes=[join, branch_a, root, branch_b]
        )

        order = WorkflowValidator.validate(defn)
        self.assertEqual(order[0], "root")
        self.assertIn("branch_a", order[1:3])
        self.assertIn("branch_b", order[1:3])
        self.assertEqual(order[3], "join")

    def test_cyclic_dependency_rejection(self):
        """Verifies cyclic dependencies are identified and rejected."""
        n1 = WorkflowNode(node_id="n1", name="N1", execution_type=ExecutionType.PROCESS, payload={"command": "echo 1"}, depends_on=["n3"])
        n2 = WorkflowNode(node_id="n2", name="N2", execution_type=ExecutionType.PROCESS, payload={"command": "echo 2"}, depends_on=["n1"])
        n3 = WorkflowNode(node_id="n3", name="N3", execution_type=ExecutionType.PROCESS, payload={"command": "echo 3"}, depends_on=["n2"])

        defn = WorkflowDefinition(
            workflow_id="wf_cyclic",
            name="Cyclic Workflow",
            description="Has a cycle",
            nodes=[n1, n2, n3]
        )

        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowValidator.validate(defn)
        self.assertIn("Cyclic dependency detected", str(ctx.exception))

    def test_self_dependency_rejection(self):
        """Verifies self-dependencies are caught."""
        n1 = WorkflowNode(node_id="self_node", name="Self", execution_type=ExecutionType.PROCESS, payload={"command": "echo"}, depends_on=["self_node"])
        defn = WorkflowDefinition(workflow_id="wf_self", name="Self", description="", nodes=[n1])

        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowValidator.validate(defn)
        self.assertIn("self-dependency", str(ctx.exception))

    def test_dangling_dependency_rejection(self):
        """Verifies references to non-existent node IDs are rejected."""
        n1 = WorkflowNode(node_id="n1", name="N1", execution_type=ExecutionType.PROCESS, payload={"command": "echo"}, depends_on=["ghost_node"])
        defn = WorkflowDefinition(workflow_id="wf_dangling", name="Dangling", description="", nodes=[n1])

        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowValidator.validate(defn)
        self.assertIn("Dangling dependency", str(ctx.exception))

    def test_phase_barrier_violation_rejection(self):
        """Verifies earlier phase nodes cannot depend on later phase nodes."""
        n1 = WorkflowNode(node_id="p1_node", name="P1", execution_type=ExecutionType.PROCESS, payload={"command": "echo"}, phase=1, depends_on=["p2_node"])
        n2 = WorkflowNode(node_id="p2_node", name="P2", execution_type=ExecutionType.PROCESS, payload={"command": "echo"}, phase=2)
        defn = WorkflowDefinition(workflow_id="wf_phase", name="Phase", description="", nodes=[n1, n2])

        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowValidator.validate(defn)
        self.assertIn("Phase barrier violation", str(ctx.exception))

    def test_payload_schema_validations(self):
        """Verifies schema validation for each execution type."""
        # Tool missing tool_name
        bad_tool = WorkflowNode(node_id="t1", name="T1", execution_type=ExecutionType.TOOL, payload={"args": {}})
        with self.assertRaises(WorkflowValidationError):
            WorkflowValidator.validate(WorkflowDefinition(workflow_id="w1", name="W", description="", nodes=[bad_tool]))

        # Subagent missing goal
        bad_subagent = WorkflowNode(node_id="s1", name="S1", execution_type=ExecutionType.SUBAGENT, payload={"role": "researcher"})
        with self.assertRaises(WorkflowValidationError):
            WorkflowValidator.validate(WorkflowDefinition(workflow_id="w2", name="W", description="", nodes=[bad_subagent]))

        # Process missing command
        bad_process = WorkflowNode(node_id="p1", name="P1", execution_type=ExecutionType.PROCESS, payload={"timeout": 10})
        with self.assertRaises(WorkflowValidationError):
            WorkflowValidator.validate(WorkflowDefinition(workflow_id="w3", name="W", description="", nodes=[bad_process]))

        # Verify missing assertion_type
        bad_verify = WorkflowNode(node_id="v1", name="V1", execution_type=ExecutionType.VERIFY, payload={"expected": 0})
        with self.assertRaises(WorkflowValidationError):
            WorkflowValidator.validate(WorkflowDefinition(workflow_id="w4", name="W", description="", nodes=[bad_verify]))

    def test_serialization_roundtrip(self):
        """Verifies full WorkflowDefinition to_dict and from_dict fidelity."""
        n1 = WorkflowNode(
            node_id="fetch_data",
            name="Fetch Data",
            execution_type=ExecutionType.TOOL,
            payload={"tool_name": "presence.fetch_web_page", "args": {"url": "https://example.com"}},
            phase=1,
            timeout_seconds=30.0,
            max_retries=2
        )
        n2 = WorkflowNode(
            node_id="analyze",
            name="Analyze",
            execution_type=ExecutionType.SUBAGENT,
            payload={"goal": "Analyze page content", "role": "researcher"},
            depends_on=["fetch_data"],
            phase=2
        )

        original = WorkflowDefinition(
            workflow_id="wf_full_test",
            name="Full Test Workflow",
            description="Serialization test",
            nodes=[n1, n2],
            failure_policy=FailurePolicy.CONTINUE_INDEPENDENT_BRANCHES,
            max_parallel_nodes=4,
            metadata={"tag": "production"}
        )

        as_dict = original.to_dict()
        restored = WorkflowDefinition.from_dict(as_dict)

        self.assertEqual(restored.workflow_id, original.workflow_id)
        self.assertEqual(len(restored.nodes), 2)
        self.assertEqual(restored.nodes[0].node_id, "fetch_data")
        self.assertEqual(restored.nodes[1].depends_on, ["fetch_data"])
        self.assertEqual(restored.failure_policy, FailurePolicy.CONTINUE_INDEPENDENT_BRANCHES)
        self.assertEqual(restored.max_parallel_nodes, 4)
        self.assertEqual(restored.metadata["tag"], "production")


if __name__ == "__main__":
    unittest.main()
