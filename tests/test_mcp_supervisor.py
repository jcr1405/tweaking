"""Tests for MCP Process Supervisor & Subprocess Containment
Verifies MCP server lifecycle, Win32 Job Object process-tree containment,
environment variable secret isolation, crash recovery backoff, and ToolRegistry bridging.
"""

import os
import sys
import time
import shutil
import tempfile
import unittest
from pathlib import Path

from runtime.mcp.models import MCPServerConfig, MCPServerState
from runtime.mcp.schemas import SchemaCache
from runtime.mcp.supervisor import MCPSupervisor
from runtime.permissions import PermissionBoundary, PermissionTier
from runtime.tools.registry import ToolRegistry


class TestMCPSupervisor(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_mcp_supervisor_"))
        self.schema_cache = SchemaCache(cache_dir=self.temp_dir / "schemas")
        self.supervisor = MCPSupervisor(schema_cache=self.schema_cache)

        # Path to test fixture server
        fixture_path = Path(__file__).parent / "fixtures" / "echo_mcp_server.py"
        self.assertTrue(fixture_path.exists(), f"Fixture missing at {fixture_path}")

        self.server_config = MCPServerConfig(
            server_id="fixture",
            name="Test Echo MCP Server",
            command=sys.executable,
            args=[str(fixture_path)],
            startup_timeout=15.0,
            request_timeout=10.0,
            shutdown_timeout=5.0,
            max_restarts=2,
            restart_backoff_seconds=0.2,
            default_authority_tier=PermissionTier.L0_OBSERVE
        )
        self.supervisor.register_server(self.server_config)

    def tearDown(self):
        self.supervisor.stop_all()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_server_lifecycle_and_stdio_execution(self):
        """Verifies start, tool discovery, tool execution, and clean shutdown."""
        # Start server
        started = self.supervisor.start_server("fixture")
        self.assertTrue(started)

        status = self.supervisor.get_status("fixture")
        self.assertEqual(status.state, MCPServerState.READY)
        self.assertIsNotNone(status.pid)
        self.assertGreater(status.tools_count, 0)

        # Call tool via supervisor
        result = self.supervisor.execute_tool("fixture", "echo", {"message": "Presence Test"})
        self.assertEqual(result.get("status"), "SUCCESS")
        self.assertEqual(result.get("result"), "ECHO: Presence Test")

        # Verify schemas were cached to disk
        cached_tools = self.schema_cache.load_server_schemas("fixture")
        tool_names = [t.tool_name for t in cached_tools]
        self.assertIn("echo", tool_names)
        self.assertIn("generate_large_payload", tool_names)

        # Stop server
        stopped = self.supervisor.stop_server("fixture")
        self.assertTrue(stopped)
        final_status = self.supervisor.get_status("fixture")
        self.assertEqual(final_status.state, MCPServerState.STOPPED)

    def test_environment_isolation_strips_secrets(self):
        """Verifies that API keys and secrets are never inherited or passed to child processes."""
        # Set sensitive environment variables in parent process
        os.environ["GEMINI_API_KEY"] = "super_secret_gemini_key_never_leak"
        os.environ["ANTHROPIC_API_KEY"] = "super_secret_anthropic_key"
        os.environ["PRESENCE_AUTH_TOKEN"] = "token_xyz"

        try:
            # Also attempt to configure a secret in server config's custom env
            self.server_config.env = {
                "SAFE_USER_FLAG": "presence_active",
                "MY_SECRET_KEY": "should_be_stripped"
            }

            started = self.supervisor.start_server("fixture")
            self.assertTrue(started)

            # Query the child process for GEMINI_API_KEY
            res_gemini = self.supervisor.execute_tool("fixture", "get_env_var", {"var_name": "GEMINI_API_KEY"})
            self.assertEqual(res_gemini.get("result"), "<NOT_SET>")

            # Query for MY_SECRET_KEY
            res_secret = self.supervisor.execute_tool("fixture", "get_env_var", {"var_name": "MY_SECRET_KEY"})
            self.assertEqual(res_secret.get("result"), "<NOT_SET>")

            # Query for SAFE_USER_FLAG
            res_safe = self.supervisor.execute_tool("fixture", "get_env_var", {"var_name": "SAFE_USER_FLAG"})
            self.assertEqual(res_safe.get("result"), "presence_active")

        finally:
            os.environ.pop("GEMINI_API_KEY", None)
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("PRESENCE_AUTH_TOKEN", None)

    def test_process_containment_in_win32_job_object(self):
        """Verifies that the child process is contained and fully terminated upon session stop."""
        started = self.supervisor.start_server("fixture")
        self.assertTrue(started)

        session = self.supervisor._sessions["fixture"]
        pid = session.pid
        self.assertIsNotNone(pid)
        self.assertTrue(session.is_alive())

        # Stop session
        self.supervisor.stop_server("fixture")
        time.sleep(0.5)

        # Process must no longer be running
        self.assertFalse(session.is_alive())
        import psutil
        self.assertFalse(psutil.pid_exists(pid))

    def test_crash_recovery_and_restart(self):
        """Verifies supervisor detects crash and restarts server automatically."""
        started = self.supervisor.start_server("fixture")
        self.assertTrue(started)

        initial_session = self.supervisor._sessions["fixture"]
        initial_pid = initial_session.pid

        # Trigger crash inside the server
        crash_res = self.supervisor.execute_tool("fixture", "trigger_crash", {})

        # Because supervisor retried after crash, it should have restarted
        status = self.supervisor.get_status("fixture")
        self.assertGreaterEqual(status.restarts, 1)

    def test_tool_registry_namespacing_and_execution(self):
        """Verifies bridging MCP tools into ToolRegistry under 'mcp.<server_id>.<tool_name>' namespace."""
        registry = ToolRegistry()
        boundary = PermissionBoundary()

        registered_names = self.supervisor.register_tools_with_registry(
            tool_registry=registry,
            permission_boundary=boundary,
            server_id="fixture"
        )

        expected_namespaced = "mcp.fixture.echo"
        self.assertIn(expected_namespaced, registered_names)

        # Verify tool can be retrieved from registry
        tool_def = registry.get_tool(expected_namespaced)
        self.assertIsNotNone(tool_def)
        self.assertEqual(tool_def.authority_tier, PermissionTier.L0_OBSERVE)

        # Execute via ToolRegistry
        exec_res = registry.execute(
            tool_id=expected_namespaced,
            args={"message": "Bridged Registry Execution"},
            permission_boundary=boundary
        )

        self.assertEqual(exec_res.get("status"), "SUCCESS")
        self.assertEqual(exec_res.get("result"), "ECHO: Bridged Registry Execution")


if __name__ == "__main__":
    unittest.main()
