"""Unit Tests for Runtime Observability Layer
Verifies typed snapshots, task hierarchy, event bridge, redaction, and non-blocking delivery.
"""

import unittest
import time
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from runtime.events import EventBus
from runtime.lifecycle import PresenceRuntime, RuntimeState
from runtime.observability.models import TaskState, TaskType, NormalizedEvent
from runtime.observability.task_registry import ActiveTaskRegistry
from runtime.observability.event_bridge import ObservabilityEventBridge
from runtime.observability.service import RuntimeObservabilityService
from runtime.observability.redaction import (
    sanitize_terminal_output,
    redact_sensitive_string,
    sanitize_and_redact,
    redact_data_structure
)
from runtime.providers.base import ModelStreamChunk
from runtime.providers.fake import FakeModelProvider


class TestObservabilityModelsAndRedaction(unittest.TestCase):
    """Verifies data models, security redaction, and terminal sanitization."""

    def test_01_terminal_escape_sanitization(self):
        # Raw ANSI CSI sequences (color, cursor movements)
        malicious = "Hello \x1b[31;1mRed Alert\x1b[0m \x1b[2JWorld"
        cleaned = sanitize_terminal_output(malicious)
        self.assertNotIn("\x1b", cleaned)
        self.assertEqual(cleaned, "Hello Red Alert World")

    def test_02_sensitive_redaction(self):
        sample = (
            "Connecting with key AIzaSyD4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t and "
            "sk-1234567890abcdef1234567890abcdef and "
            "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 and "
            "password: mysecretpassword123"
        )
        redacted = redact_sensitive_string(sample)
        self.assertNotIn("AIzaSyD", redacted)
        self.assertNotIn("sk-1234567890", redacted)
        self.assertNotIn("eyJhbGci", redacted)
        self.assertNotIn("mysecretpassword123", redacted)
        self.assertIn("[REDACTED_GEMINI_KEY]", redacted)
        self.assertIn("[REDACTED_API_KEY]", redacted)
        self.assertIn("[REDACTED_TOKEN]", redacted)

    def test_03_nested_data_structure_redaction(self):
        data = {
            "user": "alice",
            "api_key": "secret_key_value",
            "auth_headers": {"Authorization": "Bearer supersecret"},
            "sub_items": ["normal", "AIzaSyD4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t"]
        }
        redacted = redact_data_structure(data)
        self.assertEqual(redacted["api_key"], "[REDACTED]")
        self.assertEqual(redacted["auth_headers"]["Authorization"], "[REDACTED]")
        self.assertIn("[REDACTED_GEMINI_KEY]", redacted["sub_items"][1])
        self.assertEqual(redacted["user"], "alice")


class TestTaskRegistry(unittest.TestCase):
    """Verifies hierarchical task registration, progression, cancellation, and bounds."""

    def setUp(self):
        self.registry = ActiveTaskRegistry(max_completed_history=5)

    def test_04_task_lifecycle_and_hierarchy(self):
        # Parent Turn Task
        turn_task = self.registry.register_task(
            task_type=TaskType.TURN,
            display_name="Turn #1",
            conversation_id="conv-123",
            cancellable=True
        )
        self.assertEqual(turn_task.state, TaskState.QUEUED)
        self.assertEqual(len(self.registry.get_active_tasks()), 1)

        # Start turn
        self.registry.start_task(turn_task.task_id)
        self.assertEqual(turn_task.state, TaskState.RUNNING)
        self.assertIsNotNone(turn_task.started_at)

        # Child Tool Task
        tool_task = self.registry.register_task(
            task_type=TaskType.TOOL_EXECUTION,
            display_name="presence.web_search",
            parent_task_id=turn_task.task_id,
            conversation_id="conv-123"
        )
        self.assertEqual(len(turn_task.children), 1)
        self.assertEqual(tool_task.parent_task_id, turn_task.task_id)

        # Complete child tool
        self.registry.start_task(tool_task.task_id)
        self.registry.complete_task(tool_task.task_id, artifact_id="art-999")
        self.assertEqual(tool_task.state, TaskState.COMPLETED)
        self.assertEqual(tool_task.artifact_id, "art-999")

        # Complete parent turn
        self.registry.complete_task(turn_task.task_id)
        self.assertEqual(turn_task.state, TaskState.COMPLETED)
        self.assertEqual(len(self.registry.get_active_tasks()), 0)
        self.assertEqual(len(self.registry.get_completed_tasks()), 2)

    def test_05_cancellation_cascade(self):
        cancelled_calls = []

        def cancel_hook():
            cancelled_calls.append("hook_called")
            return True

        turn_task = self.registry.register_task(
            task_type=TaskType.TURN,
            display_name="Long Turn",
            cancellable=True,
            cancellation_hook=cancel_hook
        )
        child_task = self.registry.register_task(
            task_type=TaskType.TOOL_EXECUTION,
            display_name="Child Tool",
            parent_task_id=turn_task.task_id,
            cancellable=True
        )

        success = self.registry.cancel_task(turn_task.task_id, reason="User interrupted")
        self.assertTrue(success)
        self.assertEqual(len(cancelled_calls), 1)
        self.assertEqual(turn_task.state, TaskState.CANCELLED)
        self.assertEqual(child_task.state, TaskState.CANCELLED)
        self.assertEqual(len(self.registry.get_active_tasks()), 0)

    def test_06_bounded_completed_history(self):
        for i in range(10):
            t = self.registry.register_task(TaskType.TURN, f"Task {i}")
            self.registry.start_task(t.task_id)
            self.registry.complete_task(t.task_id)

        # Max completed history is 5
        completed = self.registry.get_completed_tasks(limit=10)
        self.assertEqual(len(completed), 5)
        # Most recent is Task 9
        self.assertEqual(completed[0].display_name, "Task 9")


class TestObservabilityEventBridge(unittest.TestCase):
    """Verifies thread-safe event buffering and non-blocking listener delivery."""

    def test_07_event_bridge_buffering_and_subscription(self):
        bus = EventBus()
        bridge = ObservabilityEventBridge(max_buffer_size=10)
        bridge.attach_to_event_bus(bus)

        received_events = []
        unsub = bridge.subscribe(lambda ev: received_events.append(ev))

        # Publish through event bus
        bus.publish("CustomRuntimeEvent", {"component": "kernel", "message": "hello"})
        self.assertEqual(len(received_events), 1)
        self.assertEqual(received_events[0].event_type, "CustomRuntimeEvent")
        self.assertEqual(received_events[0].component, "kernel")
        self.assertEqual(bridge.buffer_size, 1)

        # Test unsubscription
        unsub()
        bus.publish("AnotherEvent", {"message": "should not receive"})
        self.assertEqual(len(received_events), 1)
        self.assertEqual(bridge.buffer_size, 2)

    def test_08_slow_or_failing_listener_does_not_block_runtime(self):
        bus = EventBus()
        bridge = ObservabilityEventBridge(max_buffer_size=100)
        bridge.attach_to_event_bus(bus)

        def failing_listener(ev):
            raise RuntimeError("UI listener explosion")

        bridge.subscribe(failing_listener)
        # Should not raise exception
        bus.publish("TestEvent", {"data": 123})
        self.assertEqual(bridge.total_events, 1)


class TestRuntimeObservabilityService(unittest.TestCase):
    """Verifies unified service queries against actual runtime subsystems."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "runtime.db"
        self.summary_db = Path(self.temp_dir.name) / "summaries.db"
        self.artifact_dir = Path(self.temp_dir.name) / "artifacts"
        self.schema_dir = Path(self.temp_dir.name) / "schemas"

        self.runtime = PresenceRuntime(
            mutex_name=f"Local\\PresenceTest_Obs_{int(time.time()*1000)}",
            db_path=self.db_path,
            summary_db_path=self.summary_db,
            artifact_dir=self.artifact_dir,
            schema_cache_dir=self.schema_dir,
        )
        self.runtime.start()
        self.obs = self.runtime.observability

    def tearDown(self):
        self.runtime.stop()
        self.temp_dir.cleanup()

    def test_09_runtime_snapshot(self):
        snap = self.obs.get_runtime_snapshot()
        self.assertEqual(snap.instance_id, self.runtime.instance_id)
        self.assertEqual(snap.pid, self.runtime.pid)
        self.assertEqual(snap.state, "RUNNING")
        self.assertGreater(snap.memory_working_set_mb, 0.0)
        self.assertIsInstance(snap.mcp_servers, list)
        self.assertIsInstance(snap.search_telemetry, dict)
        self.assertEqual(snap.clean_recovery, True)

    def test_10_turn_execution_task_and_trace_tracking(self):
        # Create session with fake provider
        fake_provider = FakeModelProvider(scripted_responses=[
            [ModelStreamChunk(text_delta="Hello there! ")]
        ])

        with self.runtime.create_session(title="Observability Test Conv", model_provider=fake_provider) as session:
            cid = session.conversation_id
            events = list(session.execute_turn("Hi Presence!"))
            self.assertTrue(any(e.event_type == "TURN_COMPLETED" for e in events))

            # Verify conversation is indexed and retrievable
            convs = self.obs.list_conversations()
            self.assertTrue(any(c.conversation_id == cid for c in convs))

            # Verify turn detail retrieval
            turns = self.obs.get_conversation_turns(cid)
            self.assertEqual(len(turns), 1)
            self.assertEqual(turns[0].user_input, "Hi Presence!")
            self.assertIn("Hello there!", turns[0].agent_output)

            # Verify execution trace reconstruction
            trace = self.obs.get_turn_trace(cid, turn_idx=1)
            self.assertGreater(len(trace), 0)
            self.assertEqual(trace[0].event_name, "USER_INPUT")

            # Verify context inspection
            context_dto = self.obs.inspect_turn_context(cid, turn_idx=1)
            self.assertEqual(context_dto.conversation_id, cid)
            self.assertGreater(context_dto.estimated_tokens, 0)

            # Verify completed task in registry
            completed_tasks = self.obs.task_registry.get_completed_tasks()
            self.assertTrue(any(t.conversation_id == cid for t in completed_tasks))

    def test_11_conversation_pagination_and_search(self):
        # Create 3 distinct conversations
        c1 = self.runtime.create_session(title="Alpha Quantum Algorithms")
        c2 = self.runtime.create_session(title="Beta Machine Learning")
        c3 = self.runtime.create_session(title="Gamma Cloud Architecture")

        # Test pagination
        page1 = self.obs.list_conversations(limit=2, offset=0)
        self.assertLessEqual(len(page1), 2)

        # Test search
        search_res = self.obs.list_conversations(search_query="Quantum")
        self.assertTrue(any("Quantum" in c.title for c in search_res))

        c1.close()
        c2.close()
        c3.close()

    def test_12_mcp_inspection_does_not_launch_process(self):
        from runtime.mcp.models import MCPServerConfig
        cfg = MCPServerConfig(
            server_id="test_server_unstarted",
            name="Test Unstarted",
            command="cmd.exe",
            args=["/c", "dir"],
            enabled=True
        )
        self.runtime.mcp_supervisor.register_server(cfg)

        # Inspect catalog
        catalog = self.obs.get_mcp_catalog()
        srv = next((s for s in catalog if s.server_id == "test_server_unstarted"), None)
        self.assertIsNotNone(srv)
        self.assertEqual(srv.state, "STOPPED")
        self.assertFalse(srv.process_alive)
        self.assertIsNone(srv.process_id)

    def test_13_mcp_enable_disable_controls(self):
        from runtime.mcp.models import MCPServerConfig
        cfg = MCPServerConfig(
            server_id="test_srv_toggle",
            name="Test Toggle",
            command="cmd.exe",
            enabled=True
        )
        self.runtime.mcp_supervisor.register_server(cfg)

        # Disable
        success = self.obs.set_mcp_server_enabled("test_srv_toggle", False)
        self.assertTrue(success)
        self.assertFalse(self.runtime.mcp_supervisor._configs["test_srv_toggle"].enabled)

        # Re-enable
        success2 = self.obs.set_mcp_server_enabled("test_srv_toggle", True)
        self.assertTrue(success2)
        self.assertTrue(self.runtime.mcp_supervisor._configs["test_srv_toggle"].enabled)

    def test_14_search_circuit_breaker_telemetry_and_reset(self):
        from runtime.search.models import ResponseClassification
        gov = self.runtime.search_governor

        # Trip breaker into OPEN
        gov.circuit_breaker.record_failure(ResponseClassification.PROVIDER_CHALLENGED, "Challenge test")
        telemetry = self.obs.get_search_telemetry()
        self.assertEqual(telemetry["circuit_state"], "OPEN")
        self.assertGreater(telemetry["cooldown_remaining_seconds"], 0)

        # Reset circuit breaker via observability service
        reset_ok = self.obs.reset_search_circuit("duckduckgo")
        self.assertTrue(reset_ok)
        telemetry_after = self.obs.get_search_telemetry()
        self.assertEqual(telemetry_after["circuit_state"], "CLOSED")

    def test_15_unsupported_task_cancellation_reported(self):
        # Register a non-cancellable task
        task = self.obs.task_registry.register_task(
            task_type=TaskType.TOOL_EXECUTION,
            display_name="Critical File Write",
            cancellable=False
        )
        self.obs.task_registry.start_task(task.task_id)

        # Attempt to cancel
        cancelled = self.obs.request_task_cancellation(task.task_id)
        self.assertFalse(cancelled)
        self.assertEqual(task.state, TaskState.RUNNING)

    def test_16_tool_invocation_lifecycle_and_artifact_reference(self):
        # Create a session with large tool output to verify artifact generation & inspection
        large_content = "X" * 50000  # 50 KB
        fake_tool_provider = FakeModelProvider(scripted_responses=[
            [
                ModelStreamChunk(tool_calls=[{"call_id": "c1", "name": "test_large_tool", "args": {}}])
            ],
            [
                ModelStreamChunk(text_delta="Tool output processed!")
            ]
        ])

        from runtime.tools.registry import ToolDefinition
        from runtime.permissions import PermissionTier

        custom_tool = ToolDefinition(
            tool_id="test_large_tool",
            name="test_large_tool",
            description="Returns large payload",
            parameters={"type": "object", "properties": {}},
            authority_tier=PermissionTier.L0_OBSERVE,
            handler=lambda args: large_content
        )

        with self.runtime.create_session(
            title="Artifact Test Session",
            model_provider=fake_tool_provider,
            custom_tools=[custom_tool]
        ) as session:
            cid = session.conversation_id
            events = list(session.execute_turn("Run large tool"))

            turns = self.obs.get_conversation_turns(cid)
            self.assertEqual(len(turns), 1)
            self.assertEqual(len(turns[0].tool_calls), 1)
            tc = turns[0].tool_calls[0]
            self.assertEqual(tc["name"], "test_large_tool")
            self.assertIn("artifact_id", tc)

            # Check context inspection shows retrieved artifact
            ctx_dto = self.obs.inspect_turn_context(cid, turn_idx=1)
            self.assertGreater(len(ctx_dto.retrieved_data), 0)

    def test_17_event_queue_saturation_and_snapshot_recovery(self):
        bridge = ObservabilityEventBridge(max_buffer_size=50)
        for i in range(250):
            bridge.emit_synthetic(
                event_type=f"SyntheticEvent_{i}",
                payload={"index": i}
            )

        # Buffer bounded to 50
        self.assertEqual(bridge.buffer_size, 50)
        self.assertEqual(bridge.total_events, 250)

        # Most recent is index 249
        recent = bridge.get_recent_events(limit=5)
        self.assertEqual(recent[-1].event_type, "SyntheticEvent_249")

    def test_18_system_metrics_resource_measurement(self):
        metrics = self.obs.get_system_metrics()
        self.assertGreater(metrics["working_set_mb"], 0.0)
        self.assertGreaterEqual(metrics["threads"], 1)
        self.assertGreaterEqual(metrics["uptime_seconds"], 0.0)
        self.assertEqual(metrics["pid"], self.runtime.pid)


if __name__ == "__main__":
    unittest.main()
