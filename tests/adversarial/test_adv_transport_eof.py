"""Adversarial Verification Suite: MCP Stdio Transport EOF & Process Severance
Verifies that abrupt server exits before/during tool execution propagate EOF cleanly without deadlocking.
"""

import sys
import unittest
import tempfile
from pathlib import Path

from runtime.mcp.models import MCPServerConfig
from runtime.mcp.client import MCPClientSession


class TestAdvTransportEOF(unittest.TestCase):
    """Verifies EOF propagation and recovery under abrupt process termination."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="adv_eof_")
        self.fixture_file = Path(self.temp_dir) / "abrupt_server.py"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_eof_before_initialization(self):
        """Server immediately exits with code 0 before processing initialization handshake."""
        self.fixture_file.write_text("import sys; sys.exit(0)")

        config = MCPServerConfig(
            server_id="eof_pre_init",
            name="EOF Pre-Init",
            command=sys.executable,
            args=[str(self.fixture_file)],
            startup_timeout=2.0
        )
        session = MCPClientSession(config)
        started = session.start()
        # Must fail to start (not hang)
        self.assertFalse(started)
        session.stop()

    def test_eof_mid_tool_execution(self):
        """Server abruptly kills itself mid-tool execution. Verifies call_tool unblocks and reports error."""
        code = (
            "import sys, json, os\n"
            "for line in sys.stdin:\n"
            "    req = json.loads(line)\n"
            "    method = req.get('method')\n"
            "    req_id = req.get('id')\n"
            "    if method == 'initialize':\n"
            "        resp = {'jsonrpc': '2.0', 'id': req_id, 'result': {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'srv', 'version': '1'}}}\n"
            "        sys.stdout.write(json.dumps(resp) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "    elif method == 'tools/list':\n"
            "        resp = {'jsonrpc': '2.0', 'id': req_id, 'result': {'tools': [{'name': 'crash_me', 'description': 'crashes', 'inputSchema': {'type': 'object'}}]}}\n"
            "        sys.stdout.write(json.dumps(resp) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "    elif method == 'tools/call':\n"
            "        # Abrupt suicide without replying\n"
            "        os._exit(42)\n"
        )
        self.fixture_file.write_text(code)

        config = MCPServerConfig(
            server_id="eof_mid_tool",
            name="EOF Mid Tool",
            command=sys.executable,
            args=[str(self.fixture_file)],
            startup_timeout=3.0,
            request_timeout=2.0
        )
        session = MCPClientSession(config)
        self.assertTrue(session.start())

        # Call the tool that triggers suicide
        result = session.call_tool("crash_me", {})
        self.assertEqual(result.get("status"), "ERROR")
        err = result.get("error", "")
        self.assertTrue("exited unexpectedly" in err or "Connection closed" in err, f"Unexpected error: {err}")

        session.stop()


if __name__ == "__main__":
    unittest.main()
