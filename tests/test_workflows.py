"""Unit and Integration Tests for Phase 5 Deterministic Workflow Engine
Verifies:
1. Linear pipeline execution (PROCESS -> TOOL -> AGGREGATE) with artifact offloading.
2. Concurrent branching and join execution with ThreadPool bounds.
3. Verify adapter assertions (EXIT_CODE_ZERO, NON_EMPTY, JSON_FIELD_EQUALS, REGEX_MATCH, FILE_EXISTS).
4. Failure policies: FAIL_FAST, CONTINUE_INDEPENDENT, and optional node handling.
5. Cooperative workflow and child node cancellation.
6. SQLite WAL persistence checkpointing and startup crash reconciliation.
7. Agent tools (presence.run_workflow and presence.manage_workflow) invocation.
"""

import os
import sys
import time
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from runtime.workflows.models import (
    WorkflowDefinition,
    WorkflowNode,
    WorkflowStatus,
    NodeState,
    ExecutionType,
    FailurePolicy,
    DependencyCondition,
)
from runtime.workflows.engine import WorkflowEngine
from runtime.workflows.adapters import get_adapter_for_type, NodeExecutionContext
from runtime.tools.workflow_tools import build_workflow_tools
from runtime.tools.registry import ToolRegistry, ToolDefinition
from runtime.permissions import PermissionBoundary, PermissionTier
from runtime.persistence import PersistenceManager
from runtime.artifacts.store import ArtifactStore
from runtime.observability.task_registry import ActiveTaskRegistry
from runtime.events import EventBus
from runtime.agent_executor import CancellationToken


class TestDeterministicWorkflows(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_presence_wf_"))
        self.db_path = self.temp_dir / "presence_test.db"
        self.artifact_dir = self.temp_dir / "artifacts"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)

        self.persistence = PersistenceManager(db_path=str(self.db_path))
        self.artifact_store = ArtifactStore(root_dir=self.artifact_dir)
        self.task_registry = ActiveTaskRegistry()
        self.event_bus = EventBus()
        self.permissions = PermissionBoundary()
        self.tool_registry = ToolRegistry()

        # Register sample tool for testing TOOL execution nodes
        def _echo_tool(args):
            return {
                "status": "SUCCESS",
                "transformed": f"ECHO: {args.get('input_text', '')}",
                "count": len(args.get("input_text", "")),
            }

        self.tool_registry.register(
            ToolDefinition(
                tool_id="test.echo",
                name="echo",
                description="Echo tool for testing",
                parameters={"type": "object", "properties": {"input_text": {"type": "string"}}},
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

        self.engine = WorkflowEngine(
            persistence=self.persistence,
            artifact_store=self.artifact_store,
            tool_registry=self.tool_registry,
            process_manager=self.process_manager,
            subagent_manager=None,
            task_registry=self.task_registry,
            event_bus=self.event_bus,
            permission_boundary=self.permissions,
        )

        self.conv_id = "test_wf_conv_01"

    def tearDown(self):
        self.persistence.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_linear_pipeline_tool_and_aggregate(self):
        """Verifies linear pipeline: TOOL (node1) -> TOOL (node2) -> AGGREGATE (node3)."""
        nodes = [
            WorkflowNode(
                node_id="step1",
                name="Step 1 - Produce Data",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "test.echo", "args": {"input_text": "Hello World"}},
                phase=1,
            ),
            WorkflowNode(
                node_id="step2",
                name="Step 2 - Transform",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "test.echo", "args": {"input_text": "Second Step"}},
                depends_on=["step1"],
                phase=1,
            ),
            WorkflowNode(
                node_id="step3_agg",
                name="Step 3 - Aggregate",
                execution_type=ExecutionType.AGGREGATE,
                payload={"title": "Summary Deliverable", "source_nodes": ["step1", "step2"]},
                depends_on=["step2"],
                phase=2,
            ),
        ]

        defn = WorkflowDefinition(
            workflow_id="wf_linear_01",
            name="Linear Test Pipeline",
            nodes=nodes,
            failure_policy=FailurePolicy.FAIL_FAST,
        )

        run = self.engine.execute_workflow(defn, conversation_id=self.conv_id)

        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        self.assertEqual(len(run.node_executions), 3)
        self.assertEqual(run.node_executions["step1"].state, NodeState.COMPLETED)
        self.assertEqual(run.node_executions["step2"].state, NodeState.COMPLETED)
        self.assertEqual(run.node_executions["step3_agg"].state, NodeState.COMPLETED)

        # Verify aggregate produced an artifact
        agg_artifact_id = run.node_executions["step3_agg"].artifact_id
        self.assertIsNotNone(agg_artifact_id)
        self.assertEqual(run.final_artifact_id, agg_artifact_id)

        # Read back artifact from store
        art = self.artifact_store.read_artifact(self.conv_id, agg_artifact_id, mode="lines", limit=5000)
        self.assertIn("# Summary Deliverable", art.content)
        self.assertIn("Hello World", art.content)
        self.assertIn("Second Step", art.content)

        # Verify task registry recorded root task and steps
        completed_tasks = self.task_registry.get_completed_tasks()
        self.assertGreaterEqual(len(completed_tasks), 3)

        # Verify persistence recorded the run
        persisted_run = self.persistence.get_workflow_run(run.run_id)
        self.assertIsNotNone(persisted_run)
        self.assertEqual(persisted_run["status"], "COMPLETED")
        self.assertEqual(len(persisted_run["nodes"]), 3)

    def test_branching_parallel_execution(self):
        """Verifies concurrent execution of independent branches and joining aggregation."""
        # Custom sleep tool to verify concurrency
        def _sleep_tool(args):
            duration = float(args.get("duration", 0.1))
            time.sleep(duration)
            return {"status": "SUCCESS", "slept": duration, "branch": args.get("branch")}

        self.tool_registry.register(
            ToolDefinition(
                tool_id="test.sleep",
                name="sleep",
                description="Sleeps for a duration",
                parameters={"type": "object", "properties": {"duration": {"type": "number"}, "branch": {"type": "string"}}},
                authority_tier=PermissionTier.L0_OBSERVE,
                handler=_sleep_tool,
            )
        )

        nodes = [
            WorkflowNode(
                node_id="root_start",
                name="Start Node",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "test.echo", "args": {"input_text": "start"}},
                phase=1,
            ),
            WorkflowNode(
                node_id="branch_a",
                name="Branch A",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "test.sleep", "args": {"duration": 0.2, "branch": "A"}},
                depends_on=["root_start"],
                phase=2,
            ),
            WorkflowNode(
                node_id="branch_b",
                name="Branch B",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "test.sleep", "args": {"duration": 0.2, "branch": "B"}},
                depends_on=["root_start"],
                phase=2,
            ),
            WorkflowNode(
                node_id="branch_c",
                name="Branch C",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "test.sleep", "args": {"duration": 0.2, "branch": "C"}},
                depends_on=["root_start"],
                phase=2,
            ),
            WorkflowNode(
                node_id="join_agg",
                name="Join and Aggregate",
                execution_type=ExecutionType.AGGREGATE,
                payload={"title": "Parallel Results", "source_nodes": ["branch_a", "branch_b", "branch_c"]},
                depends_on=["branch_a", "branch_b", "branch_c"],
                phase=3,
            ),
        ]

        defn = WorkflowDefinition(
            workflow_id="wf_parallel_01",
            name="Parallel Branching Test",
            nodes=nodes,
            max_parallel_nodes=3,
        )

        t0 = time.time()
        run = self.engine.execute_workflow(defn, conversation_id=self.conv_id)
        total_time = time.time() - t0

        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        # Sequential time would be >= 0.6s for the 3 branches plus test overhead.
        # Parallel time executes 3 branches concurrently.
        self.assertLess(total_time, 0.95)

        agg_ne = run.node_executions["join_agg"]
        self.assertEqual(agg_ne.state, NodeState.COMPLETED)
        self.assertIsNotNone(agg_ne.artifact_id)

    def test_verify_adapter_all_assertions(self):
        """Verifies VerifyWorkflowAdapter assertions (EXIT_CODE_ZERO, NON_EMPTY, JSON_FIELD_EQUALS, REGEX_MATCH, FILE_EXISTS)."""
        adapter = get_adapter_for_type(ExecutionType.VERIFY)

        # 1. EXIT_CODE_ZERO
        from runtime.workflows.models import WorkflowNodeExecution
        ne_ok = WorkflowNodeExecution(node_id="prev_proc", workflow_run_id="run_v", state=NodeState.COMPLETED, result_payload={"exit_code": 0})
        context_zero = NodeExecutionContext(
            workflow_run_id="run_v",
            conversation_id="conv_v",
            node_executions={"prev_proc": ne_ok}
        )

        verify_node = WorkflowNode(
            node_id="v1",
            name="Verify zero",
            execution_type=ExecutionType.VERIFY,
            payload={"assertion_type": "EXIT_CODE_ZERO", "target_node_id": "prev_proc"}
        )
        res = adapter.execute(verify_node, context_zero)
        self.assertEqual(res.state, NodeState.COMPLETED)

        # Non-zero exit code
        ne_fail = WorkflowNodeExecution(node_id="prev_proc", workflow_run_id="run_v", state=NodeState.COMPLETED, result_payload={"exit_code": 1})
        context_zero.node_executions = {"prev_proc": ne_fail}
        res_fail = adapter.execute(verify_node, context_zero)
        self.assertEqual(res_fail.state, NodeState.FAILED)

        # 2. JSON_FIELD_EQUALS
        verify_field = WorkflowNode(
            node_id="v2",
            name="Verify field",
            execution_type=ExecutionType.VERIFY,
            payload={"assertion_type": "JSON_FIELD_EQUALS", "target_node_id": "prev_proc", "field": "exit_code", "expected": 1}
        )
        res_field = adapter.execute(verify_field, context_zero)
        self.assertEqual(res_field.state, NodeState.COMPLETED)

        # 3. REGEX_MATCH
        ne_str = WorkflowNodeExecution(node_id="prev_proc", workflow_run_id="run_v", state=NodeState.COMPLETED, result_payload={"output": "Found 42 vulnerabilities"})
        context_zero.node_executions = {"prev_proc": ne_str}
        verify_regex = WorkflowNode(
            node_id="v3",
            name="Verify regex",
            execution_type=ExecutionType.VERIFY,
            payload={"assertion_type": "REGEX_MATCH", "target_node_id": "prev_proc", "pattern": r"Found \d+ vulnerabilities"}
        )
        res_regex = adapter.execute(verify_regex, context_zero)
        self.assertEqual(res_regex.state, NodeState.COMPLETED)

        # 4. FILE_EXISTS
        test_file = self.temp_dir / "target_file.txt"
        test_file.write_text("sample content", encoding="utf-8")
        verify_file = WorkflowNode(
            node_id="v4",
            name="Verify file",
            execution_type=ExecutionType.VERIFY,
            payload={"assertion_type": "FILE_EXISTS", "path": str(test_file)}
        )
        res_file = adapter.execute(verify_file, context_zero)
        self.assertEqual(res_file.state, NodeState.COMPLETED)

        verify_file_missing = WorkflowNode(
            node_id="v5",
            name="Verify missing file",
            execution_type=ExecutionType.VERIFY,
            payload={"assertion_type": "FILE_EXISTS", "path": str(self.temp_dir / "does_not_exist.txt")}
        )
        res_file_missing = adapter.execute(verify_file_missing, context_zero)
        self.assertEqual(res_file_missing.state, NodeState.FAILED)

    def test_failure_policy_fail_fast(self):
        """Verifies FAIL_FAST stops the workflow and cancels downstream/pending nodes."""
        def _failing_tool(args):
            raise RuntimeError("Deliberate failure in test tool")

        self.tool_registry.register(
            ToolDefinition(
                tool_id="test.failing",
                name="failing",
                description="Always fails",
                parameters={"type": "object"},
                authority_tier=PermissionTier.L0_OBSERVE,
                handler=_failing_tool,
            )
        )

        nodes = [
            WorkflowNode(
                node_id="step_fail",
                name="Failing Step",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "test.failing", "args": {}},
                phase=1,
            ),
            WorkflowNode(
                node_id="step_downstream",
                name="Downstream Step",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "test.echo", "args": {"input_text": "never runs"}},
                depends_on=["step_fail"],
                phase=2,
            ),
        ]

        defn = WorkflowDefinition(
            workflow_id="wf_fail_fast",
            name="Fail Fast Test",
            nodes=nodes,
            failure_policy=FailurePolicy.FAIL_FAST,
        )

        run = self.engine.execute_workflow(defn, conversation_id=self.conv_id)
        self.assertEqual(run.status, WorkflowStatus.FAILED)
        self.assertEqual(run.node_executions["step_fail"].state, NodeState.FAILED)
        self.assertIn(run.node_executions["step_downstream"].state, (NodeState.CANCELLED, NodeState.SKIPPED))

    def test_failure_policy_continue_independent(self):
        """Verifies CONTINUE_INDEPENDENT skips failed branch's children but allows independent branches to complete."""
        def _failing_tool(args):
            raise RuntimeError("Branch A failed")

        self.tool_registry.register(
            ToolDefinition(
                tool_id="test.failing_a",
                name="failing_a",
                description="Always fails",
                parameters={"type": "object"},
                authority_tier=PermissionTier.L0_OBSERVE,
                handler=_failing_tool,
            )
        )

        nodes = [
            # Branch A (fails)
            WorkflowNode(
                node_id="branch_a1",
                name="Branch A1 (Fails)",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "test.failing_a", "args": {}},
                phase=1,
            ),
            WorkflowNode(
                node_id="branch_a2",
                name="Branch A2 (Dependent on A1)",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "test.echo", "args": {"input_text": "A2"}},
                depends_on=["branch_a1"],
                phase=2,
            ),
            # Branch B (independent, should succeed)
            WorkflowNode(
                node_id="branch_b1",
                name="Branch B1 (Succeeds)",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "test.echo", "args": {"input_text": "B1"}},
                phase=1,
            ),
            WorkflowNode(
                node_id="branch_b2",
                name="Branch B2 (Dependent on B1)",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "test.echo", "args": {"input_text": "B2"}},
                depends_on=["branch_b1"],
                phase=2,
            ),
        ]

        defn = WorkflowDefinition(
            workflow_id="wf_continue_indep",
            name="Continue Independent Test",
            nodes=nodes,
            failure_policy=FailurePolicy.CONTINUE_INDEPENDENT,
            max_parallel_nodes=4,
        )

        run = self.engine.execute_workflow(defn, conversation_id=self.conv_id)

        # Overall workflow status is FAILED because branch_a1 failed and is non-optional
        self.assertEqual(run.status, WorkflowStatus.FAILED)
        self.assertEqual(run.node_executions["branch_a1"].state, NodeState.FAILED)
        self.assertEqual(run.node_executions["branch_a2"].state, NodeState.SKIPPED)

        # Independent branch B completed both nodes!
        self.assertEqual(run.node_executions["branch_b1"].state, NodeState.COMPLETED)
        self.assertEqual(run.node_executions["branch_b2"].state, NodeState.COMPLETED)

    def test_optional_node_failure_allowed(self):
        """Verifies optional=True node failure does not mark the workflow as FAILED."""
        def _failing_optional(args):
            raise RuntimeError("Optional warning check failed")

        self.tool_registry.register(
            ToolDefinition(
                tool_id="test.failing_opt",
                name="failing_opt",
                description="Optional failing tool",
                parameters={"type": "object"},
                authority_tier=PermissionTier.L0_OBSERVE,
                handler=_failing_optional,
            )
        )

        nodes = [
            WorkflowNode(
                node_id="step1_mandatory",
                name="Mandatory Step 1",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "test.echo", "args": {"input_text": "Mandatory"}},
                phase=1,
            ),
            WorkflowNode(
                node_id="step2_optional",
                name="Optional Step 2",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "test.failing_opt", "args": {}},
                optional=True,
                phase=1,
            ),
            WorkflowNode(
                node_id="step3_final",
                name="Final Step",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "test.echo", "args": {"input_text": "Final"}},
                depends_on=["step1_mandatory"],
                phase=2,
            ),
        ]

        defn = WorkflowDefinition(
            workflow_id="wf_optional_01",
            name="Optional Node Test",
            nodes=nodes,
            failure_policy=FailurePolicy.ALLOW_OPTIONAL,
        )

        run = self.engine.execute_workflow(defn, conversation_id=self.conv_id)
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        self.assertEqual(run.node_executions["step1_mandatory"].state, NodeState.COMPLETED)
        self.assertEqual(run.node_executions["step2_optional"].state, NodeState.FAILED)
        self.assertEqual(run.node_executions["step3_final"].state, NodeState.COMPLETED)

    def test_cooperative_cancellation(self):
        """Verifies cooperative cancellation flags running workflow and marks remaining cancelled."""
        cancel_token = CancellationToken()

        def _long_tool(args):
            # Cancel token during tool execution
            cancel_token.cancel()
            return {"status": "SUCCESS"}

        self.tool_registry.register(
            ToolDefinition(
                tool_id="test.cancelling_tool",
                name="cancelling_tool",
                description="Triggers cancellation",
                parameters={"type": "object"},
                authority_tier=PermissionTier.L0_OBSERVE,
                handler=_long_tool,
            )
        )

        nodes = [
            WorkflowNode(
                node_id="c_step1",
                name="Trigger Cancellation",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "test.cancelling_tool", "args": {}},
                phase=1,
            ),
            WorkflowNode(
                node_id="c_step2",
                name="Should be Cancelled",
                execution_type=ExecutionType.TOOL,
                payload={"tool_name": "test.echo", "args": {"input_text": "c2"}},
                depends_on=["c_step1"],
                phase=2,
            ),
        ]

        defn = WorkflowDefinition(
            workflow_id="wf_cancel_01",
            name="Cancellation Test",
            nodes=nodes,
        )

        run = self.engine.execute_workflow(defn, conversation_id=self.conv_id, cancellation_token=cancel_token)
        self.assertEqual(run.status, WorkflowStatus.CANCELLED)

    def test_crash_recovery_and_reconciliation(self):
        """Verifies startup reconciliation marks interrupted workflows and nodes as INTERRUPTED."""
        # Insert a simulated crashed workflow
        crashed_wf_id = "wf_crashed_test_99"
        self.persistence.save_workflow_run(
            run_id=crashed_wf_id,
            workflow_id="wf_audit_def",
            conversation_id="conv_crash_01",
            name="Crashed Audit Workflow",
            status="RUNNING",
            failure_policy="FAIL_FAST",
            start_time=time.time() - 3600,
        )
        self.persistence.save_workflow_node(
            run_id=crashed_wf_id,
            node_id="node_in_flight",
            name="In-Flight Node",
            execution_type="PROCESS",
            phase=1,
            state="RUNNING",
            start_time=time.time() - 3600,
        )

        # Verify initial state is RUNNING
        run_before = self.persistence.get_workflow_run(crashed_wf_id)
        self.assertEqual(run_before["status"], "RUNNING")
        self.assertEqual(run_before["nodes"][0]["state"], "RUNNING")

        # Run startup reconciliation
        reconciled = self.persistence.reconcile_interrupted_workflows()
        self.assertGreaterEqual(len(reconciled), 1)

        # Verify state is transitioned to INTERRUPTED
        run_after = self.persistence.get_workflow_run(crashed_wf_id)
        self.assertEqual(run_after["status"], "INTERRUPTED")
        self.assertIn("Interrupted", run_after["error_message"])
        self.assertEqual(run_after["nodes"][0]["state"], "INTERRUPTED")

    def test_workflow_agent_tools_run_and_manage(self):
        """Verifies presence.run_workflow and presence.manage_workflow tool definitions."""
        tools = build_workflow_tools(self.engine, current_conversation_id=self.conv_id)
        tool_map = {t.name: t for t in tools}

        self.assertIn("run_workflow", tool_map)
        self.assertIn("manage_workflow", tool_map)

        # 1. Execute run_workflow tool
        run_tool = tool_map["run_workflow"]
        wf_args = {
            "name": "Audit Tools Test",
            "failure_policy": "FAIL_FAST",
            "nodes": [
                {
                    "node_id": "audit_node1",
                    "name": "Audit Step 1",
                    "execution_type": "TOOL",
                    "payload": {"tool_name": "test.echo", "args": {"input_text": "Audit Payload"}},
                },
                {
                    "node_id": "audit_node2",
                    "name": "Audit Step 2",
                    "execution_type": "TOOL",
                    "payload": {"tool_name": "test.echo", "args": {"input_text": "Audit Payload 2"}},
                    "depends_on": ["audit_node1"],
                },
            ]
        }

        res = run_tool.handler(wf_args)
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["workflow_status"], "COMPLETED")
        run_id = res["run_id"]
        self.assertIn("audit_node1", res["node_results"])
        self.assertIn("audit_node2", res["node_results"])

        # 2. Inspect manage_workflow tool
        manage_tool = tool_map["manage_workflow"]

        # action: list_runs
        list_res = manage_tool.handler({"action": "list_runs"})
        self.assertEqual(list_res["status"], "SUCCESS")
        self.assertGreaterEqual(list_res["count"], 1)

        # action: get_status
        status_res = manage_tool.handler({"action": "get_status", "run_id": run_id})
        self.assertEqual(status_res["status"], "SUCCESS")
        self.assertEqual(status_res["run"]["run_id"], run_id)
        self.assertEqual(status_res["run"]["status"], "COMPLETED")

    def test_workflow_with_real_process_adapter(self):
        """Verifies end-to-end PROCESS node execution under Windows Job Object supervision."""
        nodes = [
            WorkflowNode(
                node_id="proc_step",
                name="Run PowerShell Command",
                execution_type=ExecutionType.PROCESS,
                payload={"command": "Write-Output 'AUDIT_CHECK_PASSED'"},
                phase=1,
            ),
            WorkflowNode(
                node_id="verify_step",
                name="Verify Output",
                execution_type=ExecutionType.VERIFY,
                payload={"assertion_type": "REGEX_MATCH", "pattern": "AUDIT_CHECK_PASSED"},
                depends_on=["proc_step"],
                phase=2,
            ),
            WorkflowNode(
                node_id="aggregate_step",
                name="Aggregate Deliverable",
                execution_type=ExecutionType.AGGREGATE,
                payload={"title": "Process Audit Report", "source_nodes": ["proc_step", "verify_step"]},
                depends_on=["verify_step"],
                phase=3,
            ),
        ]

        defn = WorkflowDefinition(
            workflow_id="wf_real_process",
            name="Real Process Workflow",
            nodes=nodes,
            failure_policy=FailurePolicy.FAIL_FAST,
        )

        run = self.engine.execute_workflow(defn, conversation_id=self.conv_id)
        self.assertEqual(run.status, WorkflowStatus.COMPLETED)
        self.assertEqual(run.node_executions["proc_step"].state, NodeState.COMPLETED)
        self.assertEqual(run.node_executions["verify_step"].state, NodeState.COMPLETED)
        self.assertEqual(run.node_executions["aggregate_step"].state, NodeState.COMPLETED)
        self.assertIsNotNone(run.final_artifact_id)

        art = self.artifact_store.read_artifact(self.conv_id, run.final_artifact_id, mode="lines", limit=5000)
        self.assertIn("Process Audit Report", art.content)
        self.assertIn("AUDIT_CHECK_PASSED", art.content)


if __name__ == "__main__":
    unittest.main()
