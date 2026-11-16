"""End-to-End Tests for AgentExecutor + MCP Supervisor + Artifact Offload Integration
Verifies that:
1. AgentExecutor can call bridged MCP tools via FakeProvider scripted tool calls.
2. Large tool outputs exceeding 40 KB are offloaded to disk as persistent artifacts.
3. The model receives a preview envelope with retrieval instructions.
4. The model can subsequently invoke 'presence.read_artifact' to inspect chunks of the offloaded artifact.
"""

import sys
import shutil
import tempfile
import unittest
from pathlib import Path

from runtime.conversation import ConversationMetadata
from runtime.trajectory import TrajectoryStore, StepType, StepStatus
from runtime.journal import EventJournal
from runtime.summaries import GlobalSummaryManager
from runtime.reconstruction import ContextAssembler
from runtime.permissions import PermissionBoundary, PermissionTier
from runtime.tools.registry import ToolRegistry, build_default_tools
from runtime.providers.fake import FakeModelProvider
from runtime.providers.base import ModelStreamChunk
from runtime.agent_executor import AgentExecutor
from runtime.artifacts.store import ArtifactStore
from runtime.mcp.models import MCPServerConfig
from runtime.mcp.schemas import SchemaCache
from runtime.mcp.supervisor import MCPSupervisor


class TestAgentMCPIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_agent_mcp_"))
        self.conv_id = "conv_mcp_integration_01"

        # Trajectory & Storage
        self.trajectory_store = TrajectoryStore(
            conversation_id=self.conv_id,
            db_dir=self.temp_dir / "conversations"
        )
        self.meta = ConversationMetadata(
            conversation_id=self.conv_id,
            title="MCP Integration Test",
            workspace_path=str(self.temp_dir)
        )
        self.trajectory_store.save_metadata(self.meta)

        self.journal = EventJournal(
            conversation_id=self.conv_id,
            logs_dir=self.temp_dir / "logs"
        )

        self.summary_db = self.temp_dir / "summaries.db"
        self.summary_manager = GlobalSummaryManager(db_path=self.summary_db)

        self.artifact_store = ArtifactStore(root_dir=self.temp_dir / "artifacts")
        self.schema_cache = SchemaCache(cache_dir=self.temp_dir / "schemas")

        # Supervisor with test fixture server
        self.supervisor = MCPSupervisor(schema_cache=self.schema_cache)
        fixture_path = Path(__file__).parent / "fixtures" / "echo_mcp_server.py"
        self.server_config = MCPServerConfig(
            server_id="fixture",
            name="Fixture Echo MCP",
            command=sys.executable,
            args=[str(fixture_path)],
            startup_timeout=15.0,
            request_timeout=10.0,
            default_authority_tier=PermissionTier.L0_OBSERVE,
            approved_tools=["echo", "generate_large_payload", "get_env_var"]
        )
        self.supervisor.register_server(self.server_config)

        # Tool Registry & Permissions
        self.permission_boundary = PermissionBoundary()
        self.registry = ToolRegistry()

        # Bridge MCP tools into registry
        self.supervisor.register_tools_with_registry(
            tool_registry=self.registry,
            permission_boundary=self.permission_boundary,
            server_id="fixture"
        )

        self.fake_provider = FakeModelProvider()
        self.assembler = ContextAssembler(
            trajectory_store=self.trajectory_store,
            summary_manager=self.summary_manager
        )

        self.executor = AgentExecutor(
            trajectory_store=self.trajectory_store,
            event_journal=self.journal,
            summary_manager=self.summary_manager,
            permission_boundary=self.permission_boundary,
            tool_registry=self.registry,
            model_provider=self.fake_provider,
            context_assembler=self.assembler,
            artifact_store=self.artifact_store
        )

    def tearDown(self):
        self.supervisor.stop_all()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_mcp_tool_execution_in_agent_turn(self):
        """Tests that the agent executor invokes a bridged MCP tool and records results."""
        # Turn 1: Script model to request mcp.fixture.echo, then respond with greeting
        self.fake_provider.queue_response([
            ModelStreamChunk(tool_calls=[{
                "call_id": "call_mcp_1",
                "name": "mcp.fixture.echo",
                "args": {"message": "Agent Calling MCP"}
            }])
        ])
        self.fake_provider.queue_response([
            ModelStreamChunk(text_delta="Echo response received successfully!")
        ])

        events = list(self.executor.execute_turn(user_input="Please call the echo tool."))
        event_types = [e.event_type for e in events]

        self.assertIn("TOOL_REQUESTED", event_types)
        self.assertIn("TOOL_COMPLETED", event_types)
        self.assertIn("TURN_COMPLETED", event_types)

        # Verify steps in SQLite trajectory
        steps = self.trajectory_store.list_steps()
        tool_call_step = next(s for s in steps if s.step_type == StepType.TOOL_CALL)
        tool_result_step = next(s for s in steps if s.step_type == StepType.TOOL_RESULT)

        self.assertEqual(tool_call_step.step_payload["name"], "mcp.fixture.echo")
        self.assertEqual(tool_result_step.step_payload["content"], "ECHO: Agent Calling MCP")

    def test_large_mcp_output_offloaded_to_artifact_and_retrieved(self):
        """Tests that tool output > 40 KB is offloaded to ArtifactStore and retrieved via presence.read_artifact."""
        self.fake_provider.queue_response([
            ModelStreamChunk(tool_calls=[{
                "call_id": "call_large_1",
                "name": "mcp.fixture.generate_large_payload",
                "args": {"line_count": 1200}
            }])
        ])

        captured_events = []
        for ev in self.executor.execute_turn(user_input="Generate large diagnostic report."):
            captured_events.append(ev)
            if ev.event_type == "TOOL_COMPLETED" and ev.payload.get("name") == "mcp.fixture.generate_large_payload":
                artifact_id = ev.payload.get("artifact_id")
                self.assertIsNotNone(artifact_id)
                self.assertTrue(ev.payload.get("offloaded"))

                # Now queue response to read the artifact!
                self.fake_provider.queue_response([
                    ModelStreamChunk(tool_calls=[{
                        "call_id": "call_read_art",
                        "name": "presence.read_artifact",
                        "args": {"artifact_id": artifact_id, "offset": 0, "limit": 5, "mode": "lines"}
                    }])
                ])
                # And terminal text response
                self.fake_provider.queue_response([
                    ModelStreamChunk(text_delta="Read 5 lines of offloaded diagnostic artifact successfully.")
                ])

        # Check trajectory steps
        steps = self.trajectory_store.list_steps()
        result_steps = [s for s in steps if s.step_type == StepType.TOOL_RESULT]
        self.assertGreaterEqual(len(result_steps), 2)

        # First result step must be the offloaded artifact
        large_step = result_steps[0]
        self.assertIn("artifact_id", large_step.step_payload)
        self.assertGreater(large_step.step_payload["byte_size"], 40960)
        self.assertIn("TRUNCATED", large_step.step_payload["content"])

        # Second result step must be the read_artifact output
        read_step = result_steps[1]
        self.assertEqual(read_step.step_payload["name"], "presence.read_artifact")
        self.assertIn("Line 00000", read_step.step_payload["content"])

        # Check disk artifact
        artifacts = self.artifact_store.list_artifacts(self.conv_id)
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].artifact_id, large_step.step_payload["artifact_id"])


if __name__ == "__main__":
    unittest.main()
