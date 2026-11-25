"""Comprehensive Test Suite for Phase 2D: Cloud Principal Local Delegation Tool.
Verifies canonical tool identity, registration isolation, argument validation,
trusted contract construction, end-to-end cloud delegation lifecycle, zero cloud calls
during local execution, idempotency, cancellation, timeout, context budget gating,
credential sanitization, and live Ollama Qwen integration.
"""

import os
import re
import json
import time
import shutil
import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Dict, Any, List, Optional
from unittest.mock import MagicMock

from runtime.artifacts.store import ArtifactStore
from runtime.permissions import PermissionTier, PermissionBoundary
from runtime.trajectory import TrajectoryStore, ConversationMetadata, Step, StepType, StepStatus
from runtime.agent_executor import AgentExecutor, CancellationToken
from runtime.session import AgentSession, AgentSessionFactory
from runtime.providers import IModelProvider, ModelStreamChunk
from runtime.providers.fake import FakeModelProvider
from runtime.tools.registry import ToolRegistry, ToolDefinition
from runtime.local_operations.models import (
    TaskDelegationContract,
    TaskDelegationResult,
    DelegationOutcome,
    ExecutionProfile,
    DelegationConstraints,
    OutcomeStatus,
    outcome_to_status,
    VerifiedFinding,
    ModelInterpretation,
    Contradiction,
    FailureRecord,
    SubagentReceipt,
    ArtifactReference,
    EscalationRequirement,
    CompactionMetrics,
    CloudReturnPacket,
)
from runtime.local_operations.supervisor import LocalOperationsSupervisor, ScopedPermissionBoundary
from runtime.tools.delegation_tools import (
    build_delegate_local_work_tool,
    DelegateLocalWorkArgs,
    DELEGATE_LOCAL_WORK_DESCRIPTION,
    DELEGATE_LOCAL_WORK_SCHEMA,
    current_tool_call_id,
    current_conversation_id,
    current_cancellation_token,
)


class CountingMockCloudProvider(IModelProvider):
    """Cloud provider mock that tracks invocations and generates scripted tool calls."""

    def __init__(self, name: str = "mock_gemini", model_id: str = "gemini-1.5-pro"):
        self._name = name
        self._model_id = model_id
        self.call_count = 0
        self.call_history: List[Dict[str, Any]] = []
        self._queued_chunks: List[List[ModelStreamChunk]] = []

    @property
    def provider_name(self) -> str:
        return self._name

    @property
    def model_id(self) -> str:
        return self._model_id

    def queue_response(self, chunks: List[ModelStreamChunk]) -> None:
        self._queued_chunks.append(chunks)

    def validate_configuration(self):
        return True, "OK"

    def generate_stream(self, system_prompt: str = "", messages: Any = None, prompt: Any = None, **kwargs):
        self.call_count += 1
        self.call_history.append({
            "system_prompt": system_prompt,
            "messages": messages,
            "prompt": prompt,
            "kwargs": kwargs,
        })
        if self._queued_chunks:
            chunks = self._queued_chunks.pop(0)
            for c in chunks:
                yield c
        else:
            yield ModelStreamChunk(text_delta="Default mock cloud response.")


class MockSupervisor:
    """Mock LocalOperationsSupervisor that returns deterministic CloudReturnPackets."""

    def __init__(self, outcome=OutcomeStatus.COMPLETED, is_handoff_ready=True):
        self.outcome = outcome
        self.is_handoff_ready = is_handoff_ready
        self.last_contract: Optional[TaskDelegationContract] = None
        self.execution_count = 0
        self.should_raise: Optional[Exception] = None
        self.return_none_packet = False

    def execute(self, contract: TaskDelegationContract, parent_session: Any, cancellation_token: Any = None):
        self.execution_count += 1
        self.last_contract = contract

        status_outcome = (
            outcome_to_status(self.outcome)
            if isinstance(self.outcome, DelegationOutcome)
            else self.outcome
        )
        del_outcome = (
            DelegationOutcome[self.outcome.name]
            if isinstance(self.outcome, OutcomeStatus)
            else self.outcome
        )

        if self.should_raise:
            raise self.should_raise

        if cancellation_token and cancellation_token.is_cancelled:
            return TaskDelegationResult(
                task_id=contract.task_id,
                parent_conversation_id=contract.parent_conversation_id,
                outcome=DelegationOutcome.FAILED,
                summary="Execution cancelled.",
            )

        if self.return_none_packet:
            return TaskDelegationResult(
                task_id=contract.task_id,
                parent_conversation_id=contract.parent_conversation_id,
                outcome=del_outcome,
                summary="No packet result.",
                return_packet=None,
            )

        packet = CloudReturnPacket(
            task_id=contract.task_id,
            parent_conversation_id=contract.parent_conversation_id,
            objective=contract.objective,
            outcome=status_outcome,
            summary=f"Operational work completed for: {contract.objective}",
            verified_findings=[
                VerifiedFinding(
                    finding_id="find_test_1",
                    statement="Project configuration is valid.",
                    evidence_refs=["art_test_1"],
                    verification_basis="Local inspection verified config.json syntax",
                    extraction_method="deterministic",
                    verified=True,
                )
            ],
            interpretations=[
                ModelInterpretation(
                    statement="No syntax errors found.",
                    evidence_refs=["art_test_1"],
                    uncertainty="low",
                    assumptions=[],
                )
            ],
            failures=[],
            contradictions=[],
            subagent_receipts=[],
            artifact_references=[
                ArtifactReference(
                    artifact_id="art_test_1",
                    content_type="text/plain",
                    byte_size=128,
                    line_count=10,
                    sha256="0" * 64,
                    tool_name="mcp.filesystem.read_file",
                    description="Test config file",
                )
            ],
            compaction_metrics=CompactionMetrics(
                original_bytes=5000,
                final_estimated_tokens=180,
                reduction_ratio=20.0,
                is_handoff_ready=self.is_handoff_ready,
                handoff_blocked_reason=None if self.is_handoff_ready else "Context budget exhausted",
            ),
        )

        return TaskDelegationResult(
            task_id=contract.task_id,
            parent_conversation_id=contract.parent_conversation_id,
            outcome=del_outcome,
            summary=packet.summary,
            return_packet=packet,
        )


class BasePhase2DTest(unittest.TestCase):
    """Base fixture providing temporary directories and mock environments."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="presence_phase2d_test_")
        self.conv_dir = Path(self.test_dir) / "conversations"
        self.art_dir = Path(self.test_dir) / "artifacts"
        self.logs_dir = Path(self.test_dir) / "logs"
        self.conv_dir.mkdir(parents=True, exist_ok=True)
        self.art_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        self.artifact_store = ArtifactStore(root_dir=self.art_dir)
        self.mock_supervisor = MockSupervisor()

        self.session_factory = AgentSessionFactory(
            conversations_dir=self.conv_dir,
            logs_dir=self.logs_dir,
            artifact_store=self.artifact_store,
            supervisor=self.mock_supervisor,
        )
        current_tool_call_id.set(None)
        current_conversation_id.set(None)
        current_cancellation_token.set(None)

    def tearDown(self):
        current_tool_call_id.set(None)
        current_conversation_id.set(None)
        current_cancellation_token.set(None)
        shutil.rmtree(self.test_dir, ignore_errors=True)


class TestToolRegistrationAndIsolation(BasePhase2DTest):
    """Tests D2-01, D2-02, D2-03: Registration, Canonical Identity, and Local Isolation."""

    def test_01_canonical_tool_exists_and_metadata(self):
        """D2-01: Canonical tool ID is presence.delegate_local_work with valid schema."""
        parent_session = self.session_factory.create_session(
            conversation_id="conv_cloud_root",
            is_cloud_principal=True,
            nesting_depth=0,
        )
        del_tool = parent_session.tool_registry.get_tool("presence.delegate_local_work")
        self.assertIsNotNone(del_tool)
        self.assertEqual(del_tool.tool_id, "presence.delegate_local_work")
        self.assertEqual(del_tool.name, "delegate_local_work")
        self.assertTrue(del_tool.idempotent)
        self.assertEqual(del_tool.timeout_seconds, 180.0)

        # Check description content
        self.assertIn("Delegate a well-defined operational investigation", del_tool.description)
        self.assertIn("CloudReturnPacket", del_tool.description)

        # Check schema
        schema = del_tool.parameters
        self.assertEqual(schema["type"], "object")
        self.assertIn("objective", schema["properties"])
        self.assertIn("context_summary", schema["properties"])
        self.assertIn("expected_deliverable", schema["properties"])
        self.assertIn("completion_criteria", schema["properties"])
        self.assertEqual(schema.get("additionalProperties"), False)
        self.assertEqual(schema.get("required"), ["objective"])

    def test_02_registration_cloud_only(self):
        """D2-02, D2-03: Tool is registered only for Cloud Principal, not children or local sessions."""
        # Top-level cloud session
        cloud_session = self.session_factory.create_session(
            conversation_id="conv_cloud_top",
            is_cloud_principal=True,
            nesting_depth=0,
        )
        self.assertIsNotNone(cloud_session.tool_registry.get_tool("presence.delegate_local_work"))

        # Child session (nesting_depth=1)
        child_session = self.session_factory.create_session(
            conversation_id="conv_child_1",
            is_cloud_principal=False,
            nesting_depth=1,
            parent_conversation_id="conv_cloud_top",
        )
        self.assertIsNone(child_session.tool_registry.get_tool("presence.delegate_local_work"))
        self.assertIsNone(child_session.tool_registry.get_tool("delegate_local_work"))

        # Local supervisor session
        sup_session = self.session_factory.create_session(
            conversation_id="conv_local_sup",
            session_role="local_supervisor",
            is_cloud_principal=False,
            nesting_depth=1,
        )
        self.assertIsNone(sup_session.tool_registry.get_tool("presence.delegate_local_work"))

        # Subagent session
        sub_session = self.session_factory.create_session(
            conversation_id="conv_subagent",
            session_role="subagent_researcher",
            is_cloud_principal=False,
            nesting_depth=2,
        )
        self.assertIsNone(sub_session.tool_registry.get_tool("presence.delegate_local_work"))

    def test_03_no_raw_mcp_exposure_to_cloud(self):
        """D2-02: Cloud Principal does NOT receive raw MCP tool definitions when MCP is unconfigured."""
        cloud_session = self.session_factory.create_session(
            conversation_id="conv_cloud_tools",
            is_cloud_principal=True,
            nesting_depth=0,
            include_mcp_tools=False,
        )
        tool_ids = [t.tool_id for t in cloud_session.tool_registry.list_tools()]
        self.assertIn("presence.delegate_local_work", tool_ids)
        # Ensure raw MCP tools are not present
        mcp_tools = [tid for tid in tool_ids if tid.startswith("mcp.")]
        self.assertEqual(len(mcp_tools), 0)

    def test_04_direct_invocation_rejected_from_child_session(self):
        """D2-03, D2-04: Server-side check blocks invocation if called by child session."""
        child_session = self.session_factory.create_session(
            conversation_id="conv_child_invoker",
            is_cloud_principal=False,
            nesting_depth=1,
        )
        tool = build_delegate_local_work_tool(
            supervisor=self.mock_supervisor,
            parent_session=child_session,
        )
        res = tool.handler({"objective": "Test objective from child"})
        self.assertEqual(res["status"], "ERROR")
        self.assertEqual(res["outcome"], "blocked")
        self.assertIn("can only be invoked by the top-level Cloud Principal", res["error"])

    def test_05_direct_invocation_rejected_from_non_cloud_role(self):
        """D2-03: Server-side check blocks invocation if session role is not cloud principal."""
        fake_session = MagicMock()
        fake_session.conversation_id = "conv_fake"
        fake_session.metadata.nesting_depth = 0
        fake_session.session_role = "local_supervisor"
        fake_session.is_cloud_principal = False

        tool = build_delegate_local_work_tool(
            supervisor=self.mock_supervisor,
            parent_session=fake_session,
        )
        res = tool.handler({"objective": "Test objective from local supervisor"})
        self.assertEqual(res["status"], "ERROR")
        self.assertEqual(res["outcome"], "blocked")
        self.assertIn("unauthorized to invoke local delegation", res["error"])

    def test_05b_missing_metadata_fields_fail_closed(self):
        """Preflight 2.1: Missing session metadata fields fail-closed and reject execution."""
        # 1. Missing session_role
        s1 = MagicMock(spec=["conversation_id", "is_cloud_principal", "metadata"])
        s1.conversation_id = "c1"
        s1.is_cloud_principal = True
        s1.metadata = MagicMock(nesting_depth=0)
        tool1 = build_delegate_local_work_tool(supervisor=self.mock_supervisor, parent_session=s1)
        r1 = tool1.handler({"objective": "Missing role test"})
        self.assertEqual(r1["status"], "ERROR")
        self.assertEqual(r1["outcome"], "blocked")
        self.assertIn("missing required 'session_role'", r1["error"])

        # 2. Missing is_cloud_principal
        s2 = MagicMock(spec=["conversation_id", "session_role", "metadata"])
        s2.conversation_id = "c2"
        s2.session_role = "cloud_principal"
        s2.metadata = MagicMock(nesting_depth=0)
        tool2 = build_delegate_local_work_tool(supervisor=self.mock_supervisor, parent_session=s2)
        r2 = tool2.handler({"objective": "Missing is_cloud test"})
        self.assertEqual(r2["status"], "ERROR")
        self.assertEqual(r2["outcome"], "blocked")
        self.assertIn("is_cloud_principal=True", r2["error"])

        # 3. Missing nesting_depth
        s3 = MagicMock(spec=["conversation_id", "session_role", "is_cloud_principal"])
        s3.conversation_id = "c3"
        s3.session_role = "cloud_principal"
        s3.is_cloud_principal = True
        tool3 = build_delegate_local_work_tool(supervisor=self.mock_supervisor, parent_session=s3)
        r3 = tool3.handler({"objective": "Missing nesting depth test"})
        self.assertEqual(r3["status"], "ERROR")
        self.assertEqual(r3["outcome"], "blocked")
        self.assertIn("missing required 'nesting_depth'", r3["error"])


class TestArgumentValidation(BasePhase2DTest):
    """Tests D2-04, D2-05: Input validation and rejection of forbidden authority fields."""

    def setUp(self):
        super().setUp()
        self.cloud_session = self.session_factory.create_session(
            conversation_id="conv_cloud_val",
            is_cloud_principal=True,
            nesting_depth=0,
        )
        self.tool = build_delegate_local_work_tool(
            supervisor=self.mock_supervisor,
            parent_session=self.cloud_session,
        )

    def test_06_missing_or_blank_objective_rejected(self):
        """D2-04: Empty or missing objective is rejected with validation error."""
        # Missing objective
        res1 = self.tool.handler({})
        self.assertEqual(res1["status"], "ERROR")
        self.assertEqual(res1["outcome"], "failed")

        # Blank objective
        res2 = self.tool.handler({"objective": "   "})
        self.assertEqual(res2["status"], "ERROR")
        self.assertEqual(res2["outcome"], "failed")

        # Too short objective (<5 chars)
        res3 = self.tool.handler({"objective": "abc"})
        self.assertEqual(res3["status"], "ERROR")
        self.assertEqual(res3["outcome"], "failed")

    def test_07_excessive_length_objective_rejected(self):
        """D2-04: Objectives exceeding maximum length (2000 chars) are rejected."""
        res = self.tool.handler({"objective": "X" * 2001})
        self.assertEqual(res["status"], "ERROR")
        self.assertEqual(res["outcome"], "failed")

    def test_08_forbidden_authority_fields_rejected(self):
        """D2-04, D2-05: Model attempts to supply authority overrides are strictly rejected."""
        forbidden_fields = [
            ("parent_conversation_id", "malicious_parent"),
            ("task_id", "custom_task_id"),
            ("permission_tier", "L3_PROTECTED"),
            ("allowed_capabilities", ["admin", "credentials"]),
            ("execution_profile", "ADMIN_EXECUTION"),
            ("provider", "openai"),
            ("model", "gpt-4o"),
            ("read_only", False),
            ("subagents_allowed", True),
            ("max_subagents", 10),
        ]
        for field_name, bad_value in forbidden_fields:
            payload = {
                "objective": "Inspect the local project configuration",
                field_name: bad_value,
            }
            res = self.tool.handler(payload)
            self.assertEqual(
                res["status"],
                "ERROR",
                f"Field '{field_name}' was not rejected as forbidden!",
            )
            self.assertEqual(res["outcome"], "failed")
            self.assertIn("validation failed", res["error"].lower())

    def test_09_valid_optional_parameters_accepted(self):
        """D2-04: Valid optional arguments are accepted and passed to contract."""
        payload = {
            "objective": "Inspect project requirements and test suite",
            "context_summary": "User reported regression in context compaction",
            "expected_deliverable": "List of passing and failing tests",
            "completion_criteria": ["Run pytest", "Inspect log output"],
        }
        res = self.tool.handler(payload)
        self.assertEqual(res["status"], "SUCCESS")
        self.assertIsNotNone(self.mock_supervisor.last_contract)
        contract = self.mock_supervisor.last_contract
        self.assertEqual(contract.objective, payload["objective"])
        self.assertEqual(contract.context_summary, payload["context_summary"])
        self.assertEqual(contract.completion_criteria, payload["completion_criteria"])


class TestTrustedContractConstruction(BasePhase2DTest):
    """Tests D2-05, D2-06, D2-07: Trusted contract assembly and permission derivation."""

    def test_10_trusted_contract_construction(self):
        """D2-05, D2-06, D2-07: Runtime controls task_id, parent_cid, profile, constraints."""
        cloud_session = self.session_factory.create_session(
            conversation_id="conv_cloud_contract_test",
            is_cloud_principal=True,
            nesting_depth=0,
        )
        tool = build_delegate_local_work_tool(
            supervisor=self.mock_supervisor,
            parent_session=cloud_session,
            allowed_capabilities=["filesystem", "code_search"],
            read_only=True,
            subagents_allowed=True,
            max_subagents=2,
            timeout_seconds=120.0,
            context_budget_tokens=1500,
        )

        res = tool.handler({
            "objective": "Verify config file consistency across project",
            "expected_deliverable": "Consistency report",
        })
        self.assertEqual(res["status"], "SUCCESS")
        contract = self.mock_supervisor.last_contract
        self.assertIsNotNone(contract)

        # 1. Runtime-controlled identity
        self.assertTrue(contract.task_id.startswith("task_del_"))
        self.assertEqual(contract.parent_conversation_id, "conv_cloud_contract_test")

        # 2. Execution profile pinned to local supervisor routine
        self.assertEqual(contract.execution_profile, ExecutionProfile.LOCAL_SUPERVISOR_ROUTINE)

        # 3. Policy-derived constraints
        self.assertTrue(contract.constraints.read_only)
        self.assertTrue(contract.constraints.subagents_allowed)
        self.assertEqual(contract.constraints.max_subagents, 2)
        self.assertEqual(contract.constraints.timeout_seconds, 120.0)
        self.assertEqual(contract.constraints.context_budget_tokens, 1500)

        # 4. Capabilities derived from policy, not model
        self.assertIn("filesystem", contract.allowed_capabilities)
        self.assertIn("code_search", contract.allowed_capabilities)
        self.assertIn("subagents", contract.allowed_capabilities)


class TestEndToEndCloudDelegation(BasePhase2DTest):
    """Tests D2-08, D2-09, D2-14, D2-15, D2-20: Full cloud-to-local turn execution."""

    def test_11_e2e_cloud_delegation_turn_loop(self):
        """D2-08, D2-14, D2-15: Cloud principal invokes tool, supervisor executes, cloud synthesizes."""
        cloud_provider = CountingMockCloudProvider()

        # Step 1: Cloud provider requests tool call on first turn
        tool_call_chunk = ModelStreamChunk(
            tool_calls=[{
                "call_id": "call_del_101",
                "name": "presence.delegate_local_work",
                "args": {
                    "objective": "Investigate project configuration",
                    "expected_deliverable": "Config diagnosis",
                },
            }]
        )
        # Step 2: Cloud provider returns final synthesis on second turn
        synthesis_chunk = ModelStreamChunk(
            text_delta="Based on the local supervisor's investigation, the project configuration is valid."
        )
        cloud_provider.queue_response([tool_call_chunk])
        cloud_provider.queue_response([synthesis_chunk])

        session = self.session_factory.create_session(
            conversation_id="conv_e2e_del",
            model_provider=cloud_provider,
            is_cloud_principal=True,
            nesting_depth=0,
        )

        # Execute turn
        events = list(session.execute_turn("Please inspect our project config"))
        event_types = [e.event_type for e in events]

        # Verify event stream
        self.assertIn("TOOL_REQUESTED", event_types)
        self.assertIn("TOOL_STARTED", event_types)
        self.assertIn("TOOL_COMPLETED", event_types)
        self.assertIn("TURN_COMPLETED", event_types)

        # Verify supervisor was invoked
        self.assertEqual(self.mock_supervisor.execution_count, 1)

        # Verify trajectory steps:
        # Step 0: USER_INPUT
        # Step 1: TOOL_CALL (presence.delegate_local_work)
        # Step 2: TOOL_RESULT (CloudReturnPacket payload)
        # Step 3: AGENT_TURN (Final cloud synthesis)
        steps = session.trajectory_store.list_steps()
        self.assertEqual(len(steps), 4)
        self.assertEqual(steps[0].step_type, StepType.USER_INPUT)

        self.assertEqual(steps[1].step_type, StepType.TOOL_CALL)
        self.assertEqual(steps[1].step_payload["name"], "presence.delegate_local_work")

        self.assertEqual(steps[2].step_type, StepType.TOOL_RESULT)
        result_content = steps[2].step_payload["content"]
        self.assertIn("Project configuration is valid", result_content)
        self.assertIn("SUCCESS", result_content)

        self.assertEqual(steps[3].step_type, StepType.AGENT_TURN)
        self.assertIn("Based on the local supervisor's investigation", steps[3].step_payload["content"])

    def test_12_zero_cloud_calls_during_local_execution(self):
        """D2-09, D2-20: Cloud provider invocation counter remains strictly 0 during local supervisor execution."""
        cloud_provider = CountingMockCloudProvider()

        # Wrap supervisor to assert that cloud provider is never invoked during local execution
        original_execute = self.mock_supervisor.execute

        def _execute_with_cloud_check(contract, parent_session, cancellation_token=None):
            initial_cloud_calls = cloud_provider.call_count
            res = original_execute(contract, parent_session, cancellation_token)
            # Cloud calls during local supervisor execution must be 0
            calls_during_local = cloud_provider.call_count - initial_cloud_calls
            if calls_during_local != 0:
                raise AssertionError(f"Cloud provider called {calls_during_local} times during local execution!")
            return res

        self.mock_supervisor.execute = _execute_with_cloud_check

        tool_call_chunk = ModelStreamChunk(
            tool_calls=[{
                "call_id": "call_iso_202",
                "name": "presence.delegate_local_work",
                "args": {"objective": "Run isolated local analysis"},
            }]
        )
        cloud_provider.queue_response([tool_call_chunk])
        cloud_provider.queue_response([ModelStreamChunk(text_delta="Finished synthesis.")])

        session = self.session_factory.create_session(
            conversation_id="conv_iso_test",
            model_provider=cloud_provider,
            is_cloud_principal=True,
            nesting_depth=0,
        )

        list(session.execute_turn("Execute isolated analysis"))
        # 1 call for initial tool request, 1 call for final synthesis = exactly 2 cloud calls total
        self.assertEqual(cloud_provider.call_count, 2)

    def test_13_gemini_and_freellmapi_schema_compatibility(self):
        """D2-19: Canonical tool declaration conforms to standard Gemini and OpenAI formats."""
        session = self.session_factory.create_session(
            conversation_id="conv_schema_comp",
            is_cloud_principal=True,
            nesting_depth=0,
        )
        tool = session.tool_registry.get_tool("presence.delegate_local_work")
        decl = tool.to_declaration()

        # Validate name, description, parameters
        self.assertEqual(decl["name"], "presence.delegate_local_work")
        self.assertTrue(len(decl["description"]) > 50)
        self.assertEqual(decl["parameters"]["type"], "object")
        self.assertIn("objective", decl["parameters"]["properties"])
        self.assertEqual(decl["parameters"]["additionalProperties"], False)


class TestIdempotencyAndConcurrency(BasePhase2DTest):
    """Tests D2-16: Idempotent replay, distinct tool calls, and concurrency guard."""

    def setUp(self):
        super().setUp()
        self.cloud_session = self.session_factory.create_session(
            conversation_id="conv_idempotency_test",
            is_cloud_principal=True,
            nesting_depth=0,
        )
        self.tool = build_delegate_local_work_tool(
            supervisor=self.mock_supervisor,
            parent_session=self.cloud_session,
        )

    def test_14_idempotent_replay_same_call_id(self):
        """D2-16: Replaying the same tool_call_id returns cached result without re-executing."""
        args = {
            "objective": "Idempotent operational inspection",
            "_tool_call_id": "call_idemp_001",
        }
        # First execution
        res1 = self.tool.handler(args)
        self.assertEqual(res1["status"], "SUCCESS")
        self.assertEqual(self.mock_supervisor.execution_count, 1)

        # Second execution with same tool_call_id
        res2 = self.tool.handler(args)
        self.assertEqual(res2["status"], "SUCCESS")
        # Supervisor execution count remains 1
        self.assertEqual(self.mock_supervisor.execution_count, 1)
        self.assertEqual(res1["task_id"], res2["task_id"])

    def test_15_distinct_call_ids_same_objective(self):
        """D2-16: Different tool_call_ids with identical objectives execute independently."""
        args1 = {"objective": "Repeatable inspection", "_tool_call_id": "call_a_1"}
        args2 = {"objective": "Repeatable inspection", "_tool_call_id": "call_b_2"}

        res1 = self.tool.handler(args1)
        res2 = self.tool.handler(args2)

        self.assertEqual(res1["status"], "SUCCESS")
        self.assertEqual(res2["status"], "SUCCESS")
        self.assertEqual(self.mock_supervisor.execution_count, 2)
        self.assertNotEqual(res1["task_id"], res2["task_id"])

    def test_16_concurrency_guard_active_call_id(self):
        """D2-16: Simultaneous invocation of an actively executing tool_call_id is blocked."""
        call_id = "call_concurrent_99"
        original_execute = self.mock_supervisor.execute

        # Simulate recursive/reentrant call during active execution
        def _reentrant_supervisor(contract, parent_session, cancellation_token=None):
            # Attempt to invoke the same tool call again while inside supervisor
            nested_res = self.tool.handler({
                "objective": "Nested duplicate invocation",
                "_tool_call_id": call_id,
            })
            self.assertEqual(nested_res["status"], "ERROR")
            self.assertEqual(nested_res["outcome"], "blocked")
            self.assertIn("already actively executing", nested_res["error"])
            return original_execute(contract, parent_session, cancellation_token)

        self.mock_supervisor.execute = _reentrant_supervisor
        res = self.tool.handler({
            "objective": "Test concurrency guard",
            "_tool_call_id": call_id,
        })
        self.assertEqual(res["status"], "SUCCESS")


class TestCancellationTimeoutAndResilience(BasePhase2DTest):
    """Tests D2-17, D2-18, D2-22: Cancellation, timeout, supervisor exceptions, and missing packets."""

    def setUp(self):
        super().setUp()
        self.cloud_session = self.session_factory.create_session(
            conversation_id="conv_resilience_test",
            is_cloud_principal=True,
            nesting_depth=0,
        )
        self.tool = build_delegate_local_work_tool(
            supervisor=self.mock_supervisor,
            parent_session=self.cloud_session,
        )

    def test_17_cancellation_propagation(self):
        """D2-17: Pre-cancelled cancellation token returns cancelled status."""
        cancel_token = CancellationToken()
        cancel_token.cancel()
        current_cancellation_token.set(cancel_token)

        try:
            res = self.tool.handler({
                "objective": "Test cancelled execution",
                "_tool_call_id": "call_cancel_01",
            })
            self.assertEqual(res["status"], "CANCELLED")
            self.assertEqual(res["outcome"], "failed")
            self.assertEqual(self.mock_supervisor.execution_count, 0)
        finally:
            current_cancellation_token.set(None)

    def test_18_supervisor_exception_resilience(self):
        """D2-22: Unhandled exception in supervisor returns structured error envelope without crash."""
        self.mock_supervisor.should_raise = RuntimeError("Unexpected disk I/O failure in local worker")

        res = self.tool.handler({
            "objective": "Test supervisor exception handling",
            "_tool_call_id": "call_err_01",
        })
        self.assertEqual(res["status"], "ERROR")
        self.assertEqual(res["outcome"], "failed")
        self.assertIn("Unexpected disk I/O failure", res["error"])
        self.assertEqual(len(res["failures"]), 1)
        self.assertEqual(res["failures"][0]["failure_type"], "SUPERVISOR_EXCEPTION")

    def test_19_missing_packet_resilience(self):
        """D2-22: Supervisor returning None packet returns structured error envelope."""
        self.mock_supervisor.return_none_packet = True

        res = self.tool.handler({
            "objective": "Test missing return packet handling",
            "_tool_call_id": "call_none_01",
        })
        self.assertEqual(res["status"], "ERROR")
        self.assertIn("did not produce a CloudReturnPacket", res["error"])


class TestContextBudgetAndSecurity(BasePhase2DTest):
    """Tests D2-11, D2-12, D2-13, D2-21: Context budget safety, credential redaction, and handoff gating."""

    def setUp(self):
        super().setUp()
        self.cloud_session = self.session_factory.create_session(
            conversation_id="conv_sec_test",
            is_cloud_principal=True,
            nesting_depth=0,
        )

    def test_20_context_budget_overflow_blocks_handoff(self):
        """D2-13: When is_handoff_ready=False, a compact BLOCKED envelope is returned without oversized payload."""
        self.mock_supervisor.is_handoff_ready = False
        tool = build_delegate_local_work_tool(
            supervisor=self.mock_supervisor,
            parent_session=self.cloud_session,
        )

        res = tool.handler({
            "objective": "Test context budget overflow blocking",
            "_tool_call_id": "call_overflow_01",
        })
        self.assertEqual(res["status"], "BLOCKED")
        self.assertEqual(res["outcome"], "blocked")
        self.assertIn("Handoff blocked", res["error"])
        self.assertIn("Context budget exhausted", res["error"])
        # Verified findings must NOT be dumped to the cloud model when blocked
        self.assertNotIn("verified_findings", res)

    def test_21_credential_redaction_in_handoff(self):
        """D2-21: Sensitive credentials in findings or interpretations are redacted before handoff."""
        # Wrap supervisor to emit unredacted API key in finding
        original_execute = self.mock_supervisor.execute

        def _execute_with_credential(contract, parent_session, cancellation_token=None):
            result = original_execute(contract, parent_session, cancellation_token)
            # Inject simulated exposed API key
            result.return_packet.verified_findings.append(
                VerifiedFinding(
                    finding_id="find_test_cred",
                    statement="Found API key AIzaSyD9876543210zyxwvutsrqponmlkjihgf in env",
                    evidence_refs=["art_test_1"],
                    verification_basis="File inspection",
                    extraction_method="deterministic",
                    verified=True,
                )
            )
            return result

        self.mock_supervisor.execute = _execute_with_credential
        tool = build_delegate_local_work_tool(
            supervisor=self.mock_supervisor,
            parent_session=self.cloud_session,
        )

        res = tool.handler({
            "objective": "Scan environment for credentials",
            "_tool_call_id": "call_redact_01",
        })
        self.assertEqual(res["status"], "SUCCESS")
        # Ensure raw key was redacted
        serialized = json.dumps(res)
        self.assertNotIn("AIzaSyD9876543210zyxwvutsrqponmlkjihgf", serialized)
        self.assertIn("[REDACTED_GEMINI_KEY]", serialized)


class TestLiveCloudDelegation(BasePhase2DTest):
    """Test D2-23: Live integration using local Ollama Qwen 3.5 9B model."""

    def test_22_live_cloud_delegation_ollama(self):
        """D2-23: Live delegation of a real read-only task to LocalOperationsSupervisor via Ollama."""
        from runtime.providers.ollama import OllamaModelProvider
        from runtime.providers.coordinator import MultiProviderCoordinator
        real_ollama = OllamaModelProvider(model_id="qwen3.5:9b", mode="routine", think=False)
        is_val, reason = real_ollama.validate_configuration()
        if not is_val:
            self.skipTest(f"Local Ollama Qwen 3.5 9B unavailable: {reason}")

        # Create temporary project structure for local inspection
        proj_dir = os.path.join(self.test_dir, "sample_project")
        os.makedirs(proj_dir, exist_ok=True)
        config_path = os.path.join(proj_dir, "app_config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"name": "PresenceLiveTest", "version": "2.4.0", "status": "active"}, f)

        # Set up real LocalOperationsSupervisor with Ollama
        coordinator = MultiProviderCoordinator(
            providers={"ollama": real_ollama},
            primary_provider="ollama",
            fallback_order=[],
        )
        real_supervisor = LocalOperationsSupervisor(
            session_factory=self.session_factory,
            coordinator=coordinator,
            artifact_store=self.artifact_store,
        )

        cloud_session = self.session_factory.create_session(
            conversation_id="conv_cloud_live_del",
            is_cloud_principal=True,
            nesting_depth=0,
            supervisor=real_supervisor,
        )

        del_tool = cloud_session.tool_registry.get_tool("presence.delegate_local_work")
        self.assertIsNotNone(del_tool)

        # Execute real operational delegation
        res = del_tool.handler({
            "objective": f"Inspect the configuration file at '{config_path}' and determine the application name and version.",
            "expected_deliverable": "Application name and version summary",
            "_tool_call_id": "call_live_del_001",
        })

        self.assertIn(res["status"], ("SUCCESS", "BLOCKED"))
        self.assertTrue(res["task_id"].startswith("task_del_"))
        self.assertEqual(res["parent_conversation_id"], "conv_cloud_live_del")
        self.assertTrue(len(res["summary"]) > 0)


if __name__ == "__main__":
    unittest.main()
