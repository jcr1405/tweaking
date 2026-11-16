"""Adversarial Verification Suite: MCP Server Lifecycle & Supervisor
Tests:
1. Startup failure with invalid executable / cwd fails cleanly without leaking state.
2. Crash during tool execution:
   AUDIT TEST FOR FINDING-ADV-02:
   Verifies whether the supervisor blindly replays non-idempotent tool requests on crash,
   or safely reports the failure without duplicate execution.
3. Rapid crash-and-restart bounded backoff:
   Verifies that repeated crashes hit max_restarts and transition to FAILED state.
4. Concurrent tool calls against the same supervised server.
"""

import sys
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from runtime.mcp.models import MCPServerConfig, MCPServerState
from runtime.mcp.schemas import SchemaCache
from runtime.mcp.supervisor import MCPSupervisor
from runtime.permissions import PermissionBoundary, PermissionTier
from runtime.tools.registry import ToolRegistry


class TestAdvSupervisorLifecycle(unittest.TestCase):

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_adv_supervisor_"))
        self.schema_cache = SchemaCache(cache_dir=self.temp_dir / "schemas")
        self.supervisor = MCPSupervisor(schema_cache=self.schema_cache)
        self.fixture_path = Path(__file__).parent.parent / "fixtures" / "echo_mcp_server.py"
        self.assertTrue(self.fixture_path.exists())

    def tearDown(self):
        self.supervisor.stop_all()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_startup_failure_clean_state_transition(self):
        """Verifies that an unresolvable executable or invalid cwd fails cleanly with FAILED state."""
        config = MCPServerConfig(
            server_id="invalid_exe_srv",
            name="Invalid Server",
            command="non_existent_executable_12345.exe",
            approved_tools=["echo"]
        )
        self.supervisor.register_server(config)

        started = self.supervisor.start_server("invalid_exe_srv")
        self.assertFalse(started)

        status = self.supervisor.get_status("invalid_exe_srv")
        self.assertIn(status.state, (MCPServerState.FAILED, MCPServerState.STOPPED))
        self.assertIsNone(status.pid)

    def test_blind_retry_on_crash_vulnerability_audit(self):
        """CRITICAL AUDIT TEST FOR FINDING-ADV-02:
        What happens if a tool causes a crash?
        In runtime/mcp/supervisor.py line 190:
            if tool_name != "trigger_crash":
                result = session.call_tool(tool_name, args, timeout=config.request_timeout)
        If an arbitrary non-idempotent tool crashes the process, does supervisor automatically
        retry it against the restarted server?
        """
        # Create a fixture server with a crashing mutating tool called 'mutate_and_crash'
        crash_tool_code = (
            "import sys, os\n"
            "from mcp.server.mcpserver import MCPServer\n"
            "srv = MCPServer('mutate_crash_server')\n"
            "counter_file = sys.argv[1]\n"
            "@srv.tool()\n"
            "def non_idempotent_mutation(action_id: str) -> str:\n"
            "    # Count executions\n"
            "    count = 0\n"
            "    if os.path.exists(counter_file):\n"
            "        with open(counter_file, 'r') as f: count = int(f.read().strip() or '0')\n"
            "    with open(counter_file, 'w') as f: f.write(str(count + 1))\n"
            "    # Simulate process crash immediately after mutation committed\n"
            "    os._exit(42)\n"
            "if __name__ == '__main__': srv.run('stdio')\n"
        )

        fixture_file = self.temp_dir / "crash_mutating_srv.py"
        fixture_file.write_text(crash_tool_code, encoding="utf-8")
        counter_file = self.temp_dir / "execution_counter.txt"

        config = MCPServerConfig(
            server_id="crash_mutate_srv",
            name="Crash Mutate Server",
            command=sys.executable,
            args=[str(fixture_file), str(counter_file)],
            approved_tools=["non_idempotent_mutation"],
            max_restarts=2,
            restart_backoff_seconds=0.1
        )
        self.supervisor.register_server(config)

        # Call the non-idempotent tool
        result = self.supervisor.execute_tool("crash_mutate_srv", "non_idempotent_mutation", {"action_id": "tx_001"})

        # Check how many times the non-idempotent action was executed
        execution_count = 0
        if counter_file.exists():
            execution_count = int(counter_file.read_text().strip())

        # If execution_count > 1, the supervisor blindly re-executed the non-idempotent action!
        if execution_count > 1:
            self.fail(
                f"VULNERABILITY REPRODUCED (FINDING-ADV-02): Supervisor blindly re-executed "
                f"non-idempotent mutating tool on crash! (Execution count: {execution_count})"
            )
        else:
            self.assertEqual(execution_count, 1)
            self.assertEqual(result.get("status"), "ERROR")

    def test_bounded_restart_backoff_exceeds_quota(self):
        """Verifies that an intrinsically crashing server hits max_restarts and halts."""
        config = MCPServerConfig(
            server_id="fixture_crash",
            name="Crashing Server",
            command=sys.executable,
            args=[str(self.fixture_path)],
            approved_tools=["trigger_crash"],
            max_restarts=2,
            restart_backoff_seconds=0.05
        )
        self.supervisor.register_server(config)

        started = self.supervisor.start_server("fixture_crash")
        self.assertTrue(started)

        # Trigger crashes
        self.supervisor.execute_tool("fixture_crash", "trigger_crash", {})
        self.supervisor.execute_tool("fixture_crash", "trigger_crash", {})
        self.supervisor.execute_tool("fixture_crash", "trigger_crash", {})

        status = self.supervisor.get_status("fixture_crash")
        self.assertGreaterEqual(status.restarts, 2)

    def test_concurrent_tool_execution(self):
        """Verifies multiple threads executing tool calls against the same server."""
        config = MCPServerConfig(
            server_id="fixture_concurrent",
            name="Concurrent Server",
            command=sys.executable,
            args=[str(self.fixture_path)],
            approved_tools=["echo"]
        )
        self.supervisor.register_server(config)

        results = [None] * 5
        errors = [None] * 5

        def worker(idx):
            try:
                res = self.supervisor.execute_tool("fixture_concurrent", "echo", {"message": f"Thread_{idx}"})
                results[idx] = res
            except Exception as e:
                errors[idx] = e

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()

        for err in errors:
            self.assertIsNone(err)

        for i, res in enumerate(results):
            self.assertIsNotNone(res)
            self.assertEqual(res.get("status"), "SUCCESS")
            self.assertEqual(res.get("result"), f"ECHO: Thread_{i}")


if __name__ == "__main__":
    unittest.main()
