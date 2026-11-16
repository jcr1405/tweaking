"""Opt-In Live Integration Test for Gemini + MCP Tool Calling
Skipped automatically if GEMINI_API_KEY is not set in environment.
"""

import os
import sys
import shutil
import tempfile
import unittest
from pathlib import Path

from runtime.conversation import ConversationMetadata
from runtime.trajectory import TrajectoryStore, StepType
from runtime.journal import EventJournal
from runtime.summaries import GlobalSummaryManager
from runtime.permissions import PermissionBoundary, PermissionTier
from runtime.tools.registry import build_default_tools
from runtime.providers.gemini import GeminiModelProvider, _GENAI_AVAILABLE
from runtime.agent_executor import AgentExecutor
from runtime.artifacts.store import ArtifactStore
from runtime.mcp.models import MCPServerConfig
from runtime.mcp.schemas import SchemaCache
from runtime.mcp.supervisor import MCPSupervisor


class TestMCPLive(unittest.TestCase):
    def test_live_gemini_mcp_invocation(self):
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise unittest.SkipTest("GEMINI_API_KEY environment variable not set. Skipping live test.")

        if not _GENAI_AVAILABLE:
            raise unittest.SkipTest("google-genai SDK not available. Skipping live test.")

        temp_dir = Path(tempfile.mkdtemp(prefix="test_mcp_live_"))
        conv_id = "live-mcp-unit-conv"

        try:
            store = TrajectoryStore(conversation_id=conv_id, db_dir=temp_dir / "conversations")
            meta = ConversationMetadata(conversation_id=conv_id, title="Live MCP Test")
            store.save_metadata(meta)

            journal = EventJournal(conversation_id=conv_id, logs_dir=temp_dir / "logs")
            summary_mgr = GlobalSummaryManager(db_path=temp_dir / "summaries.db")
            artifact_store = ArtifactStore(root_dir=temp_dir / "artifacts")
            schema_cache = SchemaCache(cache_dir=temp_dir / "schemas")

            supervisor = MCPSupervisor(schema_cache=schema_cache)
            fixture_path = Path(__file__).parent / "fixtures" / "echo_mcp_server.py"

            server_config = MCPServerConfig(
                server_id="echo_srv",
                name="Echo MCP Server",
                command=sys.executable,
                args=[str(fixture_path)],
                startup_timeout=15.0,
                request_timeout=10.0,
                default_authority_tier=PermissionTier.L0_OBSERVE
            )
            supervisor.register_server(server_config)

            boundary = PermissionBoundary()
            registry = build_default_tools(permission_boundary=boundary)
            supervisor.register_tools_with_registry(registry, boundary, "echo_srv")

            provider = GeminiModelProvider(api_key=api_key, model_id="gemini-3.5-flash", temperature=0.0)

            executor = AgentExecutor(
                trajectory_store=store,
                event_journal=journal,
                summary_manager=summary_mgr,
                permission_boundary=boundary,
                tool_registry=registry,
                model_provider=provider,
                artifact_store=artifact_store
            )

            events = list(executor.execute_turn(
                user_input="Use the echo_srv tool to echo 'Presence Live MCP Verified'."
            ))

            event_types = [e.event_type for e in events]
            self.assertIn("TOOL_REQUESTED", event_types)
            self.assertIn("TOOL_COMPLETED", event_types)
            self.assertIn("TURN_COMPLETED", event_types)

            # Check SQLite
            steps = store.list_steps()
            tool_calls = [s for s in steps if s.step_type == StepType.TOOL_CALL]
            self.assertTrue(len(tool_calls) > 0)
            self.assertEqual(tool_calls[0].step_payload["name"], "mcp.echo_srv.echo")

            supervisor.stop_all()
            store.close()
            journal.close()
            summary_mgr.close()
            provider.close()

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
