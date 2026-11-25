"""Tests for Phase 2A: Local Operations Supervisor Core
Verifies:
1. Contract Validation (models, constraints, capability allowlists, conflicting flags).
2. Explicit Local Provider Selection (zero cloud inference requests, strict Ollama pinning).
3. Multi-Turn Local Execution (Qwen drives multi-step tool loops locally).
4. Scoped Permission Enforcement (strict read-only enforcement blocks prohibited mutations).
5. Tool Namespace Isolation (allowlist filtering, anti-recursion guarantees).
6. Parent/Child Identity Isolation (dedicated trajectories, zero transcript contamination).
7. Deterministic Completion Verification (falsified claims rejected without execution evidence).
8. Cancellation Propagation (cooperative cancellation across supervisor and tools).
9. Timeout and Failure Handling (clean termination, no cloud fallback).
10. Large Tool Output Handling (ArtifactStore offloading).
11. Live Integration with Real Ollama Qwen 3.5 9B.
"""

import os
import time
import json
import shutil
import tempfile
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
from runtime.permissions import PermissionBoundary, PermissionTier
from runtime.session import AgentSessionFactory, AgentSession
from runtime.tools.registry import ToolRegistry, ToolDefinition
from runtime.providers.base import IModelProvider, ModelStreamChunk
from runtime.providers.coordinator import MultiProviderCoordinator, InferenceRequestConfig
from runtime.providers.ollama import OllamaModelProvider
from runtime.providers.fake import FakeModelProvider
from runtime.observability.task_registry import ActiveTaskRegistry
from runtime.observability.models import TaskType, TaskState
from runtime.events import EventBus
from runtime.artifacts.store import ArtifactStore
from runtime.agent_executor import CancellationToken
from runtime.summaries import GlobalSummaryManager


class CountingMockProvider(IModelProvider):
    """Mock provider that counts how many times generate_stream is invoked."""
    def __init__(self, name: str, chunks: Optional[List[List[ModelStreamChunk]]] = None, simulate_error: Optional[Exception] = None):
        self._name = name
        self._chunks = list(chunks) if chunks else []
        self._error = simulate_error
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

    def generate_stream(self, system_prompt, messages, tools=None, cancellation_token=None, request_config=None, **kwargs):
        self.call_count += 1
        self.call_history.append({
            "system_prompt": system_prompt,
            "message_count": len(messages),
            "tools_count": len(tools) if tools else 0,
            "request_config": request_config
        })
        if self._error:
            raise self._error

        if self._chunks:
            stream = self._chunks.pop(0)
            for chunk in stream:
                yield chunk
        else:
            yield ModelStreamChunk(text_delta=f"Default response from {self._name}")


class TestLocalOperationsSupervisor(unittest.TestCase):
    """Comprehensive test suite for Phase 2A Local Operations Supervisor."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="presence_test_local_sup_"))
        self.conv_dir = self.temp_dir / "conversations"
        self.logs_dir = self.temp_dir / "logs"
        self.art_dir = self.temp_dir / "artifacts"
        self.db_path = self.temp_dir / "summaries.db"

        self.conv_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.art_dir.mkdir(parents=True, exist_ok=True)

        self.event_bus = EventBus()
        self.task_registry = ActiveTaskRegistry(event_bus=self.event_bus)
        self.summary_mgr = GlobalSummaryManager(db_path=self.db_path)
        self.artifact_store = ArtifactStore(root_dir=self.art_dir)
        self.permissions = PermissionBoundary()

    def tearDown(self):
        self.summary_mgr.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # =========================================================================
    # Test 1: Contract Validation
    # =========================================================================
    def test_01_contract_validation(self):
        """Verifies contract validation: valid accepted, conflicting rejected,
        invalid capabilities rejected, and limits enforced.
        """
        # 1. Valid contract
        contract = TaskDelegationContract(
            task_id="task_val_001",
            parent_conversation_id="conv_parent_001",
            objective="Inspect directory and count test files.",
            context_summary="Investigating repo test layout.",
            allowed_capabilities=["mcp.filesystem", "process_inspection"],
            completion_criteria=["files_inspected"]
        )
        self.assertEqual(contract.task_id, "task_val_001")
        self.assertTrue(contract.constraints.read_only)

        # 2. Conflicting constraints rejected (read_only=True and source_modification_allowed=True)
        with self.assertRaises(ValueError) as ctx:
            TaskDelegationContract(
                task_id="task_val_002",
                parent_conversation_id="conv_parent_001",
                objective="Modify code files safely.",
                constraints=DelegationConstraints(read_only=True, source_modification_allowed=True),
                allowed_capabilities=["mcp.filesystem"]
            )
        self.assertIn("Conflicting constraints", str(ctx.exception))

        # 3. Invalid capability identifier rejected
        with self.assertRaises(ValueError) as ctx:
            TaskDelegationContract(
                task_id="task_val_003",
                parent_conversation_id="conv_parent_001",
                objective="Arbitrary task",
                allowed_capabilities=["unsupported_hardware_access"]
            )
        self.assertIn("Invalid capability identifier", str(ctx.exception))

        # 4. Phase 2B subagents capability deferred and rejected in Phase 2A
        with self.assertRaises(ValueError) as ctx:
            TaskDelegationContract(
                task_id="task_val_004",
                parent_conversation_id="conv_parent_001",
                objective="Delegate to child agents",
                allowed_capabilities=["subagents"]
            )
        self.assertIn("deferred to Phase 2B", str(ctx.exception))

        # 5. Empty objective rejected
        with self.assertRaises(Exception):
            TaskDelegationContract(
                task_id="task_val_005",
                parent_conversation_id="conv_parent_001",
                objective="",
                allowed_capabilities=["mcp.filesystem"]
            )

    # =========================================================================
    # Test 2: Explicit Local Provider Selection (Zero Cloud Inference)
    # =========================================================================
    def test_02_explicit_local_provider_selection_zero_cloud_calls(self):
        """Verifies that when FreeLLMAPI is configured as the primary cloud provider,
        the LocalOperationsSupervisor explicitly pins every inference request to Ollama,
        making EXACTLY ZERO calls to the cloud provider.
        """
        cloud_mock = CountingMockProvider(name="freellmapi")
        ollama_mock = CountingMockProvider(
            name="ollama",
            chunks=[
                [ModelStreamChunk(text_delta="Operational objective analyzed locally. Finished cleanly.")]
            ]
        )

        coordinator = MultiProviderCoordinator(
            providers={"freellmapi": cloud_mock, "ollama": ollama_mock},
            primary_provider="freellmapi",
            fallback_order=["ollama"],
            event_bus=self.event_bus
        )

        factory = AgentSessionFactory(
            conversations_dir=self.conv_dir,
            logs_dir=self.logs_dir,
            summary_manager=self.summary_mgr,
            artifact_store=self.artifact_store,
            default_model_provider=coordinator,
            task_registry=self.task_registry
        )

        supervisor = LocalOperationsSupervisor(
            session_factory=factory,
            coordinator=coordinator,
            task_registry=self.task_registry,
            event_bus=self.event_bus,
            artifact_store=self.artifact_store
        )

        contract = TaskDelegationContract(
            task_id="task_zero_cloud_002",
            parent_conversation_id="parent_conv_123",
            objective="Perform local diagnostic check.",
            allowed_capabilities=["all_read_only"],
            constraints=DelegationConstraints(read_only=True, max_tool_cycles=5)
        )

        parent_sess = factory.create_session(conversation_id=contract.parent_conversation_id)
        result = supervisor.execute(contract, parent_session=parent_sess)

        # 1. Local execution succeeded
        self.assertEqual(result.outcome, DelegationOutcome.COMPLETED)
        self.assertIn("Operational objective analyzed locally", result.summary)

        # 2. Strict Zero-Cloud Guarantee: FreeLLMAPI received 0 calls!
        self.assertEqual(cloud_mock.call_count, 0, "Cloud provider received inference calls during local supervisor execution!")

        # 3. Local Ollama model received the inference request
        self.assertEqual(ollama_mock.call_count, 1)

        # 4. Verify request_config pinned to Ollama in routine mode with no fallback
        last_req = ollama_mock.call_history[0]["request_config"]
        self.assertEqual(last_req.provider, "ollama")
        self.assertEqual(last_req.mode, "routine")
        self.assertEqual(last_req.fallback_policy, "no_fallback")
        self.assertEqual(result.metrics["cloud_inference_calls"], 0)

    # =========================================================================
    # Test 3: Multi-Turn Local Execution
    # =========================================================================
    def test_03_multi_turn_local_execution_loop(self):
        """Verifies that the supervisor independently executes a multi-turn operational
        loop: Tool 1 selected -> executed -> Tool 2 selected -> executed -> completion.
        All cycles run locally.
        """
        tool1_count = 0
        tool2_count = 0

        def _list_files(args):
            nonlocal tool1_count
            tool1_count += 1
            return {"files": ["main.py", "test_core.py", "config.json"]}

        def _read_file(args):
            nonlocal tool2_count
            tool2_count += 1
            return {"content": "def test_example(): assert True\n", "lines": 1}

        custom_registry = ToolRegistry()
        custom_registry.register(ToolDefinition(
            tool_id="mcp.filesystem.list_dir",
            name="mcp.filesystem.list_dir",
            description="List directory",
            parameters={"type": "object"},
            authority_tier=PermissionTier.L0_OBSERVE,
            handler=_list_files
        ))
        custom_registry.register(ToolDefinition(
            tool_id="mcp.filesystem.read_file",
            name="mcp.filesystem.read_file",
            description="Read file",
            parameters={"type": "object"},
            authority_tier=PermissionTier.L0_OBSERVE,
            handler=_read_file
        ))

        # Script Ollama:
        # Turn 1: Call list_dir
        # Turn 2: Call read_file
        # Turn 3: Conclude findings
        cloud_mock = CountingMockProvider(name="freellmapi")
        ollama_scripted = CountingMockProvider(
            name="ollama",
            chunks=[
                [ModelStreamChunk(tool_calls=[{"call_id": "tc1", "name": "mcp.filesystem.list_dir", "args": {}}], finish_reason="tool_calls")],
                [ModelStreamChunk(tool_calls=[{"call_id": "tc2", "name": "mcp.filesystem.read_file", "args": {"path": "test_core.py"}}], finish_reason="tool_calls")],
                [ModelStreamChunk(text_delta="Inspected directory and test_core.py. Test asserts True cleanly.", finish_reason="stop")]
            ]
        )

        coordinator = MultiProviderCoordinator(
            providers={"freellmapi": cloud_mock, "ollama": ollama_scripted},
            primary_provider="freellmapi",
            fallback_order=["ollama"],
            event_bus=self.event_bus
        )

        factory = AgentSessionFactory(
            conversations_dir=self.conv_dir,
            logs_dir=self.logs_dir,
            summary_manager=self.summary_mgr,
            artifact_store=self.artifact_store,
            default_model_provider=coordinator,
            task_registry=self.task_registry
        )

        # Inject custom tools into session creation
        orig_create = factory.create_session
        def _custom_create(*args, **kwargs):
            sess = orig_create(*args, **kwargs)
            for t in custom_registry.list_tools():
                sess.tool_registry.register(t, permission_boundary=sess.permission_boundary)
            return sess
        factory.create_session = _custom_create

        supervisor = LocalOperationsSupervisor(
            session_factory=factory,
            coordinator=coordinator,
            task_registry=self.task_registry,
            event_bus=self.event_bus,
            artifact_store=self.artifact_store
        )

        contract = TaskDelegationContract(
            task_id="task_multi_turn_003",
            parent_conversation_id="parent_conv_multi",
            objective="Inspect test files and verify assertions.",
            allowed_capabilities=["mcp.filesystem"],
            completion_criteria=["files_inspected"],
            constraints=DelegationConstraints(read_only=True, max_tool_cycles=5)
        )

        parent_sess = factory.create_session(conversation_id=contract.parent_conversation_id)
        result = supervisor.execute(contract, parent_session=parent_sess)

        # Assertions
        self.assertEqual(result.outcome, DelegationOutcome.COMPLETED)
        self.assertEqual(tool1_count, 1)
        self.assertEqual(tool2_count, 1)
        self.assertEqual(ollama_scripted.call_count, 3)
        self.assertEqual(cloud_mock.call_count, 0)
        self.assertEqual(len(result.evidence), 2)
        self.assertIn("Inspected directory and test_core.py", result.summary)

    # =========================================================================
    # Test 4: Permission Enforcement (Read-Only Blocks Mutations)
    # =========================================================================
    def test_04_scoped_permission_enforcement_blocks_mutation(self):
        """Verifies that ScopedPermissionBoundary blocks any L1/L2/L3 tool execution
        when contract enforces read_only=True, reporting outcome BLOCKED.
        """
        mutation_called = False
        def _delete_file(args):
            nonlocal mutation_called
            mutation_called = True
            return {"status": "DELETED"}

        mutation_tool = ToolDefinition(
            tool_id="mcp.filesystem.delete_file",
            name="mcp.filesystem.delete_file",
            description="Delete file from filesystem",
            parameters={"type": "object"},
            authority_tier=PermissionTier.L2_EXECUTE_REVERSIBLE,
            handler=_delete_file
        )

        ollama_tries_delete = CountingMockProvider(
            name="ollama",
            chunks=[
                [ModelStreamChunk(tool_calls=[{"call_id": "bad_tc", "name": "mcp.filesystem.delete_file", "args": {}}], finish_reason="tool_calls")],
                [ModelStreamChunk(text_delta="I attempted to delete the file but was blocked by security policy.", finish_reason="stop")]
            ]
        )

        coordinator = MultiProviderCoordinator(
            providers={"ollama": ollama_tries_delete},
            primary_provider="ollama",
            fallback_order=[]
        )

        factory = AgentSessionFactory(
            conversations_dir=self.conv_dir,
            logs_dir=self.logs_dir,
            summary_manager=self.summary_mgr,
            artifact_store=self.artifact_store,
            default_model_provider=coordinator,
            task_registry=self.task_registry
        )

        orig_create = factory.create_session
        def _custom_create(*args, **kwargs):
            sess = orig_create(*args, **kwargs)
            sess.tool_registry.register(mutation_tool, permission_boundary=sess.permission_boundary)
            return sess
        factory.create_session = _custom_create

        supervisor = LocalOperationsSupervisor(
            session_factory=factory,
            coordinator=coordinator,
            task_registry=self.task_registry,
            event_bus=self.event_bus
        )

        contract = TaskDelegationContract(
            task_id="task_permission_004",
            parent_conversation_id="parent_conv_sec",
            objective="Remove temporary files.",
            allowed_capabilities=["mcp.filesystem"],
            constraints=DelegationConstraints(read_only=True)
        )

        parent_sess = factory.create_session(conversation_id=contract.parent_conversation_id)
        result = supervisor.execute(contract, parent_session=parent_sess)

        # Assertions
        self.assertFalse(mutation_called, "Protected mutation tool was executed despite read_only=True!")
        self.assertEqual(result.outcome, DelegationOutcome.BLOCKED)
        self.assertIsNotNone(result.escalation_details)
        self.assertIn("blocked by permission policy", result.summary.lower())

    # =========================================================================
    # Test 5: Tool Namespace Isolation & Anti-Recursion
    # =========================================================================
    def test_05_tool_namespace_isolation_and_anti_recursion(self):
        """Verifies that only contract-authorized tools are present in the supervisor
        session, and presence.delegate_local_work is strictly omitted.
        """
        cloud_mock = CountingMockProvider(name="freellmapi")
        ollama_mock = CountingMockProvider(name="ollama", chunks=[[ModelStreamChunk(text_delta="OK")]])

        coordinator = MultiProviderCoordinator(
            providers={"freellmapi": cloud_mock, "ollama": ollama_mock},
            primary_provider="freellmapi",
            fallback_order=["ollama"]
        )

        factory = AgentSessionFactory(
            conversations_dir=self.conv_dir,
            logs_dir=self.logs_dir,
            summary_manager=self.summary_mgr,
            artifact_store=self.artifact_store,
            default_model_provider=coordinator
        )

        supervisor = LocalOperationsSupervisor(
            session_factory=factory,
            coordinator=coordinator
        )

        # Create session through supervisor filter
        contract = TaskDelegationContract(
            task_id="task_iso_005",
            parent_conversation_id="parent_conv_iso",
            objective="Inspect search capabilities.",
            allowed_capabilities=["mcp.code_search"]
        )

        child_sess = factory.create_session(
            conversation_id="child_test_iso_sess",
            include_mcp_tools=True
        )
        # Register a fake delegation tool and fake filesystem tool
        child_sess.tool_registry.register(ToolDefinition(
            tool_id="presence.delegate_local_work",
            name="delegate",
            description="Delegate recursively",
            parameters={},
            authority_tier=PermissionTier.L0_OBSERVE,
            handler=lambda a: {}
        ))
        child_sess.tool_registry.register(ToolDefinition(
            tool_id="mcp.filesystem.read",
            name="read",
            description="Read file",
            parameters={},
            authority_tier=PermissionTier.L0_OBSERVE,
            handler=lambda a: {}
        ))

        # Filter
        supervisor._filter_supervisor_tools(child_sess, contract.allowed_capabilities)

        # 1. Anti-recursion: presence.delegate_local_work must NOT exist
        self.assertIsNone(child_sess.tool_registry.get_tool("presence.delegate_local_work"))

        # 2. Filesystem tool removed because allowed_capabilities has only "mcp.code_search"
        self.assertIsNone(child_sess.tool_registry.get_tool("mcp.filesystem.read"))

        # 3. Default artifact reader is preserved
        self.assertIsNotNone(child_sess.tool_registry.get_tool("presence.read_artifact"))

        child_sess.close()

    # =========================================================================
    # Test 6: Parent/Child Identity Isolation
    # =========================================================================
    def test_06_parent_child_identity_isolation(self):
        """Verifies that supervisor execution uses a separate scoped session,
        retaining correct parent association without polluting the parent trajectory.
        """
        ollama_mock = CountingMockProvider(
            name="ollama",
            chunks=[[ModelStreamChunk(text_delta="Supervisor task executed cleanly.")]]
        )
        coord = MultiProviderCoordinator(providers={"ollama": ollama_mock}, primary_provider="ollama")

        factory = AgentSessionFactory(
            conversations_dir=self.conv_dir,
            logs_dir=self.logs_dir,
            summary_manager=self.summary_mgr,
            artifact_store=self.artifact_store,
            default_model_provider=coord,
            task_registry=self.task_registry
        )

        # Create parent session and record a step
        parent_sess = factory.create_session(conversation_id="parent_conv_main_006", title="Parent Session")
        list(parent_sess.execute_turn("Parent turn 1"))
        parent_steps_before = parent_sess.get_step_count()

        supervisor = LocalOperationsSupervisor(
            session_factory=factory,
            coordinator=coord,
            task_registry=self.task_registry
        )

        contract = TaskDelegationContract(
            task_id="task_ident_006",
            parent_conversation_id="parent_conv_main_006",
            objective="Child execution test",
            allowed_capabilities=["all_read_only"]
        )

        result = supervisor.execute(contract, parent_session=parent_sess)

        # 1. Result parent ID matches
        self.assertEqual(result.parent_conversation_id, "parent_conv_main_006")

        # 2. Parent trajectory was NOT polluted by child steps
        parent_steps_after = parent_sess.get_step_count()
        self.assertEqual(parent_steps_before, parent_steps_after)

        parent_sess.close()

    # =========================================================================
    # Test 7: Deterministic Completion Verification
    # =========================================================================
    def test_07_deterministic_completion_verification_rejects_unexecuted_claims(self):
        """Verifies that if Qwen claims completion in text, but required completion
        criteria (e.g. 'tests_executed') were never actually run, the completion gate
        flags the result as PARTIALLY_COMPLETED.
        """
        # Qwen claims it ran tests, but never emits a tool call
        ollama_hallucinator = CountingMockProvider(
            name="ollama",
            chunks=[
                [ModelStreamChunk(text_delta="All 45 tests executed successfully and passed 100%!")]
            ]
        )
        coord = MultiProviderCoordinator(providers={"ollama": ollama_hallucinator}, primary_provider="ollama")

        factory = AgentSessionFactory(
            conversations_dir=self.conv_dir,
            logs_dir=self.logs_dir,
            summary_manager=self.summary_mgr,
            default_model_provider=coord
        )
        supervisor = LocalOperationsSupervisor(session_factory=factory, coordinator=coord)

        contract = TaskDelegationContract(
            task_id="task_verify_007",
            parent_conversation_id="parent_conv_007",
            objective="Run test suite and report results.",
            allowed_capabilities=["process_execution"],
            completion_criteria=["tests_executed"]
        )

        parent_sess = factory.create_session(conversation_id=contract.parent_conversation_id)
        result = supervisor.execute(contract, parent_session=parent_sess)

        # Assertions
        self.assertEqual(result.outcome, DelegationOutcome.PARTIALLY_COMPLETED)
        self.assertTrue(len(result.unresolved_questions) > 0)
        self.assertIn("No test execution tool was invoked", result.unresolved_questions[0])

    # =========================================================================
    # Test 8: Cancellation Propagation
    # =========================================================================
    def test_08_cancellation_propagation(self):
        """Verifies that cancelling the cancellation token cleanly stops supervisor
        execution and marks the task CANCELLED.
        """
        token = CancellationToken()
        token.cancel()  # Pre-cancelled

        ollama_mock = CountingMockProvider(name="ollama")
        coord = MultiProviderCoordinator(providers={"ollama": ollama_mock}, primary_provider="ollama")

        factory = AgentSessionFactory(
            conversations_dir=self.conv_dir,
            logs_dir=self.logs_dir,
            summary_manager=self.summary_mgr,
            default_model_provider=coord
        )
        supervisor = LocalOperationsSupervisor(session_factory=factory, coordinator=coord)

        contract = TaskDelegationContract(
            task_id="task_cancel_008",
            parent_conversation_id="parent_conv_008",
            objective="Cancelled task",
            allowed_capabilities=["all_read_only"]
        )

        parent_sess = factory.create_session(conversation_id=contract.parent_conversation_id)
        result = supervisor.execute(contract, parent_session=parent_sess, cancellation_token=token)

        self.assertEqual(result.outcome, DelegationOutcome.CANCELLED)
        self.assertEqual(ollama_mock.call_count, 0)

    # =========================================================================
    # Test 9: Timeout and Failure Handling
    # =========================================================================
    def test_09_timeout_handling_no_cloud_fallback(self):
        """Verifies that when a local task times out, it terminates with FAILED status
        and NEVER attempts to fall back to a cloud provider.
        """
        cloud_mock = CountingMockProvider(name="freellmapi")
        class HangingProvider(IModelProvider):
            @property
            def provider_name(self): return "ollama"
            @property
            def model_id(self): return "qwen3.5:9b"
            def validate_configuration(self): return True, "OK"
            def generate_stream(self, system_prompt, messages, tools=None, cancellation_token=None, **kwargs):
                time.sleep(0.5)
                yield ModelStreamChunk(text_delta="Late")

        coord = MultiProviderCoordinator(
            providers={"freellmapi": cloud_mock, "ollama": HangingProvider()},
            primary_provider="freellmapi",
            fallback_order=["ollama"]
        )

        factory = AgentSessionFactory(
            conversations_dir=self.conv_dir,
            logs_dir=self.logs_dir,
            summary_manager=self.summary_mgr,
            default_model_provider=coord
        )
        supervisor = LocalOperationsSupervisor(session_factory=factory, coordinator=coord)

        contract = TaskDelegationContract(
            task_id="task_timeout_009",
            parent_conversation_id="parent_conv_009",
            objective="Timeout test",
            allowed_capabilities=["all_read_only"],
            constraints=DelegationConstraints(timeout_seconds=0.1)
        )

        parent_sess = factory.create_session(conversation_id=contract.parent_conversation_id)
        result = supervisor.execute(contract, parent_session=parent_sess)

        self.assertEqual(result.outcome, DelegationOutcome.FAILED)
        self.assertEqual(cloud_mock.call_count, 0, "Cloud provider was invoked on local timeout!")

    # =========================================================================
    # Test 10: Large Tool Output Handled by ArtifactStore
    # =========================================================================
    def test_10_large_tool_output_persisted_to_artifact_store(self):
        """Verifies that large tool outputs are offloaded into ArtifactStore and
        referenced cleanly in the evidence list.
        """
        def _get_massive_log(args):
            large_content = "ENTRY LOG LINE DATA\n" * 500  # ~10 KB
            # Write to artifact store directly
            art_meta = self.artifact_store.store_text_artifact(
                conversation_id="child_test_large",
                tool_call_id="call_mock_1",
                tool_name="mcp.system_diagnostics.get_logs",
                content_str=large_content
            )
            return {
                "status": "SUCCESS",
                "artifact_id": art_meta.artifact_id,
                "summary": f"Output offloaded to artifact '{art_meta.artifact_id}'"
            }

        custom_reg = ToolRegistry()
        custom_reg.register(ToolDefinition(
            tool_id="mcp.system_diagnostics.get_logs",
            name="mcp.system_diagnostics.get_logs",
            description="Get large log dump",
            parameters={"type": "object"},
            authority_tier=PermissionTier.L0_OBSERVE,
            handler=_get_massive_log
        ))

        ollama_reads_log = CountingMockProvider(
            name="ollama",
            chunks=[
                [ModelStreamChunk(tool_calls=[{"call_id": "tc_log", "name": "mcp.system_diagnostics.get_logs", "args": {}}], finish_reason="tool_calls")],
                [ModelStreamChunk(text_delta="Retrieved logs via artifact pointer. Analyzed successfully.", finish_reason="stop")]
            ]
        )

        coord = MultiProviderCoordinator(providers={"ollama": ollama_reads_log}, primary_provider="ollama")

        factory = AgentSessionFactory(
            conversations_dir=self.conv_dir,
            logs_dir=self.logs_dir,
            summary_manager=self.summary_mgr,
            artifact_store=self.artifact_store,
            default_model_provider=coord
        )
        orig_create = factory.create_session
        def _custom_create(*args, **kwargs):
            sess = orig_create(*args, **kwargs)
            for t in custom_reg.list_tools():
                sess.tool_registry.register(t, permission_boundary=sess.permission_boundary)
            return sess
        factory.create_session = _custom_create

        supervisor = LocalOperationsSupervisor(
            session_factory=factory,
            coordinator=coord,
            artifact_store=self.artifact_store
        )

        contract = TaskDelegationContract(
            task_id="task_art_010",
            parent_conversation_id="parent_conv_art",
            objective="Fetch diagnostic logs",
            allowed_capabilities=["mcp.system_diagnostics"]
        )

        parent_sess = factory.create_session(conversation_id=contract.parent_conversation_id)
        result = supervisor.execute(contract, parent_session=parent_sess)

        self.assertEqual(result.outcome, DelegationOutcome.COMPLETED)
        self.assertTrue(len(result.artifacts_created) > 0)
        self.assertIn("art_", result.artifacts_created[0])
        self.assertEqual(result.evidence[0].artifact_id, result.artifacts_created[0])

    # =========================================================================
    # Test 11: Live Qwen Integration Test with Real Ollama Daemon
    # =========================================================================
    def test_11_live_qwen_integration_with_real_ollama(self):
        """End-to-End Live Integration Test with the REAL local Ollama daemon and qwen3.5:9b model.
        Verifies:
        1. Cloud Principal provider is configured as FreeLLMAPI mock.
        2. Real local Ollama daemon on http://localhost:11434 with qwen3.5:9b is configured as 'ollama'.
        3. Supervisor executes real read-only task: inspect directory and read pyproject.toml.
        4. Real Qwen emits tool calls, Presence executes tools, and returns evidence.
        5. Zero calls to FreeLLMAPI (100% verified local execution).
        """
        real_ollama = OllamaModelProvider(model_id="qwen3.5:9b", mode="routine", think=False)
        is_ready, msg = real_ollama.validate_configuration()
        if not is_ready:
            self.skipTest(f"Ollama daemon not reachable for live test: {msg}")

        cloud_mock = CountingMockProvider(name="freellmapi")

        coordinator = MultiProviderCoordinator(
            providers={"freellmapi": cloud_mock, "ollama": real_ollama},
            primary_provider="freellmapi",
            fallback_order=["ollama"],
            event_bus=self.event_bus
        )

        # Register real safe inspection tools into session factory
        project_root = Path.cwd()

        def _list_project_files(args):
            items = [f.name for f in project_root.iterdir() if not f.name.startswith(".")]
            return {"files": items[:15], "total_count": len(items)}

        def _read_pyproject(args):
            p = project_root / "pyproject.toml"
            if p.exists():
                return {"content": p.read_text(encoding="utf-8")[:500], "status": "EXISTS"}
            return {"content": "pyproject.toml not found", "status": "NOT_FOUND"}

        custom_reg = ToolRegistry()
        custom_reg.register(ToolDefinition(
            tool_id="mcp.filesystem.list_dir",
            name="mcp.filesystem.list_dir",
            description="Lists files in project directory",
            parameters={"type": "object"},
            authority_tier=PermissionTier.L0_OBSERVE,
            handler=_list_project_files
        ))
        custom_reg.register(ToolDefinition(
            tool_id="mcp.filesystem.read_file",
            name="mcp.filesystem.read_file",
            description="Reads file in project directory",
            parameters={"type": "object"},
            authority_tier=PermissionTier.L0_OBSERVE,
            handler=_read_pyproject
        ))

        factory = AgentSessionFactory(
            conversations_dir=self.conv_dir,
            logs_dir=self.logs_dir,
            summary_manager=self.summary_mgr,
            artifact_store=self.artifact_store,
            default_model_provider=coordinator,
            task_registry=self.task_registry
        )

        orig_create = factory.create_session
        def _custom_create(*args, **kwargs):
            sess = orig_create(*args, **kwargs)
            for t in custom_reg.list_tools():
                sess.tool_registry.register(t, permission_boundary=sess.permission_boundary)
            return sess
        factory.create_session = _custom_create

        supervisor = LocalOperationsSupervisor(
            session_factory=factory,
            coordinator=coordinator,
            task_registry=self.task_registry,
            event_bus=self.event_bus,
            artifact_store=self.artifact_store
        )

        contract = TaskDelegationContract(
            task_id="task_live_qwen_011",
            parent_conversation_id="parent_conv_live",
            objective="Inspect the project directory using mcp.filesystem.list_dir and check pyproject.toml.",
            allowed_capabilities=["mcp.filesystem"],
            completion_criteria=["files_inspected"],
            constraints=DelegationConstraints(read_only=True, max_tool_cycles=5, timeout_seconds=60.0)
        )

        parent_sess = factory.create_session(conversation_id=contract.parent_conversation_id)
        start_t = time.time()
        result = supervisor.execute(contract, parent_session=parent_sess)
        duration = time.time() - start_t

        # 1. Zero Cloud Inference Calls Verified
        self.assertEqual(cloud_mock.call_count, 0, "Cloud provider was called during live Qwen operational execution!")

        # 2. Operational Execution Result Verified
        self.assertEqual(result.outcome, DelegationOutcome.COMPLETED)
        self.assertTrue(len(result.summary) > 10)
        self.assertTrue(result.metrics["tools_executed_count"] >= 1)
        self.assertTrue(len(result.evidence) >= 1)
        self.assertEqual(result.metrics["cloud_inference_calls"], 0)


if __name__ == "__main__":
    unittest.main()
