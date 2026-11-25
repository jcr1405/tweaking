"""Unit Tests for Phase 1: Task Decomposition Planner & Complexity Classifier
Verifies:
1. Deterministic complexity classification (SIMPLE, MULTI_STEP, COMPLEX, LONG_RUNNING, RESEARCH_HEAVY).
2. TaskPlan data models, serialization, and deserialization roundtrips.
3. TaskPlanValidator graph invariants (Kahn's cycle detection, phase barriers, limits).
4. TaskDecompositionPlanner generation for archetype prompts.
"""

import unittest
from runtime.task_planning.models import (
    ComplexityCategory,
    PlanStatus,
    TaskPlan,
    TaskPlanNode,
)
from runtime.task_planning.complexity import ComplexityClassifier
from runtime.task_planning.validator import TaskPlanValidator, TaskPlanValidationError
from runtime.task_planning.planner import TaskDecompositionPlanner
from runtime.workflows.models import ExecutionType, FailurePolicy


class TestTaskPlanningPhase1(unittest.TestCase):

    def test_complexity_classifier_simple_prompts(self):
        """Verifies conversational and single-step prompts are classified as SIMPLE without decomposition."""
        simple_prompts = [
            "hello there!",
            "what is the current time?",
            "explain what a python dataclass is",
            "can you explain how git worktrees work?",
            "thanks for your help!",
            "status",
        ]
        for p in simple_prompts:
            cat = ComplexityClassifier.classify(p)
            self.assertEqual(cat, ComplexityCategory.SIMPLE, f"Expected SIMPLE for: '{p}'")
            self.assertFalse(ComplexityClassifier.requires_decomposition(cat))

    def test_complexity_classifier_complex_and_multistep_prompts(self):
        """Verifies compound, audit, long-running, and research prompts trigger decomposition."""
        # 1. Complex MCP & App prompt
        prompt_mcp = (
            "so hey could u check internet git hub repos for these mcps and add them for these apps "
            "chrome, davinci, github, discord, gmail, outlook, blender, and check my device for most used apps "
            "and bring them to my notice and make a report with all the mcps together"
        )
        cat_mcp = ComplexityClassifier.classify(prompt_mcp)
        self.assertIn(cat_mcp, (ComplexityCategory.COMPLEX, ComplexityCategory.RESEARCH_HEAVY))
        self.assertTrue(ComplexityClassifier.requires_decomposition(cat_mcp))

        # 2. Long running build prompt
        prompt_build = "install dependencies and run test suite for the repo"
        cat_build = ComplexityClassifier.classify(prompt_build)
        self.assertEqual(cat_build, ComplexityCategory.LONG_RUNNING)
        self.assertTrue(ComplexityClassifier.requires_decomposition(cat_build))

        # 3. Chained multi-step prompt
        prompt_chain = "inspect the config and then check device and bring them to my notice"
        cat_chain = ComplexityClassifier.classify(prompt_chain)
        self.assertIn(cat_chain, (ComplexityCategory.COMPLEX, ComplexityCategory.MULTI_STEP))
        self.assertTrue(ComplexityClassifier.requires_decomposition(cat_chain))

    def test_task_plan_serialization_roundtrip(self):
        """Verifies TaskPlan to_dict and from_dict integrity."""
        node1 = TaskPlanNode(
            node_id="n1",
            title="Step 1",
            description="First step",
            execution_type=ExecutionType.PROCESS,
            payload={"command": "dir"},
            phase=1,
            completion_criteria={"type": "EXIT_CODE_ZERO"},
        )
        node2 = TaskPlanNode(
            node_id="n2",
            title="Step 2",
            description="Second step",
            execution_type=ExecutionType.TOOL,
            payload={"tool_name": "test.echo"},
            depends_on=["n1"],
            phase=2,
            completion_criteria={"type": "NON_EMPTY"},
        )
        plan = TaskPlan(
            plan_id="plan_test_01",
            title="Test Plan",
            user_goal="Goal test",
            description="A test plan description",
            complexity=ComplexityCategory.COMPLEX,
            originating_conversation_id="conv_01",
            nodes=[node1, node2],
            failure_policy=FailurePolicy.CONTINUE_INDEPENDENT,
            assumptions=["assumption 1"],
            constraints=["constraint 1"],
            expected_deliverables=["report.md"],
        )

        d = plan.to_dict()
        reconstructed = TaskPlan.from_dict(d)

        self.assertEqual(reconstructed.plan_id, "plan_test_01")
        self.assertEqual(len(reconstructed.nodes), 2)
        self.assertEqual(reconstructed.nodes[0].node_id, "n1")
        self.assertEqual(reconstructed.nodes[1].depends_on, ["n1"])
        self.assertEqual(reconstructed.nodes[1].execution_type, ExecutionType.TOOL)
        self.assertEqual(reconstructed.complexity, ComplexityCategory.COMPLEX)
        self.assertEqual(reconstructed.failure_policy, FailurePolicy.CONTINUE_INDEPENDENT)

    def test_validator_cycle_detection(self):
        """Verifies TaskPlanValidator detects and rejects cyclic dependencies."""
        node_a = TaskPlanNode(node_id="a", title="A", description="", execution_type=ExecutionType.TOOL, depends_on=["b"], phase=1)
        node_b = TaskPlanNode(node_id="b", title="B", description="", execution_type=ExecutionType.TOOL, depends_on=["a"], phase=1)
        plan = TaskPlan(
            plan_id="plan_cyclic",
            title="Cyclic",
            user_goal="Test",
            description="",
            complexity=ComplexityCategory.COMPLEX,
            originating_conversation_id="conv_1",
            nodes=[node_a, node_b],
        )

        with self.assertRaises(TaskPlanValidationError) as ctx:
            TaskPlanValidator.validate(plan)
        self.assertIn("Cyclic dependency", str(ctx.exception))

    def test_validator_phase_barrier_enforcement(self):
        """Verifies TaskPlanValidator rejects dependencies that cross phase barriers backwards."""
        node1 = TaskPlanNode(node_id="n1", title="N1", description="", execution_type=ExecutionType.TOOL, phase=1, depends_on=["n2"])
        node2 = TaskPlanNode(node_id="n2", title="N2", description="", execution_type=ExecutionType.TOOL, phase=2)
        plan = TaskPlan(
            plan_id="plan_phase_err",
            title="Phase Error",
            user_goal="Test",
            description="",
            complexity=ComplexityCategory.COMPLEX,
            originating_conversation_id="conv_1",
            nodes=[node1, node2],
        )

        with self.assertRaises(TaskPlanValidationError) as ctx:
            TaskPlanValidator.validate(plan)
        self.assertIn("Phase barrier violation", str(ctx.exception))

    def test_validator_dangling_dependency(self):
        """Verifies TaskPlanValidator rejects references to non-existent nodes."""
        node1 = TaskPlanNode(node_id="n1", title="N1", description="", execution_type=ExecutionType.TOOL, phase=1, depends_on=["missing_node"])
        plan = TaskPlan(
            plan_id="plan_dangling",
            title="Dangling",
            user_goal="Test",
            description="",
            complexity=ComplexityCategory.COMPLEX,
            originating_conversation_id="conv_1",
            nodes=[node1],
        )

        with self.assertRaises(TaskPlanValidationError) as ctx:
            TaskPlanValidator.validate(plan)
        self.assertIn("non-existent node 'missing_node'", str(ctx.exception))

    def test_archetype_planner_generation(self):
        """Verifies TaskDecompositionPlanner generates a valid DAG for user MCP request."""
        planner = TaskDecompositionPlanner()
        prompt = (
            "so hey could u check internet git hub repos for these mcps and add them for these apps "
            "chrome, davinci, github, discord, gmail, outlook, blender, and check my device for most used apps "
            "and bring them to my notice and make a report with all the mcps together"
        )
        plan = planner.plan(prompt, conversation_id="conv_mcp_01")

        self.assertIsNotNone(plan)
        self.assertEqual(plan.complexity, ComplexityCategory.COMPLEX)
        self.assertGreaterEqual(len(plan.nodes), 4)

        # First node should inspect local apps
        self.assertEqual(plan.nodes[0].node_id, "inspect_local_apps")
        self.assertEqual(plan.nodes[0].execution_type, ExecutionType.PROCESS)

        # Last node should be AGGREGATE
        last_node = plan.nodes[-1]
        self.assertEqual(last_node.execution_type, ExecutionType.AGGREGATE)
        self.assertIn("inspect_local_apps", last_node.payload.get("source_nodes", []))

        # Validate DAG
        is_val, err = TaskPlanValidator.is_valid(plan)
        self.assertTrue(is_val, f"Generated archetype plan was invalid: {err}")


if __name__ == "__main__":
    unittest.main()
