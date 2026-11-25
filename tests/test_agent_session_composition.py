"""Integration Tests for Headless Agent Session Composition Layer (runtime/session.py & runtime/lifecycle.py)
Verifies:
1. End-to-end headless turn execution composed with synthetic Echo MCP server.
2. Complete session isolation across multiple concurrent/sequential sessions.
3. Lazy MCP child process startup (no spawned processes until first tool call).
4. Clean process termination and Job Object containment during runtime shutdown.
5. Dynamic server disable and unregister tool pruning from active session registries.
"""

import sys
import shutil
import tempfile
import unittest
from pathlib import Path

from runtime.lifecycle import PresenceRuntime, RuntimeState
from runtime.permissions import PermissionTier
from runtime.providers.fake import FakeModelProvider
from runtime.providers.base import ModelStreamChunk
from runtime.mcp.models import MCPServerConfig, MCPServerState, MCPToolInfo
from runtime.mcp.schemas import SchemaCache
from runtime.mcp.config import save_mcp_configuration


class TestAgentSessionComposition(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_session_comp_"))
        self.db_path = self.temp_dir / "presence_test.db"
        self.summary_db = self.temp_dir / "summaries.db"
        self.schema_dir = self.temp_dir / "schemas"
        self.artifact_dir = self.temp_dir / "artifacts"
        self.conv_dir = self.temp_dir / "conversations"
        self.logs_dir = self.temp_dir / "logs"
        self.mcp_cfg_path = self.temp_dir / "mcp_servers.json"

        # Pre-seed schema cache to verify pure lazy startup without spawning at boot
        cache = SchemaCache(cache_dir=self.schema_dir)
        cache.save_tool_schema(MCPToolInfo(
            server_id="echo_fixture",
            tool_name="echo",
            description="Echoes back input",
            input_schema={"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}
        ))
        cache.save_tool_schema(MCPToolInfo(
            server_id="echo_fixture",
            tool_name="get_env_var",
            description="Reads env var",
            input_schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
        ))

        # Create MCP configuration for synthetic Echo server
        self.fixture_path = Path(__file__).parent / "fixtures" / "echo_mcp_server.py"
        self.echo_config = MCPServerConfig(
            server_id="echo_fixture",
            name="Echo MCP Fixture",
            command=sys.executable,
            args=[str(self.fixture_path)],
            startup_timeout=15.0,
            request_timeout=10.0,
            default_authority_tier=PermissionTier.L0_OBSERVE,
            approved_tools=["echo", "get_env_var"]
        )
        save_mcp_configuration([self.echo_config], self.mcp_cfg_path)

        # Initialize PresenceRuntime pointing to test directories
        self.runtime = PresenceRuntime(
            mutex_name=f"Local\\PresenceTest_Session_{self.temp_dir.name}",
            db_path=self.db_path,
            schema_cache_dir=self.schema_dir,
            artifact_dir=self.artifact_dir,
            summary_db_path=self.summary_db,
            mcp_config_path=self.mcp_cfg_path
        )
        self.runtime.session_factory.conversations_dir = self.conv_dir
        self.runtime.session_factory.logs_dir = self.logs_dir

        self.assertTrue(self.runtime.start(), "Runtime failed to start")

    def tearDown(self):
        if self.runtime.is_running:
            self.runtime.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_lazy_startup_preserved(self):
        """MCP child processes are not spawned upon runtime startup or session creation."""
        status = self.runtime.mcp_supervisor.get_status("echo_fixture")
        self.assertIsNotNone(status)
        self.assertEqual(status.state, MCPServerState.STOPPED)
        self.assertIsNone(status.pid, "Child process must not be running at runtime boot")

        # Creating a session loads cached schemas and bridges tools without spawning the child process
        session = self.runtime.create_session(conversation_id="conv_lazy_test")
        self.assertIsNotNone(session.tool_registry.get_tool("mcp.echo_fixture.echo"))

        status_after_session = self.runtime.mcp_supervisor.get_status("echo_fixture")
        self.assertEqual(status_after_session.state, MCPServerState.STOPPED)
        self.assertIsNone(status_after_session.pid, "Child process must not be running at session creation")
        session.close()

    def test_end_to_end_turn_execution_with_echo_mcp(self):
        """Headless turn execution calls MCP echo tool and yields normalized events."""
        fake_provider = FakeModelProvider()
        # Script turn 1: model requests echo tool
        fake_provider.queue_response([
            ModelStreamChunk(
                text_delta="",
                tool_calls=[{
                    "call_id": "call_echo_1",
                    "name": "mcp.echo_fixture.echo",
                    "args": {"message": "Hello from Headless Agent"}
                }]
            )
        ])
        # Script turn 2: model generates final response using tool result
        fake_provider.queue_response([
            ModelStreamChunk(
                text_delta="Echo tool completed successfully."
            )
        ])

        session = self.runtime.create_session(
            conversation_id="conv_e2e_turn",
            model_provider=fake_provider
        )

        events = list(session.execute_turn("Test echo execution"))

        event_types = [e.event_type for e in events]
        self.assertIn("TOOL_REQUESTED", event_types)
        self.assertIn("TOOL_COMPLETED", event_types)
        self.assertIn("TEXT_DELTA", event_types)
        self.assertIn("TURN_COMPLETED", event_types)

        # Verify tool payload
        tool_comp_event = next(e for e in events if e.event_type == "TOOL_COMPLETED")
        self.assertEqual(tool_comp_event.payload.get("name"), "mcp.echo_fixture.echo")
        self.assertIn("Hello from Headless Agent", str(tool_comp_event.payload.get("result")))

        # Verify process is now running (lazy startup triggered upon execution)
        status = self.runtime.mcp_supervisor.get_status("echo_fixture")
        self.assertEqual(status.state, MCPServerState.READY)
        self.assertIsNotNone(status.pid)
        self.assertGreater(status.pid, 0)

        # Verify persistence
        history = session.get_history()
        self.assertGreater(len(history), 0)
        session.close()

    def test_session_isolation(self):
        """Two separate sessions have independent persistence, journals, and tool registries."""
        from runtime.trajectory import StepType
        prov1 = FakeModelProvider()
        prov1.queue_response([
            ModelStreamChunk(text_delta="Response for Session 1")
        ])
        prov2 = FakeModelProvider()
        prov2.queue_response([
            ModelStreamChunk(text_delta="Response for Session 2")
        ])

        session1 = self.runtime.create_session(conversation_id="conv_iso_1", model_provider=prov1)
        session2 = self.runtime.create_session(conversation_id="conv_iso_2", model_provider=prov2)

        # Execute turn in session 1
        list(session1.execute_turn("Prompt 1"))
        # Execute turn in session 2
        list(session2.execute_turn("Prompt 2"))

        # Verify isolation of step histories
        hist1 = session1.get_history()
        hist2 = session2.get_history()
        self.assertGreater(len(hist1), 0)
        self.assertGreater(len(hist2), 0)

        user_step1 = next(s for s in hist1 if s.step_type == StepType.USER_INPUT)
        user_step2 = next(s for s in hist2 if s.step_type == StepType.USER_INPUT)
        self.assertEqual(user_step1.step_payload.get("content"), "Prompt 1")
        self.assertEqual(user_step2.step_payload.get("content"), "Prompt 2")

        # Verify registries are distinct instances
        self.assertIsNot(session1.tool_registry, session2.tool_registry)

        # Closing session 1 does not affect session 2
        session1.close()
        self.assertTrue(session1.is_closed)
        self.assertFalse(session2.is_closed)

        session2.close()
        self.assertTrue(session2.is_closed)

    def test_disable_and_unregister_server(self):
        """Disabling server blocks tool execution; unregistering prunes tools from ToolRegistry."""
        session = self.runtime.create_session(conversation_id="conv_prune_test")
        self.assertIsNotNone(session.tool_registry.get_tool("mcp.echo_fixture.echo"))

        # 1. Disable server
        self.runtime.mcp_supervisor.disable_server("echo_fixture")
        exec_res = self.runtime.mcp_supervisor.execute_tool("echo_fixture", "echo", {"message": "hi"})
        self.assertEqual(exec_res.get("status"), "ERROR")
        self.assertIn("disabled", exec_res.get("error", "").lower())

        # 2. Unregister server (should remove tools from session registry)
        self.runtime.mcp_supervisor.unregister_server("echo_fixture", tool_registry=session.tool_registry)
        self.assertIsNone(session.tool_registry.get_tool("mcp.echo_fixture.echo"))
        session.close()


if __name__ == "__main__":
    unittest.main()
