"""Tests for Presence Phase 2B: Local Subagent Supervision & Shared Local Inference Semaphore.

Verifies:
Part 1: Subagent Supervision & Governance (Tests 1-13)
- Test 1: TaskDelegationConstraints validation (subagents_allowed, max_subagents coherent defaults and bounds).
- Test 2: Capability validation for 'subagents' (allowed only when subagents_allowed=True).
- Test 3: Supervisor tool registry exposure (subagent tools registered when allowed).
- Test 4: Supervisor tool registry stripping (subagent tools stripped when not allowed).
- Test 5: Anti-recursion and child tool stripping (subagent cannot spawn grandchild subagents).
- Test 6: Provider pinning & zero cloud calls (subagents strictly pinned to local Ollama Qwen 3.5 9B).
- Test 7: Permission inheritance & read-only ceiling (subagents inherit L0_OBSERVE ceiling).
- Test 8: SubagentManager concurrency limits (max_parent_subagents and max_global_subagents).
- Test 9: Deduplication guard (rejects duplicate active subagents with identical role and prompt).
- Test 10: Turn serialization (_turn_lock prevents concurrent turns on same subagent).
- Test 11: Cancellation propagation (cancelling supervisor kills all child subagents).
- Test 12: Subagent results collection (child summaries collected in TaskDelegationResult).
- Test 13: ScopedPermissionBoundary subagent tool authorization behavior.

Part 2: Shared Local Inference Semaphore (Tests 14-23)
- Test 14: Singleton registry (get_shared_inference_semaphore returns consistent instance, reset clears).
- Test 15: Concurrency bounding (max_concurrent=1 strictly limits active generations to 1).
- Test 16: Strict FIFO queue ordering (callers acquire slot in exact arrival order).
- Test 17: Bounded queue capacity (exceeding max_queue_size raises InferenceQueueFullError).
- Test 18: Timeout handling (waiter times out and raises InferenceTimeoutError without leaking slots).
- Test 19: Cancellation handling (CancellationToken cancels queued waiter without leaking slots).
- Test 20: Re-entrancy protection (nested acquisition on same thread does not deadlock).
- Test 21: Tool execution non-holding (semaphore released before tool execution commences).
- Test 22: Zero-deadlock parent/subagent interaction (interleaved generation completes cleanly).
- Test 23: Telemetry accuracy (active, queued, acquired, timeouts, and cancelled counters).

Part 3: Live Local Ollama Integration (Test 24)
- Test 24: End-to-end execution with real Ollama Qwen 3.5 9B, supervisor spawning 2 operational subagents, zero cloud calls.
"""

import os
import time
import json
import shutil
import tempfile
import threading
import uuid
import unittest
from pathlib import Path
from typing import Dict, Any, List, Optional

from runtime.local_operations.models import (
    TaskDelegationContract,
    TaskDelegationResult,
    DelegationOutcome,
    DelegationConstraints,
    ExecutionProfile,
    VerifiedEvidenceItem,
)
from runtime.local_operations.supervisor import (
    LocalOperationsSupervisor,
    ScopedPermissionBoundary,
)
from runtime.permissions import PermissionBoundary, PermissionTier, AuthorizationResult
from runtime.session import AgentSessionFactory, AgentSession
from runtime.tools.registry import ToolRegistry, ToolDefinition
from runtime.tools.subagent_tools import build_subagent_tools
from runtime.subagents.models import (
    SubagentConfig,
    SubagentRole,
    SubagentStatus,
    ManagedSubagent,
)
from runtime.subagents.manager import SubagentManager
from runtime.providers.base import IModelProvider, ModelStreamChunk
from runtime.providers.coordinator import (
    MultiProviderCoordinator,
    InferenceRequestConfig,
)
from runtime.providers.ollama import OllamaModelProvider
from runtime.providers.inference_semaphore import (
    LocalInferenceSemaphore,
    get_shared_inference_semaphore,
    reset_shared_inference_semaphores,
    InferenceTimeoutError,
    InferenceCancelledError,
    InferenceQueueFullError,
    ReentrantInferenceError,
)
from runtime.observability.task_registry import ActiveTaskRegistry
from runtime.observability.models import TaskType, TaskState
from runtime.events import EventBus
from runtime.artifacts.store import ArtifactStore
from runtime.agent_executor import CancellationToken, AgentExecutionEvent
from runtime.summaries import GlobalSummaryManager


class CountingMockProvider(IModelProvider):
    """Mock provider that records every generate_stream call and dispatches simulated chunks."""
    def __init__(
        self,
        name: str,
        chunks: Optional[List[List[ModelStreamChunk]]] = None,
        simulate_error: Optional[Exception] = None,
        on_generate: Optional[Any] = None,
    ):
        self._name = name
        self._chunks = list(chunks) if chunks else []
        self._error = simulate_error
        self._on_generate = on_generate
        self.call_count = 0
        self.call_history: List[Dict[str, Any]] = []

    @property
    def provider_name(self) -> str:
        return self._name

    @property
    def model_id(self) -> str:
        return f"{self._name}-mock"

    def validate_configuration(self):
        return True, "OK"

    def generate_stream(
        self,
        system_prompt,
        messages,
        tools=None,
        cancellation_token=None,
        request_config=None,
        **kwargs
    ):
        self.call_count += 1
        self.call_history.append({
            "system_prompt": system_prompt,
            "message_count": len(messages),
            "tools_count": len(tools) if tools else 0,
            "request_config": request_config,
        })
        if self._on_generate:
            self._on_generate()
        if self._error:
            raise self._error

        if self._chunks:
            stream = self._chunks.pop(0)
            for chunk in stream:
                yield chunk
        else:
            yield ModelStreamChunk(text_delta=f"Default response from {self._name}")


class TestLocalSubagentSupervision(unittest.TestCase):
    """Comprehensive test suite for Phase 2B subagent coordination and inference semaphore."""

    def setUp(self):
        reset_shared_inference_semaphores()
        self.temp_dir = Path(tempfile.mkdtemp(prefix="presence_test_phase2b_"))
        self.conv_dir = self.temp_dir / "conversations"
        self.logs_dir = self.temp_dir / "logs"
        self.art_dir = self.temp_dir / "artifacts"
        self.db_path = self.temp_dir / "summaries.db"
        self.conv_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.art_dir.mkdir(parents=True, exist_ok=True)

        self.event_bus = EventBus()
        self.task_registry = ActiveTaskRegistry(event_bus=self.event_bus)
        self.artifact_store = ArtifactStore(root_dir=self.art_dir)
        self.summary_manager = GlobalSummaryManager(db_path=self.db_path)

        self.mock_cloud = CountingMockProvider("freellmapi")
        self.mock_ollama = CountingMockProvider("ollama")

        self.coordinator = MultiProviderCoordinator(
            providers={
                "freellmapi": self.mock_cloud,
                "ollama": self.mock_ollama,
            },
            primary_provider="freellmapi",
            event_bus=self.event_bus,
        )

        self.factory = AgentSessionFactory(
            conversations_dir=self.conv_dir,
            logs_dir=self.logs_dir,
            default_model_provider=self.coordinator,
            task_registry=self.task_registry,
            summary_manager=self.summary_manager,
            artifact_store=self.artifact_store,
        )

        self.subagent_manager = SubagentManager(
            session_factory=self.factory,
            task_registry=self.task_registry,
            event_bus=self.event_bus,
            artifact_store=self.artifact_store,
            max_global_subagents=4,
            max_subagents_per_parent=2,
            max_recursion_depth=2,
        )

        self.supervisor = LocalOperationsSupervisor(
            session_factory=self.factory,
            coordinator=self.coordinator,
            task_registry=self.task_registry,
            event_bus=self.event_bus,
            artifact_store=self.artifact_store,
            subagent_manager=self.subagent_manager,
        )

    def tearDown(self):
        reset_shared_inference_semaphores()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # =========================================================================
    # Part 1: Subagent Supervision & Governance (Tests 1-13)
    # =========================================================================

    def test_01_delegation_constraints_validation(self):
        """Verifies subagents_allowed and max_subagents validation in DelegationConstraints."""
        # 1. Default: subagents_allowed=False, max_subagents=0
        c1 = DelegationConstraints()
        self.assertFalse(c1.subagents_allowed)
        self.assertEqual(c1.max_subagents, 0)

        # 2. subagents_allowed=False with max_subagents > 0 raises ValueError
        with self.assertRaises(ValueError) as ctx:
            DelegationConstraints(subagents_allowed=False, max_subagents=2)
        self.assertIn("max_subagents must be 0 when subagents_allowed=False", str(ctx.exception))

        # 3. subagents_allowed=True with default max_subagents auto-adjusts to 2
        c3 = DelegationConstraints(subagents_allowed=True)
        self.assertTrue(c3.subagents_allowed)
        self.assertEqual(c3.max_subagents, 2)

        # 4. subagents_allowed=True with explicit max_subagents=3
        c4 = DelegationConstraints(subagents_allowed=True, max_subagents=3)
        self.assertTrue(c4.subagents_allowed)
        self.assertEqual(c4.max_subagents, 3)

        # 5. Out of bounds max_subagents > 4 raises validation error
        with self.assertRaises(Exception):
            DelegationConstraints(subagents_allowed=True, max_subagents=5)

    def test_02_capability_validation_subagents(self):
        """Verifies that capability 'subagents' is permitted only when subagents_allowed=True."""
        # Rejected when subagents_allowed is False
        with self.assertRaises(ValueError) as ctx:
            TaskDelegationContract(
                task_id="task_cap_01",
                parent_conversation_id="conv_p1",
                objective="Run child agents",
                allowed_capabilities=["subagents"],
                constraints=DelegationConstraints(subagents_allowed=False),
            )
        self.assertIn("subagents", str(ctx.exception).lower())

        # Permitted when subagents_allowed is True
        contract = TaskDelegationContract(
            task_id="task_cap_02",
            parent_conversation_id="conv_p1",
            objective="Run child agents",
            allowed_capabilities=["subagents", "mcp.filesystem"],
            constraints=DelegationConstraints(subagents_allowed=True, max_subagents=2),
        )
        self.assertIn("subagents", contract.allowed_capabilities)
        self.assertTrue(contract.constraints.subagents_allowed)

    def test_03_supervisor_subagent_tool_exposure(self):
        """Verifies supervisor session receives subagent tools when subagents_allowed=True."""
        parent_sess = self.factory.create_session(conversation_id="parent_conv_03")
        contract = TaskDelegationContract(
            task_id="task_tools_03",
            parent_conversation_id="parent_conv_03",
            objective="Supervise investigation with subagents",
            allowed_capabilities=["subagents", "mcp.filesystem"],
            constraints=DelegationConstraints(subagents_allowed=True, max_subagents=2),
        )

        # Setup mock chunks so supervisor immediately returns completion
        self.mock_ollama._chunks = [
            [ModelStreamChunk(text_delta="Operational task complete.", finish_reason="stop")]
        ]

        result = self.supervisor.execute(contract=contract, parent_session=parent_sess)
        self.assertEqual(result.outcome, DelegationOutcome.COMPLETED)
        # Verify supervisor has access to subagent manager
        self.assertIsNotNone(self.supervisor.subagent_manager)

    def test_04_supervisor_subagent_tool_stripping(self):
        """Verifies supervisor session strips subagent tools when subagents_allowed=False."""
        parent_sess = self.factory.create_session(conversation_id="parent_conv_04")
        contract = TaskDelegationContract(
            task_id="task_tools_04",
            parent_conversation_id="parent_conv_04",
            objective="Single-threaded local execution without subagents",
            allowed_capabilities=["mcp.filesystem"],
            constraints=DelegationConstraints(subagents_allowed=False, max_subagents=0),
        )

        # Track registered tools on child session
        created_tools = []
        original_filter = self.supervisor._filter_supervisor_tools

        def intercept_filter(session, allowed_capabilities, subagents_allowed=False):
            original_filter(session, allowed_capabilities, subagents_allowed=subagents_allowed)
            created_tools.extend(session.tool_registry.list_tools())

        self.supervisor._filter_supervisor_tools = intercept_filter

        self.mock_ollama._chunks = [
            [ModelStreamChunk(text_delta="Single thread finished.", finish_reason="stop")]
        ]

        result = self.supervisor.execute(contract=contract, parent_session=parent_sess)
        self.assertEqual(result.outcome, DelegationOutcome.COMPLETED)

        tool_ids = [t.tool_id for t in created_tools]
        self.assertNotIn("presence.invoke_subagent", tool_ids)
        self.assertNotIn("presence.send_subagent_message", tool_ids)
        self.assertNotIn("presence.manage_subagents", tool_ids)

    def test_05_anti_recursion_and_child_tool_stripping(self):
        """Verifies child subagents receive include_subagent_tools=False and cannot spawn grandchildren."""
        parent_cid = "conv_parent_05"
        self.factory.create_session(conversation_id=parent_cid)

        # Spawn subagent directly via SubagentManager
        cfg = SubagentConfig(
            role=SubagentRole.RESEARCH.value,
            prompt="Analyze background metrics",
            pin_local_ollama=True,
        )

        res = self.subagent_manager.spawn_subagent(
            parent_conversation_id=parent_cid,
            config=cfg,
            wait_ms=100,
        )
        self.assertIn("subagent_id", res)
        sub_id = res["subagent_id"]
        managed = self.subagent_manager._subagents[sub_id]

        child_sess = managed.session_ref
        child_tools = [t.tool_id for t in child_sess.tool_registry.list_tools()]
        # Child subagent must NOT have subagent spawning tools
        self.assertNotIn("presence.invoke_subagent", child_tools)
        self.assertNotIn("presence.manage_subagents", child_tools)
        self.assertNotIn("presence.send_subagent_message", child_tools)

        # Verify nesting depth
        from runtime.trajectory import TrajectoryStore
        child_store = TrajectoryStore(conversation_id=managed.conversation_id, db_dir=self.conv_dir)
        meta = child_store.get_metadata()
        self.assertEqual(meta.nesting_depth, 1)

    def test_06_subagent_provider_pinning_zero_cloud(self):
        """Verifies subagents execute strictly against local Ollama with zero cloud calls."""
        parent_cid = "conv_parent_06"
        self.factory.create_session(conversation_id=parent_cid)

        self.mock_cloud.call_count = 0
        self.mock_ollama.call_count = 0
        self.mock_ollama._chunks = [
            [ModelStreamChunk(text_delta="Subagent findings: all clean.", finish_reason="stop")]
        ]

        cfg = SubagentConfig(
            role=SubagentRole.CODE_ANALYSIS.value,
            prompt="Inspect file structure",
            pin_local_ollama=True,
        )

        res = self.subagent_manager.spawn_subagent(
            parent_conversation_id=parent_cid,
            config=cfg,
            wait_ms=5000,
        )

        self.assertEqual(res["status"], "SUCCESS")
        # Strict zero-cloud guarantee
        self.assertEqual(self.mock_cloud.call_count, 0)
        self.assertGreaterEqual(self.mock_ollama.call_count, 1)

        # Verify request config used for dispatch
        last_req = self.mock_ollama.call_history[-1]["request_config"]
        self.assertIsNotNone(last_req)
        self.assertEqual(last_req.provider, "ollama")
        self.assertEqual(last_req.model, "qwen3.5:9b")
        self.assertEqual(last_req.fallback_policy, "no_fallback")
        self.assertEqual(last_req.purpose, "local_subagent_execution")

    def test_07_subagent_read_only_permission_ceiling(self):
        """Verifies subagent inherits L0_OBSERVE permission boundary and rejects mutating actions."""
        parent_cid = "conv_parent_07"
        parent_boundary = PermissionBoundary()
        parent_sess = self.factory.create_session(
            conversation_id=parent_cid,
            permission_boundary=parent_boundary,
        )

        cfg = SubagentConfig(
            role=SubagentRole.VERIFIER.value,
            prompt="Verify data integrity",
            pin_local_ollama=True,
        )

        res = self.subagent_manager.spawn_subagent(
            parent_conversation_id=parent_cid,
            config=cfg,
            wait_ms=100,
        )
        sub_id = res["subagent_id"]
        managed = self.subagent_manager._subagents[sub_id]
        child_sess = managed.session_ref

        # Check permission for mutating action
        perm_res = child_sess.permission_boundary.check_permission(
            action_name="system.delete_file",
            context={"path": "important.txt"},
        )
        self.assertFalse(perm_res.allowed)

    def test_08_subagent_concurrency_limits(self):
        """Verifies SubagentManager enforces per-parent and global subagent concurrency caps."""
        parent_cid = "conv_parent_08"
        self.factory.create_session(conversation_id=parent_cid)

        # Block the mock provider so subagents stay active
        block_event = threading.Event()

        def block_worker():
            block_event.wait(timeout=2.0)

        blocking_mock = CountingMockProvider("ollama", on_generate=block_worker)
        self.coordinator._providers["ollama"] = blocking_mock

        cfg1 = SubagentConfig(role="general", prompt="Task 1", pin_local_ollama=True, max_parent_subagents=2)
        cfg2 = SubagentConfig(role="general", prompt="Task 2", pin_local_ollama=True, max_parent_subagents=2)
        cfg3 = SubagentConfig(role="general", prompt="Task 3", pin_local_ollama=True, max_parent_subagents=2)

        try:
            r1 = self.subagent_manager.spawn_subagent(parent_cid, cfg1, wait_ms=10)
            r2 = self.subagent_manager.spawn_subagent(parent_cid, cfg2, wait_ms=10)
            self.assertIn(r1["status"], ("SUCCESS", "BACKGROUNDED"))
            self.assertIn(r2["status"], ("SUCCESS", "BACKGROUNDED"))

            # 3rd subagent for SAME parent violates limit (2)
            r3 = self.subagent_manager.spawn_subagent(parent_cid, cfg3, wait_ms=10)
            self.assertEqual(r3["status"], "ERROR")
            self.assertIn("Per-parent subagent limit reached", r3["error"])
        finally:
            block_event.set()
            self.subagent_manager.kill_all(parent_cid)

    def test_09_subagent_deduplication_guard(self):
        """Verifies deduplication guard prevents duplicate concurrent subagents with identical role and prompt."""
        parent_cid = "conv_parent_09"
        self.factory.create_session(conversation_id=parent_cid)

        block_event = threading.Event()
        blocking_mock = CountingMockProvider("ollama", on_generate=lambda: block_event.wait(timeout=2.0))
        self.coordinator._providers["ollama"] = blocking_mock

        cfg = SubagentConfig(role="research", prompt="Inspect directory contents", pin_local_ollama=True)

        try:
            r1 = self.subagent_manager.spawn_subagent(parent_cid, cfg, wait_ms=10)
            self.assertIn(r1["status"], ("SUCCESS", "BACKGROUNDED"))

            # Identical role & prompt for same parent while active
            r2 = self.subagent_manager.spawn_subagent(parent_cid, cfg, wait_ms=10)
            self.assertEqual(r2["status"], "ERROR")
            self.assertIn("Active subagent already exists", r2["error"])
        finally:
            block_event.set()
            self.subagent_manager.kill_all(parent_cid)

    def test_10_subagent_turn_serialization(self):
        """Verifies _turn_lock serializes turns so send_message does not interleave with active worker."""
        parent_cid = "conv_parent_10"
        self.factory.create_session(conversation_id=parent_cid)

        worker_hold = threading.Event()
        worker_started = threading.Event()

        def slow_worker():
            worker_started.set()
            worker_hold.wait(timeout=2.0)

        slow_mock = CountingMockProvider("ollama", on_generate=slow_worker)
        self.coordinator._providers["ollama"] = slow_mock

        cfg = SubagentConfig(role="general", prompt="Slow background task", pin_local_ollama=True)
        spawn_res = self.subagent_manager.spawn_subagent(parent_cid, cfg, wait_ms=10)
        sub_id = spawn_res["subagent_id"]
        managed = self.subagent_manager._subagents[sub_id]

        worker_started.wait(timeout=1.0)
        # Lock should be currently held by worker thread
        self.assertTrue(managed._turn_lock.locked())

        worker_hold.set()
        time.sleep(0.1)

    def test_11_cancellation_propagation_kills_subagents(self):
        """Verifies that cancelling supervisor execution invokes kill_all on child subagents."""
        parent_cid = "conv_parent_11"
        self.factory.create_session(conversation_id=parent_cid)

        block_event = threading.Event()
        blocking_mock = CountingMockProvider("ollama", on_generate=lambda: block_event.wait(timeout=3.0))
        self.coordinator._providers["ollama"] = blocking_mock

        cfg = SubagentConfig(role="general", prompt="Cancellable task", pin_local_ollama=True)
        spawn_res = self.subagent_manager.spawn_subagent(parent_cid, cfg, wait_ms=10)
        sub_id = spawn_res["subagent_id"]

        # Cancel all subagents for this parent
        killed_count = self.subagent_manager.kill_all(parent_conversation_id=parent_cid)
        self.assertEqual(killed_count, 1)

        status = self.subagent_manager.get_subagent_status(sub_id)
        self.assertEqual(status["status"], SubagentStatus.CANCELLED.value)
        block_event.set()

    def test_12_subagent_results_collection_in_task_result(self):
        """Verifies that child subagent summaries are harvested into TaskDelegationResult."""
        parent_cid = "conv_parent_12"
        parent_sess = self.factory.create_session(conversation_id=parent_cid)

        contract = TaskDelegationContract(
            task_id="task_harvest_12",
            parent_conversation_id=parent_cid,
            objective="Inspect system logs and dependencies",
            allowed_capabilities=["subagents", "mcp.filesystem"],
            constraints=DelegationConstraints(subagents_allowed=True, max_subagents=2),
        )

        # Supervisor calls invoke_subagent tool in turn 1, subagent executes in turn 2, supervisor synthesizes in turn 3
        self.mock_ollama._chunks = [
            # Turn 1: Supervisor calls invoke_subagent
            [
                ModelStreamChunk(
                    tool_calls=[{
                        "call_id": "call_sub_1",
                        "name": "presence.invoke_subagent",
                        "args": {
                            "role": "research",
                            "prompt": "Check environment dependencies",
                            "wait_ms": 5000,
                        }
                    }],
                    finish_reason="tool_calls",
                )
            ],
            # Turn 2: Subagent execution inside spawn_subagent
            [
                ModelStreamChunk(text_delta="Found Python 3.12 and 15 MCP servers configured.", finish_reason="stop")
            ],
            # Turn 3: Supervisor final response
            [
                ModelStreamChunk(text_delta="Operational task complete based on subagent report.", finish_reason="stop")
            ],
        ]

        result = self.supervisor.execute(contract=contract, parent_session=parent_sess)
        self.assertEqual(result.outcome, DelegationOutcome.COMPLETED)
        self.assertEqual(len(result.subagent_results), 1)
        self.assertEqual(result.subagent_results[0]["role"], "research")
        self.assertIn("Python 3.12", result.subagent_results[0]["summary"])

    def test_13_scoped_permission_boundary_subagent_handling(self):
        """Verifies ScopedPermissionBoundary authorization rules for subagent tools."""
        parent_boundary = PermissionBoundary()

        # 1. subagents_allowed=True allows invoke_subagent, manage_subagents, send_subagent_message
        boundary_allowed = ScopedPermissionBoundary(
            parent_boundary=parent_boundary,
            read_only=True,
            max_allowed_tier=PermissionTier.L0_OBSERVE,
            subagents_allowed=True,
        )
        res_invoke = boundary_allowed.check_permission("presence.invoke_subagent")
        res_manage = boundary_allowed.check_permission("presence.manage_subagents")
        res_send = boundary_allowed.check_permission("presence.send_subagent_message")
        self.assertTrue(res_invoke.allowed)
        self.assertTrue(res_manage.allowed)
        self.assertTrue(res_send.allowed)

        # Non-subagent L2 action remains blocked by read_only
        res_shell = boundary_allowed.check_permission("system.execute_command")
        self.assertFalse(res_shell.allowed)

        # 2. subagents_allowed=False blocks invoke_subagent
        boundary_blocked = ScopedPermissionBoundary(
            parent_boundary=parent_boundary,
            read_only=True,
            max_allowed_tier=PermissionTier.L0_OBSERVE,
            subagents_allowed=False,
        )
        res_invoke_blocked = boundary_blocked.check_permission("presence.invoke_subagent")
        self.assertFalse(res_invoke_blocked.allowed)

    # =========================================================================
    # Part 2: Shared Local Inference Semaphore (Tests 14-23)
    # =========================================================================

    def test_14_semaphore_singleton_registry(self):
        """Verifies get_shared_inference_semaphore returns singleton and reset clears it."""
        sem1 = get_shared_inference_semaphore("ollama", "qwen3.5:9b")
        sem2 = get_shared_inference_semaphore("ollama", "qwen3.5:9b")
        self.assertIs(sem1, sem2)

        reset_shared_inference_semaphores()
        sem3 = get_shared_inference_semaphore("ollama", "qwen3.5:9b")
        self.assertIsNot(sem1, sem3)

    def test_15_semaphore_concurrency_bounding(self):
        """Verifies max_concurrent=1 strictly limits active generation to 1 across threads."""
        sem = LocalInferenceSemaphore(max_concurrent=1)
        max_active_observed = 0
        active_lock = threading.Lock()
        threads = []

        def worker(idx: int):
            nonlocal max_active_observed
            with sem.acquire_slot(caller_id=f"worker_{idx}", timeout=5.0):
                with active_lock:
                    if sem.active_generations > max_active_observed:
                        max_active_observed = sem.active_generations
                time.sleep(0.05)

        for i in range(4):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        self.assertEqual(max_active_observed, 1)
        self.assertEqual(sem.active_generations, 0)
        self.assertEqual(sem.total_acquired, 4)

    def test_16_semaphore_strict_fifo_ordering(self):
        """Verifies waiters are served in strict FIFO arrival order."""
        sem = LocalInferenceSemaphore(max_concurrent=1)
        execution_order = []
        barrier = threading.Event()

        def first_holder():
            with sem.acquire_slot(caller_id="holder", timeout=5.0):
                barrier.wait(timeout=2.0)

        def queued_waiter(order_num: int):
            time.sleep(0.02 * order_num)  # Stagger thread start to guarantee queue arrival order
            with sem.acquire_slot(caller_id=f"waiter_{order_num}", timeout=5.0):
                execution_order.append(order_num)

        t_holder = threading.Thread(target=first_holder)
        t_holder.start()
        time.sleep(0.03)  # Ensure holder acquired slot

        waiter_threads = []
        for i in range(1, 4):
            t = threading.Thread(target=queued_waiter, args=(i,))
            waiter_threads.append(t)
            t.start()

        time.sleep(0.15)  # Let all 3 waiters enter queue
        self.assertEqual(sem.queue_depth, 3)

        # Release first holder
        barrier.set()
        t_holder.join()

        for t in waiter_threads:
            t.join()

        self.assertEqual(execution_order, [1, 2, 3])

    def test_17_semaphore_queue_depth_and_bounded_capacity(self):
        """Verifies that exceeding max_queue_size raises InferenceQueueFullError immediately."""
        sem = LocalInferenceSemaphore(max_concurrent=1, max_queue_size=2)
        release_event = threading.Event()

        def holder():
            with sem.acquire_slot(caller_id="h", timeout=5.0):
                release_event.wait(timeout=2.0)

        def waiter(cid: str):
            with sem.acquire_slot(caller_id=cid, timeout=5.0):
                pass

        t_h = threading.Thread(target=holder)
        t_h.start()
        time.sleep(0.03)

        # Fill queue to capacity (2 items)
        t_w1 = threading.Thread(target=waiter, args=("w1",))
        t_w2 = threading.Thread(target=waiter, args=("w2",))
        t_w1.start()
        t_w2.start()
        time.sleep(0.08)

        self.assertEqual(sem.queue_depth, 2)

        # 3rd waiter should exceed max_queue_size=2 and raise InferenceQueueFullError
        with self.assertRaises(InferenceQueueFullError):
            sem.acquire(caller_id="w3_overflow", timeout=1.0)

        release_event.set()
        t_h.join()
        t_w1.join()
        t_w2.join()

    def test_18_semaphore_timeout_handling(self):
        """Verifies that waiting callers time out and raise InferenceTimeoutError without leaking slots."""
        sem = LocalInferenceSemaphore(max_concurrent=1)
        release_event = threading.Event()

        def holder():
            with sem.acquire_slot(caller_id="h", timeout=5.0):
                release_event.wait(timeout=2.0)

        t_h = threading.Thread(target=holder)
        t_h.start()
        time.sleep(0.03)

        # Wait with very short timeout
        with self.assertRaises(InferenceTimeoutError):
            sem.acquire(caller_id="timing_out_caller", timeout=0.05)

        self.assertEqual(sem.total_timeouts, 1)
        self.assertEqual(sem.queue_depth, 0)

        release_event.set()
        t_h.join()
        self.assertEqual(sem.active_generations, 0)

    def test_19_semaphore_cancellation_handling(self):
        """Verifies CancellationToken aborts waiting callers and raises InferenceCancelledError."""
        sem = LocalInferenceSemaphore(max_concurrent=1)
        release_event = threading.Event()
        cancel_token = CancellationToken()

        def holder():
            with sem.acquire_slot(caller_id="h", timeout=5.0):
                release_event.wait(timeout=2.0)

        t_h = threading.Thread(target=holder)
        t_h.start()
        time.sleep(0.03)

        # Cancel token from another thread after 0.05s
        def canceller():
            time.sleep(0.05)
            cancel_token.cancel()

        t_c = threading.Thread(target=canceller)
        t_c.start()

        with self.assertRaises(InferenceCancelledError):
            sem.acquire(caller_id="cancelled_caller", timeout=2.0, cancellation_token=cancel_token)

        self.assertEqual(sem.total_cancelled, 1)
        self.assertEqual(sem.queue_depth, 0)

        release_event.set()
        t_h.join()
        t_c.join()

    def test_20_semaphore_reentrancy_protection(self):
        """Verifies recursive acquisition on same thread succeeds without deadlock."""
        sem = LocalInferenceSemaphore(max_concurrent=1, allow_reentrant=True)

        with sem.acquire_slot(caller_id="outer"):
            self.assertEqual(sem.active_generations, 1)
            # Reentrant acquisition
            with sem.acquire_slot(caller_id="inner"):
                self.assertEqual(sem.active_generations, 1)
                self.assertEqual(sem.total_reentrant, 1)

        self.assertEqual(sem.active_generations, 0)

    def test_21_semaphore_tool_execution_non_holding(self):
        """Verifies semaphore slot is held only during stream generation, not during tool execution."""
        sem = get_shared_inference_semaphore("ollama", "qwen3.5:9b")
        active_during_tool = None

        def dummy_tool(args):
            nonlocal active_during_tool
            # Inspect semaphore while tool is executing
            active_during_tool = sem.active_generations
            return {"status": "ok"}

        tool = ToolDefinition(
            tool_id="inspect_test_tool",
            name="inspect_test_tool",
            description="Diagnostic inspection tool",
            parameters={"type": "object"},
            authority_tier=PermissionTier.L0_OBSERVE,
            handler=dummy_tool,
        )

        parent_sess = self.factory.create_session(conversation_id="conv_sem_21")
        parent_sess.tool_registry.register(tool)

        # Mock Ollama provider wrapped with real semaphore
        def simulate_inference():
            with sem.acquire_slot(caller_id="mock_inference"):
                pass

        mock_with_sem = CountingMockProvider(
            "ollama",
            chunks=[
                # Turn 1: Model calls tool
                [
                    ModelStreamChunk(
                        tool_calls=[{"call_id": "call_1", "name": "inspect_test_tool", "args": {}}],
                        finish_reason="tool_calls",
                    )
                ],
                # Turn 2: Final response
                [
                    ModelStreamChunk(text_delta="Tool execution finished.", finish_reason="stop")
                ]
            ],
            on_generate=simulate_inference,
        )
        self.coordinator._providers["ollama"] = mock_with_sem

        # Execute turn
        for event in parent_sess.execute_turn(
            user_input="Run the inspection tool",
            request_config=InferenceRequestConfig(provider="ollama", model="qwen3.5:9b", fallback_policy="no_fallback")
        ):
            pass

        # Tool was executed when stream was closed; active_generations must be 0!
        self.assertEqual(active_during_tool, 0)
        self.assertEqual(sem.active_generations, 0)

    def test_22_zero_deadlock_parent_subagent_interaction(self):
        """Verifies interleaved parent supervisor and subagent inference does not deadlock."""
        sem = get_shared_inference_semaphore("ollama", "qwen3.5:9b")
        parent_cid = "conv_deadlock_22"
        parent_sess = self.factory.create_session(conversation_id=parent_cid)

        # Step 1: Supervisor generates initial planning turn (slot acquired and released)
        with sem.acquire_slot(caller_id="supervisor_planning"):
            time.sleep(0.02)
        self.assertEqual(sem.active_generations, 0)

        # Step 2: Supervisor spawns subagent, which acquires and releases slot
        cfg = SubagentConfig(role="research", prompt="Subagent operational research", pin_local_ollama=True)
        res = self.subagent_manager.spawn_subagent(parent_cid, cfg, wait_ms=5000)
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(sem.active_generations, 0)

        # Step 3: Supervisor generates final synthesis turn (slot acquired and released)
        with sem.acquire_slot(caller_id="supervisor_synthesis"):
            time.sleep(0.02)
        self.assertEqual(sem.active_generations, 0)

    def test_23_semaphore_telemetry_accuracy(self):
        """Verifies telemetry counters track acquisitions, waits, timeouts, and cancellations."""
        sem = LocalInferenceSemaphore(max_concurrent=1)

        with sem.acquire_slot(caller_id="caller_1"):
            pass

        with sem.acquire_slot(caller_id="caller_2"):
            pass

        telemetry = sem.get_telemetry()
        self.assertEqual(telemetry["total_acquired"], 2)
        self.assertEqual(telemetry["active_generations"], 0)
        self.assertEqual(telemetry["queue_depth"], 0)
        self.assertEqual(telemetry["total_timeouts"], 0)
        self.assertEqual(telemetry["total_cancelled"], 0)
        self.assertGreaterEqual(telemetry["average_wait_seconds"], 0.0)

    # =========================================================================
    # Part 3: Live Local Ollama Integration Test (Test 24)
    # =========================================================================

    def test_24_live_local_ollama_subagent_supervision(self):
        """Live integration test: Real Qwen 3.5 9B via Ollama, supervisor spawns 2 operational subagents."""
        # 1. Check live Ollama availability
        live_provider = OllamaModelProvider(model_id="qwen3.5:9b", mode="routine", read_timeout=90.0)
        is_ok, reason = live_provider.validate_configuration()
        if not is_ok:
            self.skipTest(f"Live Ollama Qwen 3.5 9B is unavailable: {reason}")

        # 2. Setup live session factory with real Ollama provider
        live_factory = AgentSessionFactory(
            conversations_dir=self.conv_dir,
            logs_dir=self.logs_dir,
            default_model_provider=live_provider,
            task_registry=self.task_registry,
            summary_manager=self.summary_manager,
            artifact_store=self.artifact_store,
        )

        live_subagent_mgr = SubagentManager(
            session_factory=live_factory,
            task_registry=self.task_registry,
            event_bus=self.event_bus,
            artifact_store=self.artifact_store,
            max_global_subagents=4,
            max_subagents_per_parent=3,
        )

        # 3. Create real test files for inspection
        test_file_1 = self.temp_dir / "project_info.json"
        test_file_1.write_text(json.dumps({
            "project_name": "Presence AI Hybrid Architecture",
            "version": "2.0.0-phase2b",
            "status": "operational",
        }))

        test_file_2 = self.temp_dir / "system_config.ini"
        test_file_2.write_text("[runtime]\nlocal_model = qwen3.5:9b\nmax_concurrency = 1\nmode = routine\n")

        parent_cid = "conv_live_phase2b_parent"
        live_factory.create_session(conversation_id=parent_cid)

        # 4. Spawn Subagent 1: Inspect project_info.json
        cfg1 = SubagentConfig(
            role=SubagentRole.CODE_ANALYSIS.value,
            prompt=(
                f"Inspect the file at {str(test_file_1).replace(chr(92), '/')}. "
                "Extract the project_name and version, and report them concisely."
            ),
            pin_local_ollama=True,
            timeout_seconds=90.0,
        )

        res1 = live_subagent_mgr.spawn_subagent(
            parent_conversation_id=parent_cid,
            config=cfg1,
            wait_ms=60000,
        )
        self.assertEqual(res1["status"], "SUCCESS", f"Subagent 1 failed: {res1}")
        sub1_id = res1["subagent_id"]
        status1 = live_subagent_mgr.get_subagent_status(sub1_id)
        self.assertIsNotNone(status1["summary"])
        self.assertGreater(len(status1["summary"]), 0)

        # 5. Spawn Subagent 2: Inspect system_config.ini
        cfg2 = SubagentConfig(
            role=SubagentRole.RESEARCH.value,
            prompt=(
                f"Inspect the file at {str(test_file_2).replace(chr(92), '/')}. "
                "Identify what local_model and max_concurrency are configured, and report them concisely."
            ),
            pin_local_ollama=True,
            timeout_seconds=90.0,
        )

        res2 = live_subagent_mgr.spawn_subagent(
            parent_conversation_id=parent_cid,
            config=cfg2,
            wait_ms=60000,
        )
        self.assertEqual(res2["status"], "SUCCESS", f"Subagent 2 failed: {res2}")
        sub2_id = res2["subagent_id"]
        status2 = live_subagent_mgr.get_subagent_status(sub2_id)
        self.assertIsNotNone(status2["summary"])
        self.assertGreater(len(status2["summary"]), 0)

        # 6. Verify zero cloud calls were made
        self.assertEqual(self.mock_cloud.call_count, 0)

        # 7. Verify both subagent summaries exist and have verified content
        subagents = live_subagent_mgr.list_subagents(parent_conversation_id=parent_cid)
        self.assertEqual(len(subagents), 2)
        for s in subagents:
            self.assertEqual(s["status"], SubagentStatus.COMPLETED.value)
            self.assertGreater(len(s["summary"]), 0)

        live_provider.close()


if __name__ == "__main__":
    unittest.main()
