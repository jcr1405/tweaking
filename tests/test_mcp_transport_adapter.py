"""Unit & Integration Tests for Win32 MCP Stdio Transport Adapter
Verifies:
1. Subprocess launch via Win32SuspendedLauncher without monkey-patching.
2. Full MCP handshake (initialize, server info, capabilities negotiation).
3. Tool listing and execution.
4. Child process environment isolation.
5. Deterministic graceful shutdown.
"""

import sys
import os
import unittest
import anyio

from mcp import ClientSession
import mcp.client.stdio as mcp_stdio_mod

from runtime.platform_win32 import Win32JobObject
from runtime.mcp.transport import win32_mcp_stdio_transport
from runtime.mcp.client import build_isolated_environment


@unittest.skipUnless(sys.platform == "win32", "Win32 MCP transport requires Windows")
class TestWin32MCPTransportAdapter(unittest.TestCase):

    def setUp(self):
        self.fixture_server = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "tests",
            "fixtures",
            "echo_mcp_server.py"
        )
        self.assertTrue(os.path.exists(self.fixture_server), f"Fixture not found: {self.fixture_server}")

    def test_monkey_patching_not_used(self):
        """Verifies that the private SDK function is untouched."""
        self.assertFalse(
            hasattr(mcp_stdio_mod._create_platform_compatible_process, "__wrapped__"),
            "mcp.client.stdio._create_platform_compatible_process must NOT be monkey-patched."
        )

    def test_full_mcp_lifecycle_and_tool_call(self):
        """Tests initialize, list_tools, call_tool, and shutdown over Win32 suspended transport."""
        async def run_test():
            job = Win32JobObject(name="TestMCPTransportJob_001")
            self.assertTrue(job.create(kill_on_close=True))

            custom_env = {"PRESENCE_SECURE_TEST_VAR": "Verified_Win32"}
            isolated_env = build_isolated_environment(custom_env)

            async with win32_mcp_stdio_transport(
                command=sys.executable,
                args=[self.fixture_server],
                env=isolated_env,
                job=job,
            ) as (read_stream, write_stream, proc):
                self.assertIsNotNone(proc.pid)
                self.assertGreater(proc.pid, 0)

                async with ClientSession(read_stream, write_stream) as session:
                    # 1. Initialize
                    init_res = await session.initialize()
                    self.assertIsNotNone(init_res)
                    self.assertEqual(init_res.server_info.name, "test_fixture_server")

                    # 2. List tools
                    tools_res = await session.list_tools()
                    tool_names = [t.name for t in tools_res.tools]
                    self.assertIn("echo", tool_names)
                    self.assertIn("get_env_var", tool_names)
                    self.assertIn("generate_large_payload", tool_names)

                    # 3. Call tool: echo
                    echo_res = await session.call_tool("echo", {"message": "Hardened Transport Works"})
                    self.assertFalse(echo_res.is_error)
                    self.assertIn("ECHO: Hardened Transport Works", echo_res.content[0].text)

                    # 4. Call tool: verify isolated env var
                    env_res = await session.call_tool("get_env_var", {"var_name": "PRESENCE_SECURE_TEST_VAR"})
                    self.assertFalse(env_res.is_error)
                    self.assertEqual(env_res.content[0].text, "Verified_Win32")

            # Verify process is terminated and closed after transport exit
            self.assertIsNotNone(proc.poll())
            job.close()

        anyio.run(run_test)


if __name__ == "__main__":
    unittest.main()
