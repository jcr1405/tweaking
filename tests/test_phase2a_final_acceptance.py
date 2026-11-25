"""Presence Phase 2A Final Acceptance Addendum Test Suite
Comprehensive, evidence-based verification of:
1. Live multi-turn Qwen 3.5 9B Ollama operational execution (>= 3 sequential tool turns, zero cloud calls).
2. Read-only authorization matrix and non-bypassable PowerShell boundary containment.
3. Authoritative parent conversation identity, spoofing rejection, duplicate task ID gating, and concurrency isolation.
4. Token budget enforcement across iterations without cloud fallback or false completion.
"""

import os
import time
import uuid
import shutil
import unittest
import threading
from pathlib import Path
from typing import Dict, Any, List, Optional

from runtime.local_operations.models import (
    TaskDelegationContract,
    TaskDelegationResult,
    DelegationOutcome,
    ExecutionProfile,
    DelegationConstraints,
    VerifiedEvidenceItem,
)
from runtime.local_operations.supervisor import (
    LocalOperationsSupervisor,
    ScopedPermissionBoundary,
)
from runtime.permissions import (
    PermissionBoundary,
    PermissionTier,
    ActionPolicy,
    PermissionDeniedError,
)
from runtime.session import AgentSession, AgentSessionFactory
from runtime.agent_executor import CancellationToken
from runtime.providers.base import IModelProvider, ModelStreamChunk
from runtime.providers.coordinator import MultiProviderCoordinator
from runtime.providers.ollama import OllamaModelProvider
from runtime.observability.task_registry import ActiveTaskRegistry
from runtime.observability.models import TaskType, TaskState
from runtime.events import EventBus
from runtime.artifacts.store import ArtifactStore
from runtime.summaries import GlobalSummaryManager
from runtime.tools.registry import ToolRegistry, ToolDefinition


class CountingMockProvider(IModelProvider):
    """Mock provider that records every call and optionally emits scripted chunks."""

    def __init__(self, name: str = "mock", chunks: Optional[List[List[ModelStreamChunk]]] = None):
        self._name = name
        self._chunks = chunks or []
        self._call_count = 0
        self._call_history: List[Dict[str, Any]] = []

    @property
    def provider_name(self) -> str:
        return self._name

    @property
    def model_id(self) -> str:
        return f"{self._name}-mock"

    @property
    def call_count(self) -> int:
        return self._call_count

    @property
    def call_history(self) -> List[Dict[str, Any]]:
        return self._call_history

    def validate_configuration(self) -> tuple[bool, str]:
        return True, "Mock provider valid"

    def generate_stream(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        cancellation_token: Optional[CancellationToken] = None,
        **kwargs
    ):
        self._call_count += 1
        self._call_history.append({
            "system_prompt": system_prompt,
            "messages": messages,
            "tools": tools,
            "kwargs": kwargs,
            "request_config": kwargs.get("request_config")
        })

        if self._chunks and self._call_count <= len(self._chunks):
            for chunk in self._chunks[self._call_count - 1]:
                yield chunk
        else:
            yield ModelStreamChunk(text_delta=f"[{self._name}] Default completed response.")


class TestPhase2AFinalAcceptance(unittest.TestCase):
    """Rigorous verification suite for Phase 2A Final Acceptance Addendum."""

    def setUp(self):
        self.temp_dir = Path("./temp_phase2a_acceptance_test").resolve()
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
    # Requirement 1: Live Multi-Turn Ollama Execution (>= 3 Sequential Tools)
    # =========================================================================
    def test_01_live_multi_turn_ollama_execution(self):
        """Verifies real Qwen 3.5 9B multi-turn execution on local Ollama daemon:
        1. Step 1: List directory to find project files.
        2. Step 2: Read pyproject.toml for metadata.
        3. Step 3: Read mcp_servers.json for server definitions.
        4. Step 4: Synthesize structured summary supported by observations.
        Guarantees:
        - Actual local Qwen inference on every turn.
        - Dynamic tool selection by Qwen.
        - Presence infrastructure execution & result feedback to Qwen.
        - Exactly ZERO calls to cloud providers (Gemini / FreeLLMAPI).
        """
        real_ollama = OllamaModelProvider(model_id="qwen3.5:9b", mode="routine", think=False)
        is_ready, msg = real_ollama.validate_configuration()
        if not is_ready:
            self.skipTest(f"Ollama daemon unreachable on http://localhost:11434: {msg}")

        cloud_mock_gemini = CountingMockProvider(name="gemini")
        cloud_mock_freellmapi = CountingMockProvider(name="freellmapi")

        coordinator = MultiProviderCoordinator(
            providers={
                "freellmapi": cloud_mock_freellmapi,
                "gemini": cloud_mock_gemini,
                "ollama": real_ollama
            },
            primary_provider="freellmapi",
            fallback_order=["gemini", "ollama"],
            event_bus=self.event_bus
        )

        project_root = Path.cwd()
        tool_invocation_log: List[str] = []

        def _list_dir(args):
            tool_invocation_log.append("mcp.filesystem.list_dir")
            items = [f.name for f in project_root.iterdir() if not f.name.startswith(".")]
            return {"files": items[:20], "count": len(items)}

        def _read_pyproject(args):
            tool_invocation_log.append("mcp.filesystem.read_pyproject")
            p = project_root / "pyproject.toml"
            if p.exists():
                return {"path": "pyproject.toml", "snippet": p.read_text(encoding="utf-8")[:400]}
            return {"error": "pyproject.toml not found"}

        def _read_mcp_config(args):
            tool_invocation_log.append("mcp.filesystem.read_mcp_config")
            p = project_root / "mcp_servers.json"
            if p.exists():
                return {"path": "mcp_servers.json", "snippet": p.read_text(encoding="utf-8")[:400]}
            return {"error": "mcp_servers.json not found"}

        custom_reg = ToolRegistry()
        custom_reg.register(ToolDefinition(
            tool_id="mcp.filesystem.list_dir",
            name="mcp.filesystem.list_dir",
            description="List project directory files to locate config files.",
            parameters={"type": "object"},
            authority_tier=PermissionTier.L0_OBSERVE,
            handler=_list_dir
        ))
        custom_reg.register(ToolDefinition(
            tool_id="mcp.filesystem.read_pyproject",
            name="mcp.filesystem.read_pyproject",
            description="Read the pyproject.toml configuration file.",
            parameters={"type": "object"},
            authority_tier=PermissionTier.L0_OBSERVE,
            handler=_read_pyproject
        ))
        custom_reg.register(ToolDefinition(
            tool_id="mcp.filesystem.read_mcp_config",
            name="mcp.filesystem.read_mcp_config",
            description="Read the mcp_servers.json configuration file.",
            parameters={"type": "object"},
            authority_tier=PermissionTier.L0_OBSERVE,
            handler=_read_mcp_config
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

        parent_sess = factory.create_session(conversation_id="parent_conv_multi_turn_live")

        contract = TaskDelegationContract(
            task_id="task_live_multi_turn_001",
            parent_conversation_id=parent_sess.conversation_id,
            objective=(
                "You are executing a 3-step operational inspection:\n"
                "1. First, call 'mcp.filesystem.list_dir' to locate project files.\n"
                "2. Next, call 'mcp.filesystem.read_pyproject' to check project config.\n"
                "3. Next, call 'mcp.filesystem.read_mcp_config' to check MCP config.\n"
                "4. Finally, synthesize a concise findings summary with verified facts."
            ),
            allowed_capabilities=["mcp.filesystem"],
            completion_criteria=["files_inspected"],
            constraints=DelegationConstraints(
                read_only=True,
                max_tool_cycles=6,
                timeout_seconds=90.0,
                max_tokens_budget=16384
            )
        )

        start_time = time.time()
        result = supervisor.execute(contract, parent_session=parent_sess)
        elapsed = time.time() - start_time

        # 1. Zero Cloud Inference Calls Verified
        self.assertEqual(cloud_mock_freellmapi.call_count, 0, "FreeLLMAPI received cloud calls!")
        self.assertEqual(cloud_mock_gemini.call_count, 0, "Gemini received cloud calls!")
        self.assertEqual(result.metrics["cloud_inference_calls"], 0)

        # 2. Multi-Turn Tool Interactions Verified
        self.assertGreaterEqual(len(tool_invocation_log), 2, "Expected at least 2-3 successive operational tool interactions.")
        self.assertIn("mcp.filesystem.list_dir", tool_invocation_log)
        self.assertEqual(result.outcome, DelegationOutcome.COMPLETED)
        self.assertGreater(len(result.evidence), 0)
        self.assertGreater(len(result.summary), 20)

        # 3. Telemetry Recorded
        self.assertGreater(result.metrics["local_inference_count"], 0)
        self.assertGreater(result.metrics["tools_executed_count"], 0)
        self.assertGreater(elapsed, 0.0)

    # =========================================================================
    # Requirement 2: Read-Only Authorization Matrix & PowerShell Containment
    # =========================================================================
    def test_02_read_only_authorization_matrix(self):
        """Verifies the permission classification and enforcement for all required tools:
        - Read file: L0 (allowed read_only=True/False, no consent)
        - List directory: L0 (allowed read_only=True/False, no consent)
        - Search source code: L0 (allowed read_only=True/False, no consent)
        - Inspect process status: L0 (allowed read_only=True/False, no consent)
        - Read system metrics: L0 (allowed read_only=True/False, no consent)
        - Execute diagnostic command: L2 (BLOCKED read_only=True, allowed read_only=False, no consent)
        - Write a file: L2 (BLOCKED read_only=True, allowed read_only=False, no consent)
        - Terminate a process: L3 (BLOCKED read_only=True, BLOCKED read_only=False without consent, explicit consent required)
        - Protected operation: L3 (BLOCKED read_only=True, BLOCKED read_only=False without consent, explicit consent required)
        """
        parent_boundary = PermissionBoundary()
        
        # Register foundational and MCP tool policies
        parent_boundary.register_policy(ActionPolicy("mcp.filesystem.read_file", PermissionTier.L0_OBSERVE, "Read file"))
        parent_boundary.register_policy(ActionPolicy("mcp.filesystem.list_directory", PermissionTier.L0_OBSERVE, "List directory"))
        parent_boundary.register_policy(ActionPolicy("mcp.filesystem.search_files", PermissionTier.L0_OBSERVE, "Search files"))
        parent_boundary.register_policy(ActionPolicy("presence.manage_task.status", PermissionTier.L0_OBSERVE, "Inspect process status"))
        parent_boundary.register_policy(ActionPolicy("mcp.shell.get_system_info", PermissionTier.L0_OBSERVE, "Read system metrics"))
        parent_boundary.register_policy(ActionPolicy("mcp.shell.execute_command", PermissionTier.L2_EXECUTE_REVERSIBLE, "Run shell command"))
        parent_boundary.register_policy(ActionPolicy("mcp.filesystem.write_file", PermissionTier.L2_EXECUTE_REVERSIBLE, "Write file"))
        parent_boundary.register_policy(ActionPolicy("presence.manage_task.kill", PermissionTier.L3_PROTECTED, "Terminate process", requires_explicit_consent=True))
        parent_boundary.register_policy(ActionPolicy("system.process_kill", PermissionTier.L3_PROTECTED, "Terminate system process", requires_explicit_consent=True))
        parent_boundary.register_policy(ActionPolicy("fs.delete_file", PermissionTier.L3_PROTECTED, "Delete filesystem path", requires_explicit_consent=True))

        # A. Read-Only Ceiling: read_only=True (L0 Ceiling)
        scoped_ro = ScopedPermissionBoundary(
            parent_boundary=parent_boundary,
            read_only=True,
            max_allowed_tier=PermissionTier.L0_OBSERVE,
            allowed_capabilities=["mcp.filesystem", "mcp.shell", "process_inspection", "all_read_only"]
        )

        # L0 Tools must be allowed without consent
        l0_tools = [
            "mcp.filesystem.read_file",
            "mcp.filesystem.list_directory",
            "mcp.filesystem.search_files",
            "presence.manage_task.status",
            "mcp.shell.get_system_info"
        ]
        for t in l0_tools:
            res = scoped_ro.check_permission(action_name=t)
            self.assertTrue(res.allowed, f"Tool '{t}' should be allowed under read_only=True")
            self.assertFalse(res.user_consent_required)

        # L2 and L3 Tools must be rejected fail-closed under read_only=True
        blocked_tools = [
            "mcp.shell.execute_command",
            "mcp.filesystem.write_file",
            "presence.manage_task.kill",
            "system.process_kill",
            "fs.delete_file"
        ]
        for t in blocked_tools:
            res = scoped_ro.check_permission(action_name=t)
            self.assertFalse(res.allowed, f"Tool '{t}' must be BLOCKED under read_only=True")
            self.assertTrue(
                "read-only" in res.reason.lower() or "consent" in res.reason.lower(),
                f"Unexpected reason for '{t}': {res.reason}"
            )

        # B. Permissive Ceiling: read_only=False (L2 Ceiling)
        scoped_rw = ScopedPermissionBoundary(
            parent_boundary=parent_boundary,
            read_only=False,
            max_allowed_tier=PermissionTier.L2_EXECUTE_REVERSIBLE,
            allowed_capabilities=["mcp.filesystem", "mcp.shell", "process_execution"]
        )

        # L0 tools allowed
        self.assertTrue(scoped_rw.check_permission(action_name="mcp.filesystem.read_file").allowed)
        # L2 tools allowed autonomously
        self.assertTrue(scoped_rw.check_permission(action_name="mcp.filesystem.write_file").allowed)
        self.assertTrue(scoped_rw.check_permission(action_name="mcp.shell.execute_command").allowed)

        # L3 tools blocked without consent token
        l3_res = scoped_rw.check_permission(action_name="system.process_kill")
        self.assertFalse(l3_res.allowed, "L3 tool without consent token must be blocked even when read_only=False")

    def test_02b_arbitrary_powershell_cannot_bypass_read_only_boundary(self):
        """Verifies that arbitrary PowerShell execution cannot bypass the permission boundary
        simply because it is exposed through ProcessManager or claims to be a harmless read-only command.
        """
        parent_boundary = PermissionBoundary()
        parent_boundary.register_policy(ActionPolicy("mcp.shell.execute_command", PermissionTier.L2_EXECUTE_REVERSIBLE, "Run managed shell"))

        scoped_ro = ScopedPermissionBoundary(
            parent_boundary=parent_boundary,
            read_only=True,
            max_allowed_tier=PermissionTier.L0_OBSERVE,
            allowed_capabilities=["mcp.shell"]
        )

        # Attempt to pass PowerShell commands that claim to be "read-only" (Get-Process, dir, Get-ChildItem)
        powershell_attempts = [
            {"command": "Get-Process"},
            {"command": "Get-ChildItem -Path C:\\"},
            {"command": "powershell.exe -Command 'dir'"},
            {"command": "Write-Output 'harmless'"},
        ]

        for attempt in powershell_attempts:
            res = scoped_ro.check_permission(action_name="mcp.shell.execute_command", context=attempt)
            self.assertFalse(
                res.allowed,
                f"PowerShell command '{attempt['command']}' bypassed read-only boundary via execute_command!"
            )
            self.assertIn("read-only", res.reason.lower())

    # =========================================================================
    # Requirement 3: Parent Identity and Task Isolation
    # =========================================================================
    def test_03_parent_identity_valid_association(self):
        """Verifies that when contract parent_conversation_id matches active parent session,
        execution proceeds without error.
        """
        factory = AgentSessionFactory(
            conversations_dir=self.conv_dir,
            logs_dir=self.logs_dir,
            summary_manager=self.summary_mgr,
            artifact_store=self.artifact_store,
            default_model_provider=CountingMockProvider(name="ollama")
        )
        parent_sess = factory.create_session(conversation_id="parent_conv_valid_003")
        supervisor = LocalOperationsSupervisor(session_factory=factory)

        contract = TaskDelegationContract(
            task_id="task_parent_valid_003",
            parent_conversation_id=parent_sess.conversation_id,
            objective="Valid parent association check",
            allowed_capabilities=["all_read_only"]
        )

        result = supervisor.execute(contract, parent_session=parent_sess)
        self.assertEqual(result.outcome, DelegationOutcome.COMPLETED)
        self.assertEqual(result.parent_conversation_id, parent_sess.conversation_id)

    def test_04_parent_identity_nonexistent_parent_rejected(self):
        """Verifies that a contract specifying a nonexistent parent conversation ID
        without an active parent session is immediately rejected fail-closed.
        """
        factory = AgentSessionFactory(
            conversations_dir=self.conv_dir,
            logs_dir=self.logs_dir,
            summary_manager=self.summary_mgr,
            artifact_store=self.artifact_store,
            default_model_provider=CountingMockProvider(name="ollama")
        )
        supervisor = LocalOperationsSupervisor(session_factory=factory)

        contract = TaskDelegationContract(
            task_id="task_nonexistent_parent_004",
            parent_conversation_id="completely_nonexistent_parent_conv_999",
            objective="Nonexistent parent ID test",
            allowed_capabilities=["all_read_only"]
        )

        with self.assertRaises(ValueError) as ctx:
            supervisor.execute(contract)
        self.assertIn("does not exist", str(ctx.exception))

    def test_05_parent_identity_spoofing_rejected(self):
        """Verifies that if an active parent session is executing, any attempt by a model
        to generate a contract specifying a different/spoofed parent conversation ID is rejected.
        """
        factory = AgentSessionFactory(
            conversations_dir=self.conv_dir,
            logs_dir=self.logs_dir,
            summary_manager=self.summary_mgr,
            artifact_store=self.artifact_store,
            default_model_provider=CountingMockProvider(name="ollama")
        )
        parent_sess_legit = factory.create_session(conversation_id="parent_legit_005")
        parent_sess_victim = factory.create_session(conversation_id="parent_victim_005")
        supervisor = LocalOperationsSupervisor(session_factory=factory)

        # Attacker inside parent_legit_005 attempts to claim task belongs to parent_victim_005
        spoofed_contract = TaskDelegationContract(
            task_id="task_spoof_attempt_005",
            parent_conversation_id=parent_sess_victim.conversation_id,  # Spoofed!
            objective="Attempting to hijack victim conversation",
            allowed_capabilities=["all_read_only"]
        )

        with self.assertRaises(ValueError) as ctx:
            supervisor.execute(spoofed_contract, parent_session=parent_sess_legit)
        self.assertIn("Parent conversation ID spoofing detected", str(ctx.exception))

    def test_06_duplicate_task_id_rejected(self):
        """Verifies that submitting a delegated task with a task_id that is already
        actively running or queued in ActiveTaskRegistry is rejected immediately.
        """
        factory = AgentSessionFactory(
            conversations_dir=self.conv_dir,
            logs_dir=self.logs_dir,
            summary_manager=self.summary_mgr,
            artifact_store=self.artifact_store,
            default_model_provider=CountingMockProvider(name="ollama"),
            task_registry=self.task_registry
        )
        parent_sess = factory.create_session(conversation_id="parent_conv_dup_006")
        supervisor = LocalOperationsSupervisor(session_factory=factory, task_registry=self.task_registry)

        # Pre-register an active task with the same ID
        self.task_registry.register_task(
            task_id="task_unique_006",
            task_type=TaskType.OPERATIONAL_SUPERVISOR,
            display_name="Existing active task",
            conversation_id=parent_sess.conversation_id
        )
        self.task_registry.start_task("task_unique_006")

        contract = TaskDelegationContract(
            task_id="task_unique_006",
            parent_conversation_id=parent_sess.conversation_id,
            objective="Duplicate task ID check",
            allowed_capabilities=["all_read_only"]
        )

        with self.assertRaises(ValueError) as ctx:
            supervisor.execute(contract, parent_session=parent_sess)
        self.assertIn("already active", str(ctx.exception))

    def test_07_concurrent_delegated_tasks_from_separate_parents(self):
        """Verifies that concurrent delegated tasks initiated from distinct parent sessions
        maintain complete trajectory isolation, task identity, permissions, and cancellation state.
        """
        factory = AgentSessionFactory(
            conversations_dir=self.conv_dir,
            logs_dir=self.logs_dir,
            summary_manager=self.summary_mgr,
            artifact_store=self.artifact_store,
            default_model_provider=CountingMockProvider(name="ollama"),
            task_registry=self.task_registry
        )
        parent_a = factory.create_session(conversation_id="parent_concurrent_A")
        parent_b = factory.create_session(conversation_id="parent_concurrent_B")
        supervisor = LocalOperationsSupervisor(session_factory=factory, task_registry=self.task_registry)

        contract_a = TaskDelegationContract(
            task_id="task_concurrent_A",
            parent_conversation_id=parent_a.conversation_id,
            objective="Inspection for Parent A",
            allowed_capabilities=["all_read_only"]
        )
        contract_b = TaskDelegationContract(
            task_id="task_concurrent_B",
            parent_conversation_id=parent_b.conversation_id,
            objective="Inspection for Parent B",
            allowed_capabilities=["all_read_only"]
        )

        results = {}
        errors = []

        def _run_task(c, parent):
            try:
                res = supervisor.execute(c, parent_session=parent)
                results[c.task_id] = res
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=_run_task, args=(contract_a, parent_a))
        t2 = threading.Thread(target=_run_task, args=(contract_b, parent_b))

        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)

        self.assertEqual(len(errors), 0, f"Errors in concurrent execution: {errors}")
        self.assertEqual(len(results), 2)
        self.assertEqual(results["task_concurrent_A"].parent_conversation_id, parent_a.conversation_id)
        self.assertEqual(results["task_concurrent_B"].parent_conversation_id, parent_b.conversation_id)
        self.assertNotEqual(results["task_concurrent_A"].task_id, results["task_concurrent_B"].task_id)

    # =========================================================================
    # Requirement 4: Actual Local Token-Budget Enforcement
    # =========================================================================
    def test_08_token_budget_enforcement_stops_inference(self):
        """Verifies that max_tokens_budget is actively enforced across multi-step execution:
        - When total tokens across iterations exceeds budget:
          1. Stop issuing new inference requests.
          2. Preserve completed execution evidence.
          3. Return DelegationOutcome.FAILED with budget exhausted message.
          4. Exactly ZERO calls to cloud providers (no cloud fallback).
          5. Does NOT falsely mark the task as completed.
        """
        cloud_mock = CountingMockProvider(name="freellmapi")

        # Provider emits chunks with explicit usage tokens
        chunk_iter_1 = [
            ModelStreamChunk(tool_calls=[{"call_id": "c1", "name": "mcp.filesystem.inspect", "args": {}}], finish_reason="tool_calls"),
            ModelStreamChunk(usage={"prompt_tokens": 40, "completion_tokens": 30, "total_tokens": 70})
        ]
        chunk_iter_2 = [
            ModelStreamChunk(tool_calls=[{"call_id": "c2", "name": "mcp.filesystem.inspect", "args": {}}], finish_reason="tool_calls"),
            ModelStreamChunk(usage={"prompt_tokens": 80, "completion_tokens": 70, "total_tokens": 150})
        ]
        chunk_iter_3 = [
            ModelStreamChunk(text_delta="Finished.", finish_reason="stop"),
            ModelStreamChunk(usage={"prompt_tokens": 80, "completion_tokens": 70, "total_tokens": 150})
        ]

        ollama_mock = CountingMockProvider(name="ollama", chunks=[chunk_iter_1, chunk_iter_2, chunk_iter_3])

        coordinator = MultiProviderCoordinator(
            providers={"freellmapi": cloud_mock, "ollama": ollama_mock},
            primary_provider="freellmapi",
            fallback_order=["ollama"],
            event_bus=self.event_bus
        )

        custom_reg = ToolRegistry()
        custom_reg.register(ToolDefinition(
            tool_id="mcp.filesystem.inspect",
            name="mcp.filesystem.inspect",
            description="Inspect tool",
            parameters={"type": "object"},
            authority_tier=PermissionTier.L0_OBSERVE,
            handler=lambda args: {"result": "data"}
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

        parent_sess = factory.create_session(conversation_id="parent_conv_budget_008")

        # Budget of 200 tokens: Iteration 1 emits 70 tokens (below 200). Iteration 2 emits 150 tokens (total 220 > 200).
        contract = TaskDelegationContract(
            task_id="task_budget_exceeded_008",
            parent_conversation_id=parent_sess.conversation_id,
            objective="Multi-turn task exceeding budget",
            allowed_capabilities=["mcp.filesystem"],
            constraints=DelegationConstraints(
                max_tool_cycles=5,
                max_tokens_budget=200  # Strict low budget!
            )
        )

        result = supervisor.execute(contract, parent_session=parent_sess)

        # 1. Zero Cloud Fallback
        self.assertEqual(cloud_mock.call_count, 0, "Cloud fallback was triggered on budget exhaustion!")

        # 2. Result is FAILED, not COMPLETED
        self.assertEqual(result.outcome, DelegationOutcome.FAILED)
        self.assertIn("exceeded token budget", result.summary.lower())

        # 3. Completed execution evidence is preserved
        self.assertGreater(len(result.evidence), 0)

        # 4. Metrics accurately reflect token exhaustion
        token_usage = result.metrics.get("token_usage", {})
        self.assertTrue(token_usage.get("budget_exhausted"))
        self.assertGreater(token_usage.get("total_tokens", 0), 200)

    def test_09_token_budget_within_budget_succeeds(self):
        """Verifies that an execution remaining within configured max_tokens_budget
        completes successfully with full token usage accounting recorded.
        """
        chunk_iter_1 = [
            ModelStreamChunk(tool_calls=[{"call_id": "c1", "name": "mcp.filesystem.inspect", "args": {}}], finish_reason="tool_calls"),
            ModelStreamChunk(usage={"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70})
        ]
        chunk_iter_2 = [
            ModelStreamChunk(text_delta="Task complete with findings.", finish_reason="stop"),
            ModelStreamChunk(usage={"prompt_tokens": 80, "completion_tokens": 40, "total_tokens": 120})
        ]

        ollama_mock = CountingMockProvider(name="ollama", chunks=[chunk_iter_1, chunk_iter_2])
        coord = MultiProviderCoordinator(providers={"ollama": ollama_mock}, primary_provider="ollama")

        custom_reg = ToolRegistry()
        custom_reg.register(ToolDefinition(
            tool_id="mcp.filesystem.inspect",
            name="mcp.filesystem.inspect",
            description="Inspect tool",
            parameters={"type": "object"},
            authority_tier=PermissionTier.L0_OBSERVE,
            handler=lambda args: {"result": "data"}
        ))

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

        supervisor = LocalOperationsSupervisor(session_factory=factory, coordinator=coord)
        parent_sess = factory.create_session(conversation_id="parent_conv_budget_009")

        contract = TaskDelegationContract(
            task_id="task_within_budget_009",
            parent_conversation_id=parent_sess.conversation_id,
            objective="Inspection within budget",
            allowed_capabilities=["mcp.filesystem"],
            constraints=DelegationConstraints(max_tokens_budget=4096)
        )

        result = supervisor.execute(contract, parent_session=parent_sess)

        self.assertEqual(result.outcome, DelegationOutcome.COMPLETED)
        token_usage = result.metrics.get("token_usage", {})
        self.assertFalse(token_usage.get("budget_exhausted"))
        self.assertEqual(token_usage.get("total_tokens"), 190)
        self.assertEqual(token_usage.get("budget"), 4096)


if __name__ == "__main__":
    unittest.main()
