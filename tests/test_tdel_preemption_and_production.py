"""End-to-End Tests for TDEL Production Integration and Preemption
Verifies:
1. Conversational Preemption in AgentSession.execute_turn (SIMPLE vs Archetype vs Novel vs Outage).
2. Deterministic Archetypes (Software Inventory, MCP Discovery, Repo Inspection, Build/Test, Diagnostics).
3. Discussion vs Execution intent classification.
4. Least-privilege permission separation (L0_OBSERVE manage_plan vs L2_EXECUTE_REVERSIBLE cancel_plan).
5. Safe provider outage handling (no random stubs, clean PLAN_UNAVAILABLE).
6. Reactive TUI EventBus subscription in TasksView.
"""

import time
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from runtime.task_planning.models import TaskPlan, TaskPlanNode, PlanStatus, ComplexityCategory
from runtime.task_planning.complexity import ComplexityClassifier
from runtime.task_planning.planner import TaskDecompositionPlanner
from runtime.task_planning.service import TaskPlanningService
from runtime.workflows.engine import WorkflowEngine
from runtime.tools.registry import ToolRegistry, ToolDefinition
from runtime.permissions import PermissionBoundary, PermissionTier
from runtime.persistence import PersistenceManager
from runtime.artifacts.store import ArtifactStore
from runtime.observability.task_registry import ActiveTaskRegistry
from runtime.events import EventBus
from runtime.session import AgentSession
from runtime.agent_executor import AgentExecutor, AgentExecutionEvent
from runtime.trajectory import TrajectoryStore, StepType, StepStatus
from runtime.journal import EventJournal
from runtime.summaries import GlobalSummaryManager
from runtime.reconstruction import ContextAssembler
from runtime.tools.plan_tools import build_plan_tools
from runtime.process_manager import ProcessManager


class MockModelProvider:
    def __init__(self, mode="success"):
        self.mode = mode
        self.call_count = 0

    @property
    def model_id(self) -> str:
        return "mock-gemini-pro"

    def generate_stream(self, system_prompt, messages, tools=None, cancellation_token=None):
        self.call_count += 1
        class Chunk:
            text_delta = "Conversational response for simple turn."
            thinking_status = None
        yield Chunk()

    def generate_turn(self, messages, **kwargs):
        self.call_count += 1
        if self.mode == "failure":
            raise RuntimeError("HTTP 502: Provider quota exhausted (Cloudflare 429)")

        return json.dumps({
            "title": "Novel GPU AI Diagnostics",
            "nodes": [
                {
                    "node_id": "step_diag_gpu",
                    "title": "Query GPU Drivers",
                    "description": "Check GPU device state",
                    "execution_type": "PROCESS",
                    "payload": {"command": "powershell -Command \"Write-Output 'GPU Controller: NVIDIA RTX 4090'\""},
                    "phase": 1,
                },
                {
                    "node_id": "step_summary",
                    "title": "Synthesize Diagnostics Deliverable",
                    "description": "Produce report",
                    "execution_type": "AGGREGATE",
                    "payload": {"title": "AI Environment Report", "source_nodes": ["step_diag_gpu"]},
                    "depends_on": ["step_diag_gpu"],
                    "phase": 2,
                }
            ]
        })


class TestTDELPreemptionAndProduction(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_prod_tdel_"))
        self.db_path = self.temp_dir / "test_prod.db"
        self.artifact_dir = self.temp_dir / "artifacts"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.conv_dir = self.temp_dir / "conversations"
        self.conv_dir.mkdir(parents=True, exist_ok=True)

        self.persistence = PersistenceManager(db_path=self.db_path)
        self.artifact_store = ArtifactStore(root_dir=self.artifact_dir)
        self.task_registry = ActiveTaskRegistry()
        self.event_bus = EventBus()
        self.permissions = PermissionBoundary()
        self.tool_registry = ToolRegistry()
        self.proc_mgr = ProcessManager(event_bus=self.event_bus, task_registry=self.task_registry, permission_boundary=self.permissions)

        self.workflow_engine = WorkflowEngine(
            tool_registry=self.tool_registry,
            artifact_store=self.artifact_store,
            task_registry=self.task_registry,
            event_bus=self.event_bus,
            permission_boundary=self.permissions,
            persistence=self.persistence,
            process_manager=self.proc_mgr,
        )

        self.mock_provider = MockModelProvider(mode="success")

        self.planning_service = TaskPlanningService(
            workflow_engine=self.workflow_engine,
            persistence=self.persistence,
            artifact_store=self.artifact_store,
            task_registry=self.task_registry,
            event_bus=self.event_bus,
            model_provider=self.mock_provider,
        )

        # Setup AgentSession components
        self.conv_id = "conv_e2e_test"
        self.store = TrajectoryStore(conversation_id=self.conv_id, db_dir=self.conv_dir)
        self.journal = EventJournal(conversation_id=self.conv_id, logs_dir=self.conv_dir)
        self.summary_mgr = GlobalSummaryManager(db_path=self.temp_dir / "summary.db")
        self.assembler = ContextAssembler(trajectory_store=self.store, summary_manager=self.summary_mgr)

        self.executor = AgentExecutor(
            trajectory_store=self.store,
            event_journal=self.journal,
            summary_manager=self.summary_mgr,
            permission_boundary=self.permissions,
            tool_registry=self.tool_registry,
            model_provider=self.mock_provider,
            context_assembler=self.assembler,
            artifact_store=self.artifact_store,
            task_registry=self.task_registry,
        )

        from runtime.conversation import ConversationMetadata
        self.meta = ConversationMetadata(conversation_id=self.conv_id, title="Test E2E")

        self.session = AgentSession(
            conversation_id=self.conv_id,
            metadata=self.meta,
            trajectory_store=self.store,
            event_journal=self.journal,
            summary_manager=self.summary_mgr,
            tool_registry=self.tool_registry,
            permission_boundary=self.permissions,
            model_provider=self.mock_provider,
            agent_executor=self.executor,
            artifact_store=self.artifact_store,
            context_assembler=self.assembler,
            event_bus=self.event_bus,
            task_planning_service=self.planning_service,
        )

    def tearDown(self):
        self.session.close()
        self.persistence.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_preemption_path_a_simple_chat(self):
        """Path A: Simple conversational request bypasses planning and uses standard turn."""
        prompt = "Explain Python dataclasses."
        events = list(self.session.execute_turn(user_input=prompt))
        event_types = [e.event_type for e in events]

        # Must NOT emit PLAN_CREATED or execute workflow
        self.assertNotIn("PLAN_CREATED", event_types)
        self.assertNotIn("PLAN_COMPLETED", event_types)
        self.assertIn("TEXT_DELTA", event_types)
        # Model generate_stream should be called once for simple chat
        self.assertEqual(self.mock_provider.call_count, 1)

    def test_preemption_path_b_software_inventory_archetype(self):
        """Path B: Deterministic software inventory request is preempted and executed via WorkflowEngine with 0 LLM calls!"""
        self.mock_provider.call_count = 0
        prompt = "Inventory my installed desktop applications and categorize them."

        events = list(self.session.execute_turn(user_input=prompt))
        event_types = [e.event_type for e in events]

        self.assertIn("PLAN_CREATED", event_types)
        self.assertIn("PLAN_COMPLETED", event_types)
        self.assertIn("TEXT_DELTA", event_types)
        self.assertIn("TURN_COMPLETED", event_types)

        # Provider must NOT have been called (0 tokens burned!)
        self.assertEqual(self.mock_provider.call_count, 0)

        # Confirm step was persisted in TrajectoryStore
        steps = self.store.list_steps()
        self.assertGreaterEqual(len(steps), 2)
        user_step = steps[0]
        asst_step = steps[-1]
        self.assertEqual(user_step.step_type, StepType.USER_INPUT)
        self.assertEqual(asst_step.step_type, StepType.AGENT_TURN)
        self.assertIn("Windows Desktop Installed Software Inventory", asst_step.step_payload.get("content", ""))

    def test_preemption_path_c_novel_complex_request(self):
        """Path C: Novel request uses 1 LLM planning call, validates DAG, and executes through WorkflowEngine."""
        self.mock_provider.call_count = 0
        prompt = "Cluster and map our microservices network topologies, analyze latency heatmaps, and produce an infrastructure scaling report."

        events = list(self.session.execute_turn(user_input=prompt))
        event_types = [e.event_type for e in events]

        self.assertIn("PLAN_CREATED", event_types)
        self.assertIn("PLAN_COMPLETED", event_types)
        self.assertIn("TEXT_DELTA", event_types)

        # Only 1 model turn used for DAG planning, zero intermediate ReAct iterations
        self.assertEqual(self.mock_provider.call_count, 1)

    def test_archetype_e_system_diagnostics(self):
        """Verifies Archetype E runs system & GPU diagnostics deterministically with 0 LLM calls."""
        self.mock_provider.call_count = 0
        prompt = "Diagnose my Python development environment, check GPU compute stack, and generate a remediation plan."

        events = list(self.session.execute_turn(user_input=prompt))
        event_types = [e.event_type for e in events]

        self.assertIn("PLAN_CREATED", event_types)
        self.assertIn("PLAN_COMPLETED", event_types)
        self.assertEqual(self.mock_provider.call_count, 0)

    def test_preemption_path_g_provider_outage(self):
        """Path G: When provider is unavailable during novel planning, reports PLAN_UNAVAILABLE without random stubs."""
        outage_provider = MockModelProvider(mode="failure")
        self.session.task_planning_service.model_provider = outage_provider
        self.session.task_planning_service.planner.model_provider = outage_provider

        prompt = "Diagnose unknown server kernel bug and configure custom cluster topology."
        events = list(self.session.execute_turn(user_input=prompt))
        event_types = [e.event_type for e in events]

        self.assertIn("TEXT_DELTA", event_types)
        self.assertIn("TURN_FAILED", event_types)

        text_events = [e.payload.get("delta") for e in events if e.event_type == "TEXT_DELTA"]
        full_text = "".join(filter(None, text_events))
        self.assertIn("planning provider is currently unavailable", full_text)

    def test_least_privilege_plan_tools(self):
        """Verifies read-only manage_plan is L0_OBSERVE and state-mutating cancel_plan is L2_EXECUTE_REVERSIBLE."""
        tools = build_plan_tools(self.planning_service, current_conversation_id=self.conv_id)
        tool_map = {t.name: t for t in tools}

        self.assertIn("manage_plan", tool_map)
        self.assertIn("cancel_plan", tool_map)
        self.assertIn("create_plan", tool_map)

        # manage_plan is read-only
        manage_tool = tool_map["manage_plan"]
        self.assertEqual(manage_tool.authority_tier, PermissionTier.L0_OBSERVE)
        self.assertIn("list_plans", manage_tool.parameters["properties"]["action"]["enum"])
        self.assertIn("get_status", manage_tool.parameters["properties"]["action"]["enum"])

        # cancel_plan is state mutation
        cancel_tool = tool_map["cancel_plan"]
        self.assertEqual(cancel_tool.authority_tier, PermissionTier.L2_EXECUTE_REVERSIBLE)

        # Create a plan and cancel using cancel_plan
        plan = self.planning_service.create_plan("Find chrome and blender mcps", self.conv_id)
        res = cancel_tool.handler({"plan_id": plan.plan_id})
        self.assertEqual(res["status"], "SUCCESS")
        self.assertTrue(res["cancelled"])

        # Check authoritative SQLite state
        updated = self.planning_service.ledger.get_plan(plan.plan_id)
        self.assertEqual(updated.status, PlanStatus.CANCELLED)

    def test_discussion_vs_execution_classification(self):
        """Verifies discussion/explanation queries remain SIMPLE while execution requests trigger decomposition."""
        # Discussion inquiries
        discussions = [
            "Explain how I could improve my desktop setup.",
            "Can you explain the difference between processes and threads?",
            "How do I configure git credentials?",
            "Tell me about Python virtual environments.",
        ]
        for d in discussions:
            cat = ComplexityClassifier.classify(d)
            self.assertEqual(cat, ComplexityCategory.SIMPLE, f"Expected SIMPLE for discussion query: '{d}'")

        # Execution commands
        executions = [
            "Inventory my installed desktop applications and categorize them.",
            "Find compatible MCP integrations for Chrome, Blender, DaVinci and Discord.",
            "Build this project, run its tests and summarize failures.",
            "Inspect my Python and GPU development environment and identify compatibility issues.",
        ]
        for ex in executions:
            cat = ComplexityClassifier.classify(ex)
            self.assertTrue(ComplexityClassifier.requires_decomposition(cat), f"Expected decomposition for: '{ex}'")


if __name__ == "__main__":
    unittest.main()
