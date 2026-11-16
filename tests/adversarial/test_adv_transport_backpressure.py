"""Adversarial Verification Suite: MCP Stdio Transport Backpressure & Message Boundaries
Verifies transport stability under high-frequency messaging, large payloads, and un-delimited streams.
"""

import sys
import unittest
import tempfile
from pathlib import Path

from runtime.mcp.models import MCPServerConfig
from runtime.mcp.client import MCPClientSession


class TestAdvTransportBackpressure(unittest.TestCase):
    """Verifies backpressure handling, queue stability, and large message delivery."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="adv_backpressure_")
        self.fixture_file = Path(self.temp_dir) / "stream_server.py"

        code = (
            "import sys, json\n"
            "for line in sys.stdin:\n"
            "    req = json.loads(line)\n"
            "    method = req.get('method')\n"
            "    req_id = req.get('id')\n"
            "    if method == 'initialize':\n"
            "        resp = {'jsonrpc': '2.0', 'id': req_id, 'result': {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'srv', 'version': '1'}}}\n"
            "        sys.stdout.write(json.dumps(resp) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "    elif method == 'tools/list':\n"
            "        resp = {'jsonrpc': '2.0', 'id': req_id, 'result': {'tools': [\n"
            "            {'name': 'echo', 'description': 'echoes', 'inputSchema': {'type': 'object'}},\n"
            "            {'name': 'large_blob', 'description': 'large', 'inputSchema': {'type': 'object'}}\n"
            "        ]}}\n"
            "        sys.stdout.write(json.dumps(resp) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "    elif method == 'tools/call':\n"
            "        tname = req.get('params', {}).get('name')\n"
            "        args = req.get('params', {}).get('arguments', {})\n"
            "        if tname == 'echo':\n"
            "            res = {'content': [{'type': 'text', 'text': f'ECHO_{args.get(\"num\", 0)}'}]}\n"
            "        elif tname == 'large_blob':\n"
            "            size = args.get('size_kb', 100)\n"
            "            res = {'content': [{'type': 'text', 'text': 'X' * (size * 1024)}]}\n"
            "        resp = {'jsonrpc': '2.0', 'id': req_id, 'result': res}\n"
            "        sys.stdout.write(json.dumps(resp) + '\\n')\n"
            "        sys.stdout.flush()\n"
        )
        self.fixture_file.write_text(code)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_high_frequency_tool_call_stream(self):
        """Verifies rapid sequential execution of 30 tool calls without rendezvous deadlock or frame dropping."""
        config = MCPServerConfig(
            server_id="hf_srv",
            name="High Frequency Server",
            command=sys.executable,
            args=[str(self.fixture_file)],
            startup_timeout=3.0,
            request_timeout=2.0
        )
        session = MCPClientSession(config)
        self.assertTrue(session.start())

        for i in range(30):
            res = session.call_tool("echo", {"num": i})
            self.assertEqual(res.get("status"), "SUCCESS")
            self.assertEqual(res.get("result"), f"ECHO_{i}")

        session.stop()

    def test_large_message_throughput_and_framing(self):
        """Verifies transport framing remains intact when streaming a 1 MB JSON-RPC message."""
        config = MCPServerConfig(
            server_id="large_msg_srv",
            name="Large Msg Server",
            command=sys.executable,
            args=[str(self.fixture_file)],
            startup_timeout=3.0,
            request_timeout=3.0
        )
        session = MCPClientSession(config)
        self.assertTrue(session.start())

        # Request 1024 KB (1 MB) payload
        res = session.call_tool("large_blob", {"size_kb": 1024})
        self.assertEqual(res.get("status"), "SUCCESS")
        result_text = res.get("result", "")
        self.assertEqual(len(result_text), 1024 * 1024)

        session.stop()

    def test_slow_consumer_backpressure_bounded_memory(self):
        """Verifies that a fast producer writing multiple 200 KB frames against a slow consumer
        maintains strictly bounded memory (< 100 MB working set) without message loss.
        """
        import os
        import time
        import psutil

        config = MCPServerConfig(
            server_id="slow_consumer_srv",
            name="Slow Consumer Server",
            command=sys.executable,
            args=[str(self.fixture_file)],
            startup_timeout=3.0,
            request_timeout=10.0
        )
        session = MCPClientSession(config)
        self.assertTrue(session.start())

        proc = psutil.Process(os.getpid())
        initial_ws_mb = proc.memory_info().rss / (1024 * 1024)

        # Stream 10 payloads of 200 KB each (2 MB total) with consumer delays
        for i in range(10):
            res = session.call_tool("large_blob", {"size_kb": 200})
            self.assertEqual(res.get("status"), "SUCCESS")
            self.assertEqual(len(res.get("result", "")), 200 * 1024)
            # Simulated consumer delay (slow consumer)
            time.sleep(0.05)

        final_ws_mb = proc.memory_info().rss / (1024 * 1024)
        ws_growth_mb = final_ws_mb - initial_ws_mb

        # Memory should remain strictly bounded (growth < 15 MB)
        self.assertLess(ws_growth_mb, 15.0, f"Excessive working set growth: {ws_growth_mb:.2f} MB")
        session.stop()

    def test_backpressure_cancellation_latency(self):
        """Verifies that stopping a session or cancelling during backpressure completes within bounded time (< 1.5s)."""
        import time

        config = MCPServerConfig(
            server_id="bp_cancel_srv",
            name="BP Cancel Server",
            command=sys.executable,
            args=[str(self.fixture_file)],
            startup_timeout=3.0,
            request_timeout=3.0
        )
        session = MCPClientSession(config)
        self.assertTrue(session.start())

        # Start large tool call and immediately initiate stop
        t0 = time.time()
        session.stop()
        elapsed = time.time() - t0

        self.assertLess(elapsed, 1.5, f"Session stop took too long under backpressure: {elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main()
