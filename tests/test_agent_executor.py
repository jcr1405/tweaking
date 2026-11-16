"""Automated Unit & Integration Tests for AgentExecutor"""

import unittest
import tempfile
import json
from pathlib import Path

from runtime.conversation import ConversationMetadata
from runtime.trajectory import TrajectoryStore, StepType, StepStatus
from runtime.journal import EventJournal
from runtime.summaries import GlobalSummaryManager
from runtime.reconstruction import ContextAssembler
from runtime.permissions import PermissionBoundary, PermissionTier
from runtime.tools.registry import ToolDefinition, ToolRegistry, build_default_tools
from runtime.providers.fake import FakeModelProvider
from runtime.providers.base import ModelStreamChunk
from runtime.agent_executor import AgentExecutor, CancellationToken


class TestAgentExecutor(unittest.TestCase):
    """Validates the complete agent execution loop, tool reflection, and failure resilience."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.conv_id = "test-agent-conv-001"

        # 1. Trajectory & Scoped DB
        self.store = TrajectoryStore(
            conversation_id=self.conv_id,
            db_dir=self.base_dir / "conversations"
        )
        self.meta = ConversationMetadata(
            conversation_id=self.conv_id,
            title="Agent Test Session",
            workspace_path=r"C:\test\workspace"
        )
        self.store.save_metadata(self.meta)

        # 2. Event Journal
        self.journal = EventJournal(
            conversation_id=self.conv_id,
            logs_dir=self.base_dir / "logs"
        )

        # 3. Global Summary Manager
        self.summary_manager = GlobalSummaryManager(
            db_path=self.base_dir / "summaries.db"
        )
        self.summary_manager.upsert_summary(
            conversation_id=self.conv_id,
            title="Agent Test Session",
            workspace_path=r"C:\test\workspace"
        )

        # 4. Permissions & Tool Registry
        self.boundary = PermissionBoundary()
        self.registry = build_default_tools(
            runtime_status_fn=lambda: {"state": "RUNNING", "pid": 5555, "memory_mb": 22.8},
            conversation_info_fn=lambda: {"conversation_id": self.conv_id, "step_count": self.store.get_step_count()},
            search_conversations_fn=lambda q, lim: [{"title": "Agent Test Session", "snippet": q}]
        )

        # 5. Model Provider
        self.provider = FakeModelProvider()

        # 6. Executor
        self.executor = AgentExecutor(
            trajectory_store=self.store,
            event_journal=self.journal,
            summary_manager=self.summary_manager,
            permission_boundary=self.boundary,
            tool_registry=self.registry,
            model_provider=self.provider
        )

    def tearDown(self):
        self.store.close()
        self.journal.close()
        self.summary_manager.close()
        self.temp_dir.cleanup()

    def test_normal_user_assistant_turn(self):
        events = list(self.executor.execute_turn(user_input="Hello Presence"))
        event_types = [e.event_type for e in events]

        self.assertIn("TURN_STARTED", event_types)
        self.assertIn("MODEL_REQUESTED", event_types)
        self.assertIn("TEXT_DELTA", event_types)
        self.assertIn("TURN_COMPLETED", event_types)

        # Verify trajectory persistence
        steps = self.store.list_steps()
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0].step_type, StepType.USER_INPUT)
        self.assertEqual(steps[0].step_payload["content"], "Hello Presence")
        self.assertEqual(steps[1].step_type, StepType.AGENT_TURN)
        self.assertIn("Persistent Intelligent Presence", steps[1].step_payload["content"])

        # Verify JSONL journal persistence
        transcript = self.journal.read_transcript()
        self.assertEqual(len(transcript), 2)

    def test_conversation_continuation(self):
        # Turn 1
        list(self.executor.execute_turn("Turn 1 message"))
        self.assertEqual(self.store.get_step_count(), 2)

        # Turn 2
        events = list(self.executor.execute_turn("Turn 2 follow-up"))
        self.assertEqual(self.store.get_step_count(), 4)

        # Check recorded request in fake provider: must include Turn 1 context
        last_req = self.provider.recorded_requests[-1]
        req_messages = last_req["messages"]
        self.assertGreaterEqual(len(req_messages), 3)
        self.assertEqual(req_messages[0]["content"], "Turn 1 message")
        self.assertEqual(req_messages[2]["content"], "Turn 2 follow-up")

    def test_tool_calling_loop(self):
        # Script fake provider to request presence.get_runtime_status on turn 1
        tool_call_chunk = ModelStreamChunk(
            tool_calls=[{
                "call_id": "call_stat_1",
                "name": "presence.get_runtime_status",
                "args": {}
            }]
        )
        self.provider.queue_response([tool_call_chunk])

        events = list(self.executor.execute_turn("Check runtime status"))
        event_types = [e.event_type for e in events]

        self.assertIn("TOOL_REQUESTED", event_types)
        self.assertIn("TOOL_STARTED", event_types)
        self.assertIn("TOOL_COMPLETED", event_types)
        self.assertIn("TURN_COMPLETED", event_types)

        # Inspect trajectory steps:
        # Step 0: USER_INPUT
        # Step 1: TOOL_CALL (presence.get_runtime_status)
        # Step 2: TOOL_RESULT
        # Step 3: AGENT_TURN (final answer)
        steps = self.store.list_steps()
        self.assertEqual(len(steps), 4)
        self.assertEqual(steps[0].step_type, StepType.USER_INPUT)
        self.assertEqual(steps[1].step_type, StepType.TOOL_CALL)
        self.assertEqual(steps[1].step_payload["name"], "presence.get_runtime_status")
        self.assertEqual(steps[2].step_type, StepType.TOOL_RESULT)
        self.assertIn("RUNNING", steps[2].step_payload["content"])
        self.assertEqual(steps[3].step_type, StepType.AGENT_TURN)

    def test_unknown_tool_rejection(self):
        # Model requests an unregistered tool
        tool_call_chunk = ModelStreamChunk(
            tool_calls=[{
                "call_id": "call_unknown_1",
                "name": "unregistered.malicious_action",
                "args": {}
            }]
        )
        self.provider.queue_response([tool_call_chunk])

        events = list(self.executor.execute_turn("Attempt invalid action"))
        event_types = [e.event_type for e in events]

        self.assertIn("TOOL_FAILED", event_types)
        self.assertIn("TURN_COMPLETED", event_types)

        # Step 2 (TOOL_RESULT) should record error
        steps = self.store.list_steps()
        self.assertEqual(steps[2].status, StepStatus.ERROR)
        self.assertIn("not registered", steps[2].step_payload["content"])

    def test_security_gate_blocking_l3_tool(self):
        # Register an L3 tool
        self.registry.register(ToolDefinition(
            tool_id="system.protected_wipe",
            name="protected_wipe",
            description="Dangerous action.",
            parameters={"type": "object"},
            authority_tier=PermissionTier.L3_PROTECTED,
            handler=lambda args: {"wiped": True}
        ))

        tool_call_chunk = ModelStreamChunk(
            tool_calls=[{
                "call_id": "call_wipe_1",
                "name": "system.protected_wipe",
                "args": {}
            }]
        )
        self.provider.queue_response([tool_call_chunk])

        events = list(self.executor.execute_turn("Wipe everything"))
        event_types = [e.event_type for e in events]

        self.assertIn("TOOL_FAILED", event_types)
        # Verify blocked by security gate
        steps = self.store.list_steps()
        self.assertEqual(steps[2].status, StepStatus.ERROR)
        self.assertIn("blocked by security gate", steps[2].step_payload["content"])

    def test_turn_cancellation_before_execution(self):
        token = CancellationToken()
        token.cancel()

        events = list(self.executor.execute_turn("Hello", cancellation_token=token))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "TURN_CANCELLED")
        self.assertEqual(self.store.get_step_count(), 0)

    def test_provider_failure_handling(self):
        failing_provider = FakeModelProvider(
            simulate_error=ConnectionError("Simulated DNS failure")
        )
        executor = AgentExecutor(
            trajectory_store=self.store,
            event_journal=self.journal,
            summary_manager=self.summary_manager,
            permission_boundary=self.boundary,
            tool_registry=self.registry,
            model_provider=failing_provider
        )

        events = list(executor.execute_turn("Hello"))
        event_types = [e.event_type for e in events]

        self.assertIn("TURN_FAILED", event_types)
        # User input was recorded, but conversation state is clean and recoverable
        steps = self.store.list_steps()
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].step_type, StepType.USER_INPUT)


if __name__ == "__main__":
    unittest.main()
