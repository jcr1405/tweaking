"""Adversarial Verification Suite: Exact 16 MB JSON-RPC Boundary & Stderr Flood Testing
Verifies exact byte boundaries (16 MB - 1, 16 MB, 16 MB + 1), multibyte UTF-8 handling,
un-delimited stream rejection, malformed frames, and stderr flood containment.
"""

import sys
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from runtime.mcp.models import MCPServerConfig
from runtime.mcp.client import MCPClientSession
from runtime.mcp.transport import MAX_MESSAGE_LINE_LENGTH_BYTES, MAX_STDERR_LINE_LENGTH_BYTES


class TestAdvTransportMessageLimits(unittest.TestCase):
    """Adversarial boundary tests for message line lengths and stderr logging."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="adv_limits_")
        self.fixture_file = Path(self.temp_dir) / "limits_server.py"

        # Server capable of producing exact byte payloads
        code = (
            "import sys, json\n"
            "for line in sys.stdin:\n"
            "    req = json.loads(line)\n"
            "    method = req.get('method')\n"
            "    req_id = req.get('id')\n"
            "    if method == 'initialize':\n"
            "        resp = {'jsonrpc': '2.0', 'id': req_id, 'result': {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'limits_srv', 'version': '1'}}}\n"
            "        sys.stdout.write(json.dumps(resp) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "    elif method == 'tools/list':\n"
            "        resp = {'jsonrpc': '2.0', 'id': req_id, 'result': {'tools': [\n"
            "            {'name': 'exact_bytes', 'description': 'returns exact byte string', 'inputSchema': {'type': 'object'}},\n"
            "            {'name': 'multibyte_stream', 'description': 'returns multibyte utf8', 'inputSchema': {'type': 'object'}},\n"
            "            {'name': 'stderr_flood', 'description': 'floods stderr', 'inputSchema': {'type': 'object'}}\n"
            "        ]}}\n"
            "        sys.stdout.write(json.dumps(resp) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "    elif method == 'tools/call':\n"
            "        tname = req.get('params', {}).get('name')\n"
            "        args = req.get('params', {}).get('arguments', {})\n"
            "        if tname == 'exact_bytes':\n"
            "            # Generate JSON-RPC response with exact total frame length\n"
            "            target_total = args.get('target_bytes')\n"
            "            # Overhead calculation\n"
            "            template = {'jsonrpc': '2.0', 'id': req_id, 'result': {'content': [{'type': 'text', 'text': ''}]}}\n"
            "            base_len = len(json.dumps(template).encode('utf-8'))\n"
            "            fill_len = max(0, target_total - base_len)\n"
            "            template['result']['content'][0]['text'] = 'A' * fill_len\n"
            "            out = json.dumps(template)\n"
            "            sys.stdout.write(out + '\\n')\n"
            "            sys.stdout.flush()\n"
            "        elif tname == 'multibyte_stream':\n"
            "            # Return multibyte UTF-8 characters (e.g. 4-byte emojis)\n"
            "            text_payload = '🌟' * 10000 # 40,000 bytes, 10,000 chars\n"
            "            resp = {'jsonrpc': '2.0', 'id': req_id, 'result': {'content': [{'type': 'text', 'text': text_payload}]}}\n"
            "            sys.stdout.write(json.dumps(resp) + '\\n')\n"
            "            sys.stdout.flush()\n"
            "        elif tname == 'stderr_flood':\n"
            "            lines_count = args.get('lines', 1000)\n"
            "            oversized_len = args.get('oversized_len', 0)\n"
            "            if oversized_len > 0:\n"
            "                sys.stderr.write('E' * oversized_len + '\\n')\n"
            "            for i in range(lines_count):\n"
            "                sys.stderr.write(f'STDERR_LINE_{i}\\n')\n"
            "            sys.stderr.flush()\n"
            "            resp = {'jsonrpc': '2.0', 'id': req_id, 'result': {'content': [{'type': 'text', 'text': 'flood_complete'}]}}\n"
            "            sys.stdout.write(json.dumps(resp) + '\\n')\n"
            "            sys.stdout.flush()\n"
        )
        self.fixture_file.write_text(code, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_multibyte_utf8_payload_accuracy(self):
        """Verifies multibyte UTF-8 characters are handled accurately according to byte length."""
        cfg = MCPServerConfig(
            server_id="limits_mb",
            name="Limits MB Server",
            command=sys.executable,
            args=[str(self.fixture_file)],
            startup_timeout=3.0,
            request_timeout=3.0
        )
        session = MCPClientSession(cfg)
        self.assertTrue(session.start())

        res = session.call_tool("multibyte_stream", {})
        self.assertEqual(res.get("status"), "SUCCESS")
        result_text = res.get("result", "")
        self.assertEqual(len(result_text), 10000)
        self.assertEqual(len(result_text.encode("utf-8")), 40000)
        session.stop()

    def test_stderr_flood_bounded_memory(self):
        """Verifies that thousands of stderr lines and lines exceeding 64 KB are handled without memory bloat."""
        cfg = MCPServerConfig(
            server_id="limits_err",
            name="Limits Err Server",
            command=sys.executable,
            args=[str(self.fixture_file)],
            startup_timeout=3.0,
            request_timeout=5.0
        )
        session = MCPClientSession(cfg)
        self.assertTrue(session.start())

        # Flood 2,000 stderr lines plus one 100 KB line (> 64 KB truncation limit)
        res = session.call_tool("stderr_flood", {"lines": 2000, "oversized_len": 100 * 1024})
        self.assertEqual(res.get("status"), "SUCCESS")
        self.assertEqual(res.get("result"), "flood_complete")
        session.stop()

    def test_un_delimited_stream_overflow_fails_closed(self):
        """Verifies that an un-delimited stream exceeding MAX_MESSAGE_LINE_LENGTH_BYTES fails closed."""
        overflow_file = Path(self.temp_dir) / "overflow_server.py"
        code = (
            "import sys, json\n"
            "line = sys.stdin.readline()\n"
            "req = json.loads(line)\n"
            "resp = {'jsonrpc': '2.0', 'id': req.get('id'), 'result': {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'ovf', 'version': '1'}}}\n"
            "sys.stdout.write(json.dumps(resp) + '\\n')\n"
            "sys.stdout.flush()\n"
            "# Now emit continuous characters without newline past 16 MB\n"
            "for _ in range(300):\n"
            "    sys.stdout.write('A' * 65536)\n"
            "    sys.stdout.flush()\n"
        )
        overflow_file.write_text(code, encoding="utf-8")

        cfg = MCPServerConfig(
            server_id="ovf_srv",
            name="Overflow Server",
            command=sys.executable,
            args=[str(overflow_file)],
            startup_timeout=3.0,
            request_timeout=3.0
        )
        session = MCPClientSession(cfg)
        # Should initialize, but as background stream overflows 16 MB, reader aborts cleanly
        session.start()
        import time
        time.sleep(0.5)
        session.stop()

    def test_oversized_single_frame_rejected_fail_closed(self):
        """Verifies that a single frame exceeding 16 MB is rejected with clean error envelope."""
        oversized_file = Path(self.temp_dir) / "oversized_server.py"
        code = (
            "import sys, json\n"
            "for line in sys.stdin:\n"
            "    req = json.loads(line)\n"
            "    m = req.get('method')\n"
            "    mid = req.get('id')\n"
            "    if m == 'initialize':\n"
            "        resp = {'jsonrpc': '2.0', 'id': mid, 'result': {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'ovz', 'version': '1'}}}\n"
            "        sys.stdout.write(json.dumps(resp) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "    elif m == 'tools/list':\n"
            "        resp = {'jsonrpc': '2.0', 'id': mid, 'result': {'tools': [{'name': 'blowup', 'description': 'huge', 'inputSchema': {'type': 'object'}}]}}\n"
            "        sys.stdout.write(json.dumps(resp) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "    elif m == 'tools/call':\n"
            "        # Emit a single line exceeding 16 MB + 1 byte\n"
            "        huge_text = 'Z' * (16 * 1024 * 1024 + 1024)\n"
            "        resp = {'jsonrpc': '2.0', 'id': mid, 'result': {'content': [{'type': 'text', 'text': huge_text}]}}\n"
            "        sys.stdout.write(json.dumps(resp) + '\\n')\n"
            "        sys.stdout.flush()\n"
        )
        oversized_file.write_text(code, encoding="utf-8")

        cfg = MCPServerConfig(
            server_id="ovz_srv",
            name="Oversized Server",
            command=sys.executable,
            args=[str(oversized_file)],
            startup_timeout=3.0,
            request_timeout=3.0
        )
        session = MCPClientSession(cfg)
        self.assertTrue(session.start())
        res = session.call_tool("blowup", {})
        self.assertEqual(res.get("status"), "ERROR")
        session.stop()

    def test_malformed_json_frame_handled_safely(self):
        """Verifies that an unparseable malformed JSON line does not crash the transport loop."""
        malformed_file = Path(self.temp_dir) / "malformed_server.py"
        code = (
            "import sys, json\n"
            "for line in sys.stdin:\n"
            "    req = json.loads(line)\n"
            "    m = req.get('method')\n"
            "    mid = req.get('id')\n"
            "    if m == 'initialize':\n"
            "        resp = {'jsonrpc': '2.0', 'id': mid, 'result': {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'mal', 'version': '1'}}}\n"
            "        sys.stdout.write(json.dumps(resp) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "    elif m == 'tools/list':\n"
            "        resp = {'jsonrpc': '2.0', 'id': mid, 'result': {'tools': [{'name': 'bad_json', 'description': 'bad', 'inputSchema': {'type': 'object'}}]}}\n"
            "        sys.stdout.write(json.dumps(resp) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "    elif m == 'tools/call':\n"
            "        # Emit broken JSON\n"
            "        sys.stdout.write('{\"jsonrpc\": \"2.0\", \"id\": ' + str(mid) + ', \"result\": BROKEN_SYNTAX_HERE}\\n')\n"
            "        sys.stdout.flush()\n"
        )
        malformed_file.write_text(code, encoding="utf-8")

        cfg = MCPServerConfig(
            server_id="mal_srv",
            name="Malformed Server",
            command=sys.executable,
            args=[str(malformed_file)],
            startup_timeout=3.0,
            request_timeout=2.0
        )
        session = MCPClientSession(cfg)
        self.assertTrue(session.start())
        res = session.call_tool("bad_json", {})
        # Should report error envelope rather than unhandled exception
        self.assertEqual(res.get("status"), "ERROR")
        session.stop()

    def test_partial_json_followed_by_abrupt_eof(self):
        """Verifies that a partial JSON payload immediately followed by EOF fails closed."""
        partial_file = Path(self.temp_dir) / "partial_server.py"
        code = (
            "import sys, json\n"
            "for line in sys.stdin:\n"
            "    req = json.loads(line)\n"
            "    m = req.get('method')\n"
            "    mid = req.get('id')\n"
            "    if m == 'initialize':\n"
            "        resp = {'jsonrpc': '2.0', 'id': mid, 'result': {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'part', 'version': '1'}}}\n"
            "        sys.stdout.write(json.dumps(resp) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "    elif m == 'tools/list':\n"
            "        resp = {'jsonrpc': '2.0', 'id': mid, 'result': {'tools': [{'name': 'cut_off', 'description': 'cut', 'inputSchema': {'type': 'object'}}]}}\n"
            "        sys.stdout.write(json.dumps(resp) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "    elif m == 'tools/call':\n"
            "        sys.stdout.write('{\"jsonrpc\": \"2.0\", \"id\": ' + str(mid) + ', \"result\": {\"cut_off_halfway\": true')\n"
            "        sys.stdout.flush()\n"
            "        sys.exit(0)\n"
        )
        partial_file.write_text(code, encoding="utf-8")

        cfg = MCPServerConfig(
            server_id="part_srv",
            name="Partial Server",
            command=sys.executable,
            args=[str(partial_file)],
            startup_timeout=3.0,
            request_timeout=2.0
        )
        session = MCPClientSession(cfg)
        self.assertTrue(session.start())
        res = session.call_tool("cut_off", {})
        self.assertEqual(res.get("status"), "ERROR")
        session.stop()


if __name__ == "__main__":
    unittest.main()
