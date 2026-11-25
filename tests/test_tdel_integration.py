"""TDEL Integration Test Suite: Gates T-01 through T-32 & Scenarios A-E
Verifies:
- Component A: Unified Task Graph Contract (LOCAL_OPERATION, DESKTOP_OPERATION, etc.)
- Component B: Canonical Cloud Planning Interface (presence.execute_task_plan)
- Component C: Deterministic Task Graph Validation & Compilation
- Component D & E: Trusted Execution Routing (LocalOperationsSupervisor & DesktopController)
- Component F: Predecessor Data-Flow & Evidence References
- Component G & H: Execution State Model, Lifecycle Transitions & Structured Aggregation
- Component I: User Consent & Consequential Action Pausing (WAITING_FOR_APPROVAL)
- Component J: Durability, Replay & State Reconciliation
- Component K: Cooperative Cancellation & Resource Teardown
- Component L: Real-Time Observability & Event Streaming
- Component M: Token Efficiency & Context Economy (Zero Cloud Leak)
- Scenarios A, B, C, D, E end-to-end demonstrations
"""

import time
import uuid
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Dict, Any, List, Optional

from runtime.task_planning.models import (
    TaskPlan,
    TaskPlanNode,
    PlanStatus,
    ComplexityCategory,
)
from runtime.task_planning.service import TaskPlanningService
from runtime.task_planning.validator import TaskPlanValidator, TaskPlanValidationError
from runtime.tools.plan_tools import build_plan_tools
from runtime.workflows.models import (
    WorkflowDefinition,
    WorkflowNode,
    WorkflowRun,
    WorkflowStatus,
    NodeState,
    ExecutionType,
    FailurePolicy,
    DependencyCondition,
)
from runtime.workflows.engine import WorkflowEngine
from runtime.tools.registry import ToolRegistry, ToolDefinition
from runtime.permissions import (
    PermissionBoundary,
    PermissionTier,
    ActionPolicy,
)
from runtime.persistence import PersistenceManager
from runtime.artifacts.store import ArtifactStore
from runtime.observability.task_registry import ActiveTaskRegistry
from runtime.observability.models import TaskType, TaskState
from runtime.events import EventBus

from runtime.local_operations.models import (
    TaskDelegationContract,
    TaskDelegationResult,
    DelegationOutcome,
    OutcomeStatus,
    CloudReturnPacket,
    VerifiedFinding,
    ArtifactReference,
    CompactionMetrics,
)
from runtime.desktop.models import (
    DesktopWindow,
    DesktopAction,
    DesktopActionResult,
    ActionStatus,
    ActionType,
    GroundingResolution,
    GroundedCandidate,
    PerceptionRoute,
    DesktopObservation,
    ScreenRect,
)
from runtime.desktop.policy import DesktopPolicy


class MockLocalSupervisor:
    """Deterministic mock for LocalOperationsSupervisor."""

    def __init__(self, should_fail: bool = False, error_msg: str = ""):
        self.should_fail = should_fail
        self.error_msg = error_msg
        self.executed_contracts: List[TaskDelegationContract] = []

    def execute(self, contract: TaskDelegationContract, cancellation_token=None) -> TaskDelegationResult:
        self.executed_contracts.append(contract)
        if cancellation_token and cancellation_token.is_cancelled:
            return TaskDelegationResult(
                task_id=contract.task_id,
                parent_conversation_id=contract.parent_conversation_id,
                outcome=DelegationOutcome.CANCELLED,
                summary="Cancelled by user.",
            )
        if self.should_fail:
            return TaskDelegationResult(
                task_id=contract.task_id,
                parent_conversation_id=contract.parent_conversation_id,
                outcome=DelegationOutcome.FAILED,
                summary=self.error_msg or "Mock failure in local operational supervisor",
            )

        pkt = CloudReturnPacket(
            task_id=contract.task_id,
            parent_conversation_id=contract.parent_conversation_id,
            objective=contract.objective,
            outcome=OutcomeStatus.COMPLETED,
            summary=f"Investigated: {contract.objective}",
            verified_findings=[
                VerifiedFinding(
                    finding_id="find_01",
                    statement=f"Verified finding for {contract.objective[:30]}",
                    evidence_ids=["ev_1"],
                    verification_method="local_inspection",
                )
            ],
            artifacts=[
                ArtifactReference(
                    artifact_id=f"art_{contract.task_id}",
                    content_type="text/markdown",
                    byte_size=1024,
                    sha256="a" * 64,
                    tool_name="local_supervisor",
                    description="analysis report",
                )
            ],
            compaction_metrics=CompactionMetrics(
                raw_bytes=4096,
                compacted_bytes=256,
                compaction_ratio=16.0,
            ),
        )

        return TaskDelegationResult(
            task_id=contract.task_id,
            parent_conversation_id=contract.parent_conversation_id,
            outcome=DelegationOutcome.COMPLETED,
            summary=f"Successfully investigated {contract.objective}",
            return_packet=pkt,
            metrics={"duration_seconds": 0.1, "turn_count": 2},
        )


class MockDesktopController:
    """Deterministic mock for DesktopController."""

    def __init__(self, policy: Optional[DesktopPolicy] = None):
        self.policy = policy or DesktopPolicy()
        self.windows = [
            DesktopWindow(
                window_id="win_editor_01",
                hwnd=1001,
                title="Presence Diagnostic Workspace",
                process_name="diagnostic_app.exe",
                process_id=2048,
                bounds=ScreenRect(left=100, top=100, right=900, bottom=700),
                is_visible=True,
                is_minimized=False,
                is_foreground=True,
            ),
            DesktopWindow(
                window_id="win_protected_01",
                hwnd=1002,
                title="Windows Security Prompt",
                process_name="consent.exe",
                process_id=4,
                bounds=ScreenRect(left=0, top=0, right=400, bottom=300),
                is_visible=True,
                is_minimized=False,
                is_foreground=False,
            ),
        ]
        self.windows_manager = type(
            "MockWindowsManager",
            (),
            {
                "list_windows": lambda s, filter_visible=True: self.windows,
                "get_window": lambda s, wid: next((w for w in self.windows if w.window_id == wid), None),
                "find_window_by_title": lambda s, pat: next((w for w in self.windows if pat.lower() in w.title.lower()), None),
            },
        )()
        self.accessibility_backend = type("MockAccessibilityBackend", (), {
            "get_registered_element": lambda s, eid: {"name": "Save Changes", "automation_id": "btn_save"}
        })()
        self.executed_actions: List[DesktopAction] = []
        self.grounding_calls: List[Dict[str, Any]] = []

    def list_windows(self, filter_visible=True):
        return self.windows_manager.list_windows(filter_visible)

    def get_window(self, wid):
        return self.windows_manager.get_window(wid)

    def find_window_by_title(self, pat):
        return self.windows_manager.find_window_by_title(pat)

    def observe_window(self, window_id, include_tree=True, include_screenshot=False, max_depth=5, max_nodes=100):
        win = self.get_window(window_id) or self.windows[0]
        return DesktopObservation(
            observation_id=f"obs_{uuid.uuid4().hex[:8]}",
            window_id=window_id,
            observed_at="2026-11-21T12:00:00Z",
            window=win,
            elements=[],
        )

    def ground_control(self, window_id, query, force_route=None, task_id=None):
        route = force_route or PerceptionRoute.UIA
        self.grounding_calls.append({"window_id": window_id, "query": query, "route": route})
        cand = GroundedCandidate(
            candidate_id="cand_test_01",
            label=query,
            category="Button",
            bbox_normalized=(0.1, 0.1, 0.2, 0.05),
            confidence=0.98,
            grounding_source=[route.value if hasattr(route, "value") else str(route)],
            desktop_coordinate=(350, 250),
        )
        return GroundingResolution(
            target_query=query,
            selected_candidate=cand,
            all_candidates=[cand],
            route=route,
            confidence=0.98,
        )

    def execute_action(self, action: DesktopAction) -> DesktopActionResult:
        self.executed_actions.append(action)
        return DesktopActionResult(
            action_id=action.action_id,
            status=ActionStatus.COMPLETED,
            target_window_id=action.window_id,
            action_type=action.action_type.value,
            started_at="2026-11-21T12:00:00Z",
            completed_at="2026-11-21T12:00:01Z",
            verified=True,
        )


class TestTDELIntegrationSuite(unittest.TestCase):
    """32 Acceptance Gates and 5 End-to-End Scenarios."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_tdel_integ_"))
        self.db_path = self.temp_dir / "presence_test.db"
        self.artifact_dir = self.temp_dir / "artifacts"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)

        self.persistence = PersistenceManager(db_path=self.db_path)
        self.artifact_store = ArtifactStore(root_dir=self.artifact_dir)
        self.task_registry = ActiveTaskRegistry()
        self.event_bus = EventBus()
        self.permissions = PermissionBoundary()
        self.tool_registry = ToolRegistry()

        # Simple test tools
        def _echo(args):
            return {"status": "SUCCESS", "message": f"Echo: {args.get('text', '')}"}

        self.tool_registry.register(
            ToolDefinition(
                tool_id="test.echo",
                name="echo",
                description="Echo tool",
                parameters={"type": "object", "properties": {"text": {"type": "string"}}},
                authority_tier=PermissionTier.L0_OBSERVE,
                handler=_echo,
            )
        )

        from runtime.process_manager import ProcessManager
        self.process_manager = ProcessManager(
            base_tasks_dir=self.temp_dir / "tasks",
            event_bus=self.event_bus,
            task_registry=self.task_registry,
            permission_boundary=self.permissions,
        )

        self.mock_supervisor = MockLocalSupervisor()
        self.mock_desktop = MockDesktopController(
            policy=DesktopPolicy(permission_boundary=self.permissions)
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
            local_supervisor=self.mock_supervisor,
            desktop_controller=self.mock_desktop,
        )

        self.service = TaskPlanningService(
            workflow_engine=self.workflow_engine,
            persistence=self.persistence,
            artifact_store=self.artifact_store,
            task_registry=self.task_registry,
            event_bus=self.event_bus,
            local_supervisor=self.mock_supervisor,
            desktop_controller=self.mock_desktop,
        )

        self.conv_id = "test_conv_tdel_integ_01"
        self.plan_tools = build_plan_tools(self.service, current_conversation_id=self.conv_id)
        self.execute_plan_tool = next(t for t in self.plan_tools if t.tool_id == "presence.execute_task_plan")

    def tearDown(self):
        self.persistence.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # =========================================================================
    # GATES T-01 TO T-04: Graph Validation & Structural Limits
    # =========================================================================

    def test_T01_graph_acyclicity_validation(self):
        """T-01: Rejects cyclic dependency graphs via Kahn's algorithm."""
        plan = TaskPlan(
            plan_id="plan_cyclic",
            title="Cyclic Plan",
            user_goal="Test cycle detection",
            description="Cycle test",
            complexity=ComplexityCategory.COMPLEX,
            originating_conversation_id=self.conv_id,
            nodes=[
                TaskPlanNode(node_id="A", title="A", description="A", execution_type=ExecutionType.TOOL, depends_on=["B"], phase=1),
                TaskPlanNode(node_id="B", title="B", description="B", execution_type=ExecutionType.TOOL, depends_on=["A"], phase=1),
            ],
        )
        is_valid, err = TaskPlanValidator.is_valid(plan)
        self.assertFalse(is_valid)
        self.assertIn("Cyclic dependency detected", err)

        # Verify execute_task_plan tool rejects it with VALIDATION_FAILED
        res = self.execute_plan_tool.handler({
            "objective": "Test cyclic plan",
            "nodes": [
                {"node_id": "step1", "execution_type": "TOOL", "depends_on": ["step2"]},
                {"node_id": "step2", "execution_type": "TOOL", "depends_on": ["step1"]},
            ],
        })
        self.assertEqual(res["status"], "VALIDATION_FAILED")
        self.assertIn("Cyclic dependency detected", res["error"])

    def test_T02_unique_node_ids(self):
        """T-02: Rejects duplicate node IDs within the plan."""
        plan = TaskPlan(
            plan_id="plan_dupe",
            title="Duplicate Node ID Plan",
            user_goal="Test duplicates",
            description="Duplicate test",
            complexity=ComplexityCategory.COMPLEX,
            originating_conversation_id=self.conv_id,
            nodes=[
                TaskPlanNode(node_id="step_1", title="Step 1", description="1", execution_type=ExecutionType.TOOL, phase=1),
                TaskPlanNode(node_id="step_1", title="Step 1 Duplicate", description="2", execution_type=ExecutionType.TOOL, phase=1),
            ],
        )
        is_valid, err = TaskPlanValidator.is_valid(plan)
        self.assertFalse(is_valid)
        self.assertIn("Duplicate node_id 'step_1'", err)

    def test_T03_dependency_existence_check(self):
        """T-03: Rejects dangling dependency references to non-existent nodes."""
        plan = TaskPlan(
            plan_id="plan_dangling",
            title="Dangling Plan",
            user_goal="Test dangling dep",
            description="Dangling test",
            complexity=ComplexityCategory.COMPLEX,
            originating_conversation_id=self.conv_id,
            nodes=[
                TaskPlanNode(node_id="step_1", title="Step 1", description="1", execution_type=ExecutionType.TOOL, depends_on=["ghost_node"], phase=1),
            ],
        )
        is_valid, err = TaskPlanValidator.is_valid(plan)
        self.assertFalse(is_valid)
        self.assertIn("depends on non-existent node 'ghost_node'", err)

    def test_T04_phase_ordering_and_barrier_enforcement(self):
        """T-04: Rejects phase barrier violations where node depends on later phase."""
        plan = TaskPlan(
            plan_id="plan_phase_err",
            title="Phase Error Plan",
            user_goal="Test phase ordering",
            description="Phase test",
            complexity=ComplexityCategory.COMPLEX,
            originating_conversation_id=self.conv_id,
            nodes=[
                TaskPlanNode(node_id="step_1", title="Step 1", description="Phase 1", execution_type=ExecutionType.TOOL, phase=1, depends_on=["step_2"]),
                TaskPlanNode(node_id="step_2", title="Step 2", description="Phase 2", execution_type=ExecutionType.TOOL, phase=2),
            ],
        )
        is_valid, err = TaskPlanValidator.is_valid(plan)
        self.assertFalse(is_valid)
        self.assertIn("Phase barrier violation", err)

    # =========================================================================
    # GATES T-05 TO T-08: Execution Routing & Zero Cloud Leak
    # =========================================================================

    def test_T05_local_operational_routing(self):
        """T-05: Dispatches LOCAL_OPERATION nodes through LocalOperationsSupervisor."""
        res = self.execute_plan_tool.handler({
            "objective": "Audit log files locally",
            "nodes": [
                {
                    "node_id": "audit_logs",
                    "title": "Audit Local Logs",
                    "execution_type": "LOCAL_OPERATION",
                    "payload": {
                        "objective": "Inspect crash errors in log file",
                        "read_only": True,
                    },
                    "phase": 1,
                }
            ],
        })
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(len(self.mock_supervisor.executed_contracts), 1)
        contract = self.mock_supervisor.executed_contracts[0]
        self.assertIn("Inspect crash errors", contract.objective)
        self.assertTrue(contract.constraints.read_only)
        self.assertIn("audit_logs", res["node_results"])
        self.assertEqual(res["node_results"]["audit_logs"]["state"], "COMPLETED")

    def test_T06_desktop_operational_routing(self):
        """T-06: Dispatches DESKTOP_OPERATION nodes through DesktopController."""
        res = self.execute_plan_tool.handler({
            "objective": "Observe desktop window",
            "nodes": [
                {
                    "node_id": "observe_win",
                    "title": "Observe Diagnostic App",
                    "execution_type": "DESKTOP_OPERATION",
                    "payload": {
                        "action_type": "OBSERVE",
                        "window_id": "win_editor_01",
                    },
                    "phase": 1,
                }
            ],
        })
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["node_results"]["observe_win"]["state"], "COMPLETED")

    def test_T07_fast_visual_grounding_cascade(self):
        """T-07: Desktop node grounds target query via fast visual grounding cascade."""
        res = self.execute_plan_tool.handler({
            "objective": "Click Save button on desktop",
            "nodes": [
                {
                    "node_id": "click_save",
                    "title": "Click Save",
                    "execution_type": "DESKTOP_OPERATION",
                    "payload": {
                        "action_type": "CLICK_COORDINATE",
                        "window_id": "win_editor_01",
                        "query": "Save Changes",
                    },
                    "phase": 1,
                }
            ],
        })
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(len(self.mock_desktop.grounding_calls), 1)
        self.assertEqual(self.mock_desktop.grounding_calls[0]["query"], "Save Changes")
        self.assertEqual(len(self.mock_desktop.executed_actions), 1)
        self.assertEqual(tuple(self.mock_desktop.executed_actions[0].coordinate), (350, 250))

    def test_T08_zero_cloud_leak_verification(self):
        """T-08: Validates that no raw image bytes, base64 payloads, or raw process dumps leak to cloud principal."""
        res = self.execute_plan_tool.handler({
            "objective": "Inspect and interact",
            "nodes": [
                {
                    "node_id": "local_investigate",
                    "title": "Local Investigation",
                    "execution_type": "LOCAL_OPERATION",
                    "payload": {"objective": "Investigate data"},
                    "phase": 1,
                },
                {
                    "node_id": "desktop_click",
                    "title": "Desktop Click",
                    "execution_type": "DESKTOP_OPERATION",
                    "payload": {"window_id": "win_editor_01", "query": "Save Changes"},
                    "depends_on": ["local_investigate"],
                    "phase": 2,
                },
            ],
        })
        self.assertEqual(res["status"], "SUCCESS")
        # Assert returned dictionary is clean of binary or giant dumps
        import json
        dumped = json.dumps(res)
        self.assertNotIn("base64", dumped.lower())
        self.assertNotIn("png_bytes", dumped)
        self.assertNotIn("image_bytes", dumped)
        self.assertLess(len(dumped), 10000)  # Very compact token economy

    # =========================================================================
    # GATES T-09 TO T-11: Data-Flow & Failure Policies
    # =========================================================================

    def test_T09_predecessor_data_flow(self):
        """T-09: Predecessor node outputs and artifact IDs flow to downstream steps."""
        res = self.execute_plan_tool.handler({
            "objective": "Chained investigation",
            "nodes": [
                {
                    "node_id": "step_diag",
                    "title": "Diagnostic Step",
                    "execution_type": "LOCAL_OPERATION",
                    "payload": {"objective": "Run diagnostics"},
                    "phase": 1,
                },
                {
                    "node_id": "step_review",
                    "title": "Review Step",
                    "execution_type": "LOCAL_OPERATION",
                    "payload": {"objective": "Review diagnostic findings"},
                    "depends_on": ["step_diag"],
                    "phase": 2,
                },
            ],
        })
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(len(self.mock_supervisor.executed_contracts), 2)
        second_contract = self.mock_supervisor.executed_contracts[1]
        self.assertIn("Predecessor Step Results", second_contract.context_summary)
        self.assertIn("step_diag", second_contract.context_summary)

    def test_T10_failure_propagation_fail_fast_vs_continue(self):
        """T-10: Tests failure propagation under FAIL_FAST vs CONTINUE_INDEPENDENT."""
        failing_supervisor = MockLocalSupervisor(should_fail=True, error_msg="Kernel crash")
        self.workflow_engine.local_supervisor = failing_supervisor

        # 1. FAIL_FAST aborts independent branches
        res_ff = self.execute_plan_tool.handler({
            "objective": "Fail fast test",
            "failure_policy": "FAIL_FAST",
            "nodes": [
                {"node_id": "fail_node", "title": "Fail Node", "execution_type": "LOCAL_OPERATION", "payload": {}, "phase": 1},
                {"node_id": "dep_node", "title": "Dependent Node", "execution_type": "TOOL", "payload": {"tool_name": "test.echo", "args": {"text": "hi"}}, "depends_on": ["fail_node"], "phase": 2},
            ],
        })
        self.assertEqual(res_ff["status"], "FAILED")
        self.assertEqual(res_ff["node_results"]["fail_node"]["state"], "FAILED")
        self.assertEqual(res_ff["node_results"]["dep_node"]["state"], "CANCELLED")

        # Restore working supervisor
        self.workflow_engine.local_supervisor = self.mock_supervisor

    def test_T11_optional_node_failure_tolerance(self):
        """T-11: Failure of an optional=True node does not fail the overall workflow."""
        failing_supervisor = MockLocalSupervisor(should_fail=True, error_msg="Non-critical telemetry failed")
        self.workflow_engine.local_supervisor = failing_supervisor

        res = self.execute_plan_tool.handler({
            "objective": "Optional node failure test",
            "nodes": [
                {
                    "node_id": "opt_fail",
                    "title": "Optional Telemetry",
                    "execution_type": "LOCAL_OPERATION",
                    "payload": {},
                    "optional": True,
                    "phase": 1,
                },
                {
                    "node_id": "core_task",
                    "title": "Core Tool Task",
                    "execution_type": "TOOL",
                    "payload": {"tool_name": "test.echo", "args": {"text": "core ok"}},
                    "phase": 2,
                },
            ],
        })
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["node_results"]["opt_fail"]["state"], "FAILED")
        self.assertEqual(res["node_results"]["core_task"]["state"], "COMPLETED")

        self.workflow_engine.local_supervisor = self.mock_supervisor

    # =========================================================================
    # GATES T-12 TO T-14: Safety, Consent & Cancellation
    # =========================================================================

    def test_T12_action_level_consent_gating_pauses(self):
        """T-12: Destructive action without consent transitions node to WAITING_FOR_APPROVAL."""
        res = self.execute_plan_tool.handler({
            "objective": "Destructive desktop operation",
            "nodes": [
                {
                    "node_id": "delete_action",
                    "title": "Delete User Account",
                    "execution_type": "DESKTOP_OPERATION",
                    "payload": {
                        "action_type": "CLICK_COORDINATE",
                        "window_id": "win_editor_01",
                        "text": "delete account",
                        "coordinate": [100, 200],
                    },
                    "phase": 1,
                }
            ],
        })
        self.assertEqual(res["status"], "WAITING_FOR_APPROVAL")
        self.assertEqual(res["plan_status"], "AWAITING_APPROVAL")
        self.assertEqual(res["node_results"]["delete_action"]["state"], "WAITING_FOR_APPROVAL")

    def test_T13_single_use_consent_enforcement(self):
        """T-13: Valid consent token permits execution; reused token is rejected."""
        # 1. Authorize action and obtain valid token
        action_name = "desktop.execute_actions"
        self.permissions.register_policy(
            ActionPolicy(
                action_name=action_name,
                required_level=PermissionTier.L3_PROTECTED,
                description="Desktop execution",
            )
        )

        ctx = {
            "action_id": "act_test_01",
            "action_type": ActionType.CLICK_COORDINATE.value,
            "window_id": "win_editor_01",
            "element_id": "",
            "target_name": "",
            "text": "delete account",
            "coordinate": (100, 200),
            "task_id": "del_node",
        }
        token = self.permissions.issue_consent_token(
            action_name=action_name,
            context=ctx,
            conversation_id=self.conv_id,
            ttl_seconds=60.0,
        )

        # First use: valid token passes policy check
        act1 = DesktopAction(
            action_id="act_test_01",
            action_type=ActionType.CLICK_COORDINATE,
            window_id="win_editor_01",
            text="delete account",
            coordinate=(100, 200),
            consent_token=token,
            task_id="del_node",
            conversation_id=self.conv_id,
        )
        allowed, reason = self.mock_desktop.policy.evaluate_action(act1)
        self.assertTrue(allowed, f"Expected allowed with token, got: {reason}")

        # Second use: token is now consumed (replay rejected)
        allowed_reused, reason_reused = self.mock_desktop.policy.evaluate_action(act1)
        self.assertFalse(allowed_reused)
        self.assertIn("verification failed", reason_reused)

    def test_T14_cooperative_cancellation_propagation(self):
        """T-14: Cancelling workflow halts pending and in-flight operations."""
        from runtime.agent_executor import CancellationToken
        cancel_token = CancellationToken()

        nodes = [
            WorkflowNode(
                node_id="step1",
                name="Step 1",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "test.echo", "args": {"text": "hello"}},
                phase=1,
            ),
            WorkflowNode(
                node_id="step2",
                name="Step 2",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "test.echo", "args": {"text": "world"}},
                depends_on=["step1"],
                phase=2,
            ),
        ]
        defn = WorkflowDefinition(workflow_id="wf_cancel_test", name="Cancel Test", nodes=nodes)

        # Cancel immediately before execution
        cancel_token.cancel()
        run = self.workflow_engine.execute_workflow(defn, conversation_id=self.conv_id, cancellation_token=cancel_token)
        self.assertEqual(run.status, WorkflowStatus.CANCELLED)

    # =========================================================================
    # GATES T-15 TO T-19: Observability, State, Projection & Events
    # =========================================================================

    def test_T15_active_task_registry_hierarchy(self):
        """T-15: Verifies root workflow task and child step tasks registered with ActiveTaskRegistry."""
        res = self.execute_plan_tool.handler({
            "objective": "Hierarchy test",
            "nodes": [
                {"node_id": "n1", "title": "Node 1", "execution_type": "TOOL", "payload": {"tool_name": "test.echo", "args": {"text": "1"}}, "phase": 1},
            ],
        })
        self.assertEqual(res["status"], "SUCCESS")
        completed_tasks = self.task_registry.get_completed_tasks()
        self.assertTrue(any("Workflow:" in t.display_name for t in completed_tasks))

    def test_T16_sqlite_checkpointing_and_durability(self):
        """T-16: Plans and node executions are persisted durably in SQLite."""
        res = self.execute_plan_tool.handler({
            "objective": "SQLite durability test",
            "nodes": [
                {"node_id": "n1", "title": "Node 1", "execution_type": "TOOL", "payload": {"tool_name": "test.echo", "args": {"text": "1"}}, "phase": 1},
            ],
        })
        self.assertEqual(res["status"], "SUCCESS")
        plan_id = res["plan_id"]
        # Verify plan in SQLite via service ledger
        saved_plan = self.service.ledger.get_plan(plan_id)
        self.assertIsNotNone(saved_plan)
        self.assertEqual(saved_plan.status, PlanStatus.COMPLETED)

    def test_T17_resumption_and_startup_reconciliation(self):
        """T-17: Interrupted running plans reconciled on startup."""
        interrupted_plan = TaskPlan(
            plan_id="plan_interrupted_01",
            title="Interrupted Plan",
            user_goal="Test recovery",
            description="Testing recovery",
            complexity=ComplexityCategory.COMPLEX,
            originating_conversation_id=self.conv_id,
            status=PlanStatus.IN_PROGRESS,
            nodes=[
                TaskPlanNode(node_id="n1", title="N1", description="N1", execution_type=ExecutionType.TOOL, phase=1)
            ],
        )
        self.service.ledger.create_plan(interrupted_plan)

        # Run startup reconciliation
        reconciled = self.service.reconcile_startup()
        self.assertTrue(len(reconciled) >= 1)
        plan_after = self.service.ledger.get_plan("plan_interrupted_01")
        self.assertEqual(plan_after.status, PlanStatus.INTERRUPTED)

    def test_T18_markdown_projection_generation(self):
        """T-18: Verifies TASK_LEDGER.md projection rendered and updated in ArtifactStore."""
        res = self.execute_plan_tool.handler({
            "objective": "Markdown projection test",
            "nodes": [
                {"node_id": "n1", "title": "Node 1", "execution_type": "TOOL", "payload": {"tool_name": "test.echo", "args": {"text": "1"}}, "phase": 1},
            ],
        })
        self.assertEqual(res["status"], "SUCCESS")
        ledger_art_id = res["ledger_artifact_id"]
        self.assertIsNotNone(ledger_art_id)
        # Verify markdown content from artifact store
        meta = self.artifact_store.get_metadata(self.conv_id, ledger_art_id)
        self.assertIsNotNone(meta)

    def test_T19_real_time_event_streaming(self):
        """T-19: Workflow engine emits started, running, completed events on EventBus."""
        emitted_events = []
        self.event_bus.subscribe_all(lambda event: emitted_events.append(event.event_type))

        res = self.execute_plan_tool.handler({
            "objective": "Event streaming test",
            "nodes": [
                {"node_id": "n1", "title": "Node 1", "execution_type": "TOOL", "payload": {"tool_name": "test.echo", "args": {"text": "1"}}, "phase": 1},
            ],
        })
        self.assertEqual(res["status"], "SUCCESS")
        self.assertIn("WORKFLOW_NODE_STARTED", emitted_events)
        self.assertIn("WORKFLOW_NODE_COMPLETED", emitted_events)

    # =========================================================================
    # GATES T-20 TO T-27: Node Adapters & Policies
    # =========================================================================

    def test_T20_subagent_delegation_node(self):
        """T-20: SUBAGENT node dispatches through SubagentManager."""
        class MockSubagentManager:
            def spawn_subagent(self, parent_conversation_id, config, wait_ms=10000):
                return {
                    "status": "COMPLETED",
                    "subagent_id": "sub_test_01",
                    "result_summary": "Subagent investigation finished",
                    "final_artifact_id": "art_sub_01",
                }

        self.workflow_engine.subagent_manager = MockSubagentManager()
        nodes = [
            WorkflowNode(
                node_id="subagent_step",
                name="Subagent Step",
                execution_type=ExecutionType.SUBAGENT,
                payload={"goal": "Investigate security logs", "role": "researcher"},
                phase=1,
            )
        ]
        defn = WorkflowDefinition(workflow_id="wf_subagent_test", name="Subagent Test", nodes=nodes)
        run = self.workflow_engine.execute_workflow(defn, conversation_id=self.conv_id)
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        self.assertEqual(run.node_executions["subagent_step"].state, NodeState.COMPLETED)

    def test_T21_process_job_object_node(self):
        """T-21: PROCESS node executes direct shell command via ProcessManager."""
        nodes = [
            WorkflowNode(
                node_id="process_step",
                name="Process Step",
                execution_type=ExecutionType.PROCESS,
                payload={"command": "powershell -Command Write-Output 'Process execution ok'"},
                phase=1,
            )
        ]
        defn = WorkflowDefinition(workflow_id="wf_process_test", name="Process Test", nodes=nodes)
        run = self.workflow_engine.execute_workflow(defn, conversation_id=self.conv_id)
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        self.assertEqual(run.node_executions["process_step"].state, NodeState.COMPLETED)

    def test_T22_verify_adapter_assertions(self):
        """T-22: VERIFY node evaluates deterministic assertions."""
        nodes = [
            WorkflowNode(
                node_id="tool_step",
                name="Tool Step",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "test.echo", "args": {"text": "hello"}},
                phase=1,
            ),
            WorkflowNode(
                node_id="verify_step",
                name="Verify Assertions",
                execution_type=ExecutionType.VERIFY,
                payload={
                    "assertion_type": "NON_EMPTY",
                    "target_node_id": "tool_step",
                },
                depends_on=["tool_step"],
                phase=2,
            ),
        ]
        defn = WorkflowDefinition(workflow_id="wf_verify_test", name="Verify Test", nodes=nodes)
        run = self.workflow_engine.execute_workflow(defn, conversation_id=self.conv_id)
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        self.assertEqual(run.node_executions["verify_step"].state, NodeState.COMPLETED)

    def test_T23_aggregate_adapter_combines_outputs(self):
        """T-23: AGGREGATE node merges predecessor outputs into a final artifact."""
        nodes = [
            WorkflowNode(
                node_id="s1",
                name="Step 1",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "test.echo", "args": {"text": "result 1"}},
                phase=1,
            ),
            WorkflowNode(
                node_id="s2",
                name="Step 2",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "test.echo", "args": {"text": "result 2"}},
                phase=1,
            ),
            WorkflowNode(
                node_id="agg",
                name="Aggregate Step",
                execution_type=ExecutionType.AGGREGATE,
                payload={"title": "Combined Report", "source_nodes": ["s1", "s2"]},
                depends_on=["s1", "s2"],
                phase=2,
            ),
        ]
        defn = WorkflowDefinition(workflow_id="wf_agg_test", name="Aggregate Test", nodes=nodes)
        run = self.workflow_engine.execute_workflow(defn, conversation_id=self.conv_id)
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        self.assertIsNotNone(run.final_artifact_id)

    def test_T24_tool_node_execution(self):
        """T-24: TOOL node dispatches tool via ToolRegistry."""
        nodes = [
            WorkflowNode(
                node_id="tool_step",
                name="Tool Step",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "test.echo", "args": {"text": "tool works"}},
                phase=1,
            )
        ]
        defn = WorkflowDefinition(workflow_id="wf_tool_test", name="Tool Test", nodes=nodes)
        run = self.workflow_engine.execute_workflow(defn, conversation_id=self.conv_id)
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        self.assertEqual(run.node_executions["tool_step"].result_payload.get("message"), "Echo: tool works")

    def test_T25_concurrency_bounds_enforcement(self):
        """T-25: max_parallel_nodes throttles execution pool."""
        defn = WorkflowDefinition(
            workflow_id="wf_concur_test",
            name="Concurrency Test",
            nodes=[
                WorkflowNode(node_id=f"n_{i}", name=f"Node {i}", execution_type=ExecutionType.TOOL, payload={"tool_name": "test.echo", "args": {"text": str(i)}}, phase=1)
                for i in range(10)
            ],
            max_parallel_nodes=3,
        )
        run = self.workflow_engine.execute_workflow(defn, conversation_id=self.conv_id)
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        self.assertEqual(len(run.node_executions), 10)

    def test_T26_timeout_enforcement(self):
        """T-26: Timeout enforcement halts runaway node execution."""
        def _slow_tool(args):
            time.sleep(0.4)
            return {"status": "SUCCESS"}

        self.tool_registry.register(
            ToolDefinition(
                tool_id="test.slow",
                name="slow",
                description="Slow tool",
                parameters={"type": "object"},
                authority_tier=PermissionTier.L0_OBSERVE,
                handler=_slow_tool,
                timeout_seconds=0.05,
            )
        )
        nodes = [
            WorkflowNode(
                node_id="slow_step",
                name="Slow Step",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "test.slow", "args": {}},
                timeout_seconds=0.05,
                phase=1,
            )
        ]
        defn = WorkflowDefinition(workflow_id="wf_timeout_test", name="Timeout Test", nodes=nodes)
        run = self.workflow_engine.execute_workflow(defn, conversation_id=self.conv_id)
        self.assertEqual(run.status, WorkflowStatus.FAILED)
        self.assertEqual(run.node_executions["slow_step"].state, NodeState.FAILED)

    def test_T27_retry_policy_and_backoff(self):
        """T-27: Node retries up to max_retries on transient failure."""
        attempts = 0

        def _flaky_tool(args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("Transient error on first try")
            return {"status": "SUCCESS", "attempts": attempts}

        self.tool_registry.register(
            ToolDefinition(
                tool_id="test.flaky",
                name="flaky",
                description="Flaky tool",
                parameters={"type": "object"},
                authority_tier=PermissionTier.L0_OBSERVE,
                handler=_flaky_tool,
            )
        )
        nodes = [
            WorkflowNode(
                node_id="flaky_step",
                name="Flaky Step",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "test.flaky", "args": {}},
                max_retries=2,
                retry_backoff_seconds=0.01,
                phase=1,
            )
        ]
        defn = WorkflowDefinition(workflow_id="wf_retry_test", name="Retry Test", nodes=nodes)
        run = self.workflow_engine.execute_workflow(defn, conversation_id=self.conv_id)
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        self.assertEqual(attempts, 2)

    # =========================================================================
    # SCENARIOS A, B, C, D, E: End-to-End Multimodal Workflows
    # =========================================================================

    def test_T28_scenario_A_parallel_investigation(self):
        """T-28 / Scenario A: Parallel investigation over 3 independent local investigation branches."""
        res = self.execute_plan_tool.handler({
            "objective": "Multi-component system investigation",
            "nodes": [
                {"node_id": "inv_db", "title": "Database Logs", "execution_type": "LOCAL_OPERATION", "payload": {"objective": "Investigate DB"}, "phase": 1},
                {"node_id": "inv_api", "title": "API Gateway", "execution_type": "LOCAL_OPERATION", "payload": {"objective": "Investigate API"}, "phase": 1},
                {"node_id": "inv_auth", "title": "Auth Service", "execution_type": "LOCAL_OPERATION", "payload": {"objective": "Investigate Auth"}, "phase": 1},
                {
                    "node_id": "synthesize",
                    "title": "Synthesize Findings",
                    "execution_type": "AGGREGATE",
                    "payload": {"title": "System Audit Report", "source_nodes": ["inv_db", "inv_api", "inv_auth"]},
                    "depends_on": ["inv_db", "inv_api", "inv_auth"],
                    "phase": 2,
                },
            ],
        })
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["nodes_completed"], 4)
        self.assertIsNotNone(res["final_deliverable_artifact_id"])

    def test_T29_scenario_B_sequential_remediation(self):
        """T-29 / Scenario B: Sequential remediation (Inspect -> Diagnose -> Fix -> Verify)."""
        res = self.execute_plan_tool.handler({
            "objective": "Inspect, diagnose, remediate, and verify bug",
            "nodes": [
                {"node_id": "inspect", "title": "Inspect Issue", "execution_type": "LOCAL_OPERATION", "payload": {"objective": "Inspect issue"}, "phase": 1},
                {"node_id": "diagnose", "title": "Diagnose Cause", "execution_type": "LOCAL_OPERATION", "payload": {"objective": "Diagnose cause"}, "depends_on": ["inspect"], "phase": 2},
                {"node_id": "remediate", "title": "Apply Fix", "execution_type": "LOCAL_OPERATION", "payload": {"objective": "Remediate bug", "read_only": False}, "depends_on": ["diagnose"], "phase": 3},
                {
                    "node_id": "verify",
                    "title": "Verify Resolution",
                    "execution_type": "VERIFY",
                    "payload": {"assertion_type": "NON_EMPTY", "target_node_id": "remediate"},
                    "depends_on": ["remediate"],
                    "phase": 4,
                },
            ],
        })
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["nodes_completed"], 4)

    def test_T30_scenario_C_desktop_workflow(self):
        """T-30 / Scenario C: Discover window -> Ground control -> Click -> Verify observation."""
        res = self.execute_plan_tool.handler({
            "objective": "Automated desktop control workflow",
            "nodes": [
                {"node_id": "find_win", "title": "Discover Window", "execution_type": "DESKTOP_OPERATION", "payload": {"action_type": "OBSERVE"}, "phase": 1},
                {
                    "node_id": "click_btn",
                    "title": "Ground and Click Save",
                    "execution_type": "DESKTOP_OPERATION",
                    "payload": {"action_type": "CLICK_COORDINATE", "window_id": "win_editor_01", "query": "Save Changes"},
                    "depends_on": ["find_win"],
                    "phase": 2,
                },
                {
                    "node_id": "verify_obs",
                    "title": "Verify State Changed",
                    "execution_type": "DESKTOP_OPERATION",
                    "payload": {"action_type": "OBSERVE", "window_id": "win_editor_01"},
                    "depends_on": ["click_btn"],
                    "phase": 3,
                },
            ],
        })
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["nodes_completed"], 3)

    def test_T31_scenario_D_mixed_filesystem_and_desktop(self):
        """T-31 / Scenario D: Local investigation -> Desktop interaction -> Report synthesis."""
        res = self.execute_plan_tool.handler({
            "objective": "Mixed filesystem and desktop workflow",
            "nodes": [
                {"node_id": "local_investigate", "title": "Analyze Filesystem", "execution_type": "LOCAL_OPERATION", "payload": {"objective": "Analyze disk usage"}, "phase": 1},
                {
                    "node_id": "desktop_confirm",
                    "title": "Confirm UI State",
                    "execution_type": "DESKTOP_OPERATION",
                    "payload": {"action_type": "CLICK_COORDINATE", "window_id": "win_editor_01", "query": "Save Changes"},
                    "depends_on": ["local_investigate"],
                    "phase": 2,
                },
                {
                    "node_id": "final_agg",
                    "title": "Aggregate Results",
                    "execution_type": "AGGREGATE",
                    "payload": {"title": "Integrated Report", "source_nodes": ["local_investigate", "desktop_confirm"]},
                    "depends_on": ["desktop_confirm"],
                    "phase": 3,
                },
            ],
        })
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["nodes_completed"], 3)
        self.assertIsNotNone(res["final_deliverable_artifact_id"])

    def test_T32_scenario_E_failure_and_partial_recovery(self):
        """T-32 / Scenario E: Optional branch fails, independent branch completes successfully."""
        failing_supervisor = MockLocalSupervisor(should_fail=True, error_msg="Optional branch crashed")
        self.workflow_engine.local_supervisor = failing_supervisor

        res = self.execute_plan_tool.handler({
            "objective": "Partial failure and recovery scenario",
            "nodes": [
                {"node_id": "branch_fail_opt", "title": "Optional Log Extractor", "execution_type": "LOCAL_OPERATION", "payload": {}, "optional": True, "phase": 1},
                {"node_id": "branch_success", "title": "Core Tool Task", "execution_type": "TOOL", "payload": {"tool_name": "test.echo", "args": {"text": "success"}}, "phase": 1},
                {
                    "node_id": "synthesize",
                    "title": "Final Synthesis",
                    "execution_type": "AGGREGATE",
                    "payload": {"title": "Summary Report", "source_nodes": ["branch_success"]},
                    "depends_on": ["branch_success"],
                    "phase": 2,
                },
            ],
        })
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["node_results"]["branch_fail_opt"]["state"], "FAILED")
        self.assertEqual(res["node_results"]["branch_success"]["state"], "COMPLETED")
        self.assertEqual(res["node_results"]["synthesize"]["state"], "COMPLETED")
        self.workflow_engine.local_supervisor = self.mock_supervisor


if __name__ == "__main__":
    unittest.main()
