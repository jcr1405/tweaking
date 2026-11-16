"""Adversarial Verification Suite: MCP Transport Cancellation
Verifies that client cancellation and timeouts during active tool execution terminate cleanly.
"""

import sys
import unittest
import tempfile
from pathlib import Path

from runtime.mcp.models import MCPServerConfig
from runtime.mcp.client import MCPClientSession


class TestAdvTransportCancellation(unittest.TestCase):
    """Verifies cancellation propagation during active tool calls."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="adv_cancel_")
        self.fixture_file = Path(self.temp_dir) / "slow_server.py"

        code = (
            "import sys, json, time\n"
            "for line in sys.stdin:\n"
            "    req = json.loads(line)\n"
            "    method = req.get('method')\n"
            "    req_id = req.get('id')\n"
            "    if method == 'initialize':\n"
            "        resp = {'jsonrpc': '2.0', 'id': req_id, 'result': {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'srv', 'version': '1'}}}\n"
            "        sys.stdout.write(json.dumps(resp) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "    elif method == 'tools/list':\n"
            "        resp = {'jsonrpc': '2.0', 'id': req_id, 'result': {'tools': [{'name': 'sleep_tool', 'description': 'sleeps', 'inputSchema': {'type': 'object'}}]}}\n"
            "        sys.stdout.write(json.dumps(resp) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "    elif method == 'tools/call':\n"
            "        time.sleep(10.0)\n"
            "        resp = {'jsonrpc': '2.0', 'id': req_id, 'result': {'content': [{'type': 'text', 'text': 'done'}]}}\n"
            "        sys.stdout.write(json.dumps(resp) + '\\n')\n"
            "        sys.stdout.flush()\n"
        )
        self.fixture_file.write_text(code)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_tool_call_timeout_cancellation(self):
        """Verifies that an operation exceeding timeout raises TimeoutError and cancels in-flight future."""
        config = MCPServerConfig(
            server_id="slow_srv",
            name="Slow Server",
            command=sys.executable,
            args=[str(self.fixture_file)],
            startup_timeout=3.0,
            request_timeout=0.3
        )
        session = MCPClientSession(config)
        self.assertTrue(session.start())

        res = session.call_tool("sleep_tool", {}, timeout=0.2)
        self.assertEqual(res.get("status"), "ERROR")
        self.assertIn("timed out after 0.2s", res.get("error", ""))

        session.stop()

    def test_session_stop_during_in_flight_tool_call(self):
        """Verifies that stopping a session during an in-flight tool call completes within bounded time."""
        import threading
        config = MCPServerConfig(
            server_id="slow_srv_stop",
            name="Slow Server Stop",
            command=sys.executable,
            args=[str(self.fixture_file)],
            startup_timeout=3.0,
            request_timeout=5.0
        )
        session = MCPClientSession(config)
        self.assertTrue(session.start())

        # Start tool call in background thread
        tool_thread = threading.Thread(target=lambda: session.call_tool("sleep_tool", {}))
        tool_thread.start()

        import time
        time.sleep(0.2)  # Allow call to be submitted

        # Stop session abruptly
        stop_start = time.time()
        session.stop()
        stop_duration = time.time() - stop_start

        tool_thread.join(timeout=1.0)
        self.assertFalse(tool_thread.is_alive())
        self.assertLess(stop_duration, 3.0, "Session stop hung during in-flight tool execution")


if __name__ == "__main__":
    unittest.main()
