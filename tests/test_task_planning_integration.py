"""Integration and End-to-End Tests for Task Decomposition & Execution Ledger (TDEL)
Verifies:
1. End-to-end evaluation, planning, DAG conversion, execution, and ledger artifact persistence.
2. Markdown task ledger projection rendering and live progress checkmarks.
3. Plan revisioning (immutability of previous versions).
4. Startup interruption reconciliation of crashed plans.
5. Plan management agent tools (presence.manage_plan and presence.create_plan).
"""

import time
import shutil
import tempfile
import unittest
from pathlib import Path

from runtime.task_planning.models import TaskPlan, TaskPlanNode, PlanStatus, ComplexityCategory
from runtime.task_planning.service import TaskPlanningService
from runtime.task_planning.projection import LedgerProjectionRenderer
from runtime.tools.plan_tools import build_plan_tools
from runtime.workflows.models import ExecutionType, FailurePolicy
from runtime.workflows.engine import WorkflowEngine
from runtime.tools.registry import ToolRegistry, ToolDefinition
from runtime.permissions import PermissionBoundary, PermissionTier
from runtime.persistence import PersistenceManager
from runtime.artifacts.store import ArtifactStore
from runtime.observability.task_registry import ActiveTaskRegistry
from runtime.events import EventBus


class TestTaskPlanningIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_tdel_"))
        self.db_path = self.temp_dir / "presence_test.db"
        self.artifact_dir = self.temp_dir / "artifacts"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)

        self.persistence = PersistenceManager(db_path=self.db_path)
        self.artifact_store = ArtifactStore(root_dir=self.artifact_dir)
        self.task_registry = ActiveTaskRegistry()
        self.event_bus = EventBus()
        self.permissions = PermissionBoundary()
        self.tool_registry = ToolRegistry()

        # Register sample echo and search tools
        def _echo_tool(args):
            return {"status": "SUCCESS", "output": f"Found: {args.get('query', '')}"}

        self.tool_registry.register(
            ToolDefinition(
                tool_id="presence.web_search",
                name="presence.web_search",
                description="Search tool",
                parameters={"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}},
                authority_tier=PermissionTier.L0_OBSERVE,
                handler=_echo_tool,
            )
        )

        from runtime.process_manager import ProcessManager
        self.process_manager = ProcessManager(
            base_tasks_dir=self.temp_dir / "tasks",
            event_bus=self.event_bus,
            task_registry=self.task_registry,
            permission_boundary=self.permissions,
        )

        self.workflow_engine = WorkflowEngine(
            persistence=self.persistence,
            artifact_store=self.artifact_store,
            tool_registry=self.tool_registry,
            process_manager=self.process_manager,
            subagent_manager=None,
            task_registry=self.task_registry,
            event_bus=self.event_bus,
            permission_boundary=self.permissions,
        )

        self.service = TaskPlanningService(
            workflow_engine=self.workflow_engine,
            persistence=self.persistence,
            artifact_store=self.artifact_store,
            task_registry=self.task_registry,
            event_bus=self.event_bus,
        )

        self.conv_id = "test_conv_tdel_01"

    def tearDown(self):
        self.persistence.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_end_to_end_evaluation_and_workflow_execution(self):
        """Verifies end-to-end prompt decomposition, workflow bridge execution, and ledger artifact persistence."""
        prompt = (
            "so hey could u check internet git hub repos for these mcps and add them for these apps "
            "chrome, davinci, blender, discord, and make a report with all the mcps together"
        )

        res = self.service.evaluate_and_execute(prompt, conversation_id=self.conv_id)

        self.assertTrue(res["handled_by_workflow"])
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["plan_status"], "COMPLETED")
        self.assertIn(res["complexity"], ("COMPLEX", "MULTI_STEP", "RESEARCH_HEAVY"))
        self.assertGreaterEqual(res["nodes_completed"], 4)

        # Verify ledger artifact was created and is readable
        ledger_art_id = res["ledger_artifact_id"]
        self.assertIsNotNone(ledger_art_id)

        art = self.artifact_store.read_artifact(self.conv_id, ledger_art_id, mode="lines", limit=5000)
        self.assertIn("# Task Ledger:", art.content)
        self.assertIn("[x]", art.content)  # Completed checkmarks
        self.assertIn("Desktop Applications & MCP Discovery Audit", art.content)

        # Verify plan record in SQLite persistence
        persisted_plan = self.persistence.get_task_plan(res["plan_id"])
        self.assertIsNotNone(persisted_plan)
        self.assertEqual(persisted_plan["status"], "COMPLETED")
        self.assertEqual(persisted_plan["workflow_id"], res["workflow_id"])

    def test_markdown_projection_rendering(self):
        """Verifies LedgerProjectionRenderer constructs expected markdown formatting and icons."""
        node1 = TaskPlanNode(node_id="n1", title="Inspect local state", description="", execution_type=ExecutionType.PROCESS, phase=1)
        node2 = TaskPlanNode(node_id="n2", title="Query API", description="", execution_type=ExecutionType.TOOL, depends_on=["n1"], phase=2)
        plan = TaskPlan(
            plan_id="plan_md_test",
            title="Markdown Test Plan",
            user_goal="Test Goal",
            description="Testing markdown rendering",
            complexity=ComplexityCategory.COMPLEX,
            originating_conversation_id="c1",
            nodes=[node1, node2],
            expected_deliverables=["output_report.md"],
        )

        states = {
            "n1": {"state": "COMPLETED", "duration_seconds": 0.42, "artifact_id": "art_123"},
            "n2": {"state": "IN_PROGRESS"},
        }

        md = LedgerProjectionRenderer.render_markdown(plan, states)

        self.assertIn("# Task Ledger: Markdown Test Plan", md)
        self.assertIn("1/2 (50%)", md)
        self.assertIn("- [x] **Inspect local state** (0.42s)", md)
        self.assertIn("- [/] **Query API**", md)
        self.assertIn("art_123", md)
        self.assertIn("output_report.md", md)

    def test_plan_revisioning_preserves_immutable_history(self):
        """Verifies creating a plan revision increments plan_version and does not mutate the original."""
        node1 = TaskPlanNode(node_id="step1", title="Original Step", description="", execution_type=ExecutionType.TOOL)
        original_plan = TaskPlan(
            plan_id="plan_v1",
            title="Deploy Plan",
            user_goal="Deploy app",
            description="",
            complexity=ComplexityCategory.COMPLEX,
            originating_conversation_id="conv_v1",
            nodes=[node1],
            plan_version=1,
        )
        self.service.ledger.create_plan(original_plan)

        # Create revision with an additional step
        revised_node2 = TaskPlanNode(node_id="step2", title="Extra Verification", description="", execution_type=ExecutionType.VERIFY, depends_on=["step1"], phase=2)
        revised_plan = self.service.ledger.create_plan_revision(
            original_plan_id="plan_v1",
            revised_nodes=[node1, revised_node2],
            revision_title="Deploy Plan (Revised)",
        )

        self.assertEqual(revised_plan.plan_version, 2)
        self.assertEqual(len(revised_plan.nodes), 2)
        self.assertEqual(revised_plan.metadata.get("supersedes_plan_id"), "plan_v1")

        # Verify original plan v1 remains in database
        orig_rec = self.service.ledger.get_plan("plan_v1")
        self.assertIsNotNone(orig_rec)
        self.assertEqual(orig_rec.plan_version, 1)
        self.assertEqual(len(orig_rec.nodes), 1)

    def test_startup_interruption_recovery(self):
        """Verifies interrupted plans from simulated crashes are reconciled and reconstructed."""
        crashed_plan_id = "plan_crashed_88"
        node = TaskPlanNode(node_id="in_flight_proc", title="In-Flight Process", description="", execution_type=ExecutionType.PROCESS)
        plan = TaskPlan(
            plan_id=crashed_plan_id,
            title="Crashed Plan",
            user_goal="Audit",
            description="",
            complexity=ComplexityCategory.COMPLEX,
            originating_conversation_id=self.conv_id,
            nodes=[node],
            status=PlanStatus.IN_PROGRESS,
            workflow_id=f"wf_{crashed_plan_id}",
        )
        self.service.ledger.create_plan(plan)

        # Simulate in-flight workflow nodes in persistence
        self.persistence.save_workflow_run(
            run_id=f"wf_{crashed_plan_id}",
            workflow_id="wf_def",
            conversation_id=self.conv_id,
            name="Crashed WF",
            status="RUNNING",
            failure_policy="FAIL_FAST",
            start_time=time.time() - 3600,
        )
        self.persistence.save_workflow_node(
            run_id=f"wf_{crashed_plan_id}",
            node_id="in_flight_proc",
            name="In-Flight Process",
            execution_type="PROCESS",
            phase=1,
            state="RUNNING",
            start_time=time.time() - 3600,
        )

        # Run startup reconciliation
        reports = self.service.reconcile_startup()
        self.assertGreaterEqual(len(reports), 1)

        # Verify plan status transitioned to INTERRUPTED
        updated = self.service.ledger.get_plan(crashed_plan_id)
        self.assertEqual(updated.status, PlanStatus.INTERRUPTED)

        # Verify regenerated ledger artifact exists
        self.assertIsNotNone(updated.ledger_artifact_id)
        art = self.artifact_store.read_artifact(self.conv_id, updated.ledger_artifact_id, mode="lines")
        self.assertIn("[?]", art.content)

    def test_plan_agent_tools_create_and_manage(self):
        """Verifies presence.create_plan and presence.manage_plan agent tools."""
        tools = build_plan_tools(self.service, current_conversation_id=self.conv_id)
        tool_map = {t.name: t for t in tools}

        self.assertIn("create_plan", tool_map)
        self.assertIn("manage_plan", tool_map)

        # 1. create_plan tool
        create_res = tool_map["create_plan"].handler({
            "prompt": "inspect chrome and blender mcps on github and make a report"
        })
        self.assertEqual(create_res["status"], "SUCCESS")
        plan_id = create_res["plan_id"]
        self.assertGreaterEqual(create_res["node_count"], 3)

        # 2. manage_plan: get_status
        status_res = tool_map["manage_plan"].handler({
            "action": "get_status",
            "plan_id": plan_id,
        })
        self.assertEqual(status_res["status"], "SUCCESS")
        self.assertEqual(status_res["plan"]["plan_id"], plan_id)

        # 3. manage_plan: list_plans
        list_res = tool_map["manage_plan"].handler({"action": "list_plans"})
        self.assertEqual(list_res["status"], "SUCCESS")
        self.assertGreaterEqual(list_res["count"], 1)

        # 4. manage_plan: cancel
        cancel_res = tool_map["manage_plan"].handler({
            "action": "cancel",
            "plan_id": plan_id,
        })
        self.assertEqual(cancel_res["status"], "SUCCESS")
        self.assertTrue(cancel_res["cancelled"])

        # Confirm cancellation in ledger
        cancelled_plan = self.service.ledger.get_plan(plan_id)
        self.assertEqual(cancelled_plan.status, PlanStatus.CANCELLED)


if __name__ == "__main__":
    unittest.main()
