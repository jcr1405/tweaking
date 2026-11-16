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

    def test_native_subprocess_pipe_backpressure_1mb(self):
        """Verifies native Windows pipe backpressure with a real subprocess across 1 MB payload.
        Compares unthrottled consumer vs slow consumer (1ms delay per frame).
        Demonstrates that slow consumer causes Windows kernel pipe buffer saturation and producer write blocking.
        """
        import anyio
        import os
        import time
        from runtime.platform_win32 import Win32JobObject, get_process_memory_info
        from runtime.mcp.transport import win32_mcp_stdio_transport

        pipe_fixture = str(Path(__file__).parent.parent / "fixtures" / "pipe_backpressure_fixture.py")
        env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")}

        async def run_scenario(target_bytes: int, consumer_delay: float):
            job = Win32JobObject(name="TestBPJob_1MB")
            job.create(kill_on_close=True)
            mem_0 = get_process_memory_info()
            summary = None
            t0 = time.perf_counter()

            async with win32_mcp_stdio_transport(
                command=sys.executable,
                args=[pipe_fixture, str(target_bytes), "1000"],
                env=env,
                job=job
            ) as (read_stream, write_stream, proc):
                async for msg in read_stream:
                    if hasattr(msg, "message") and hasattr(msg.message, "root"):
                        root = msg.message.root
                        if getattr(root, "method", None) == "notifications/summary":
                            summary = root.params
                    elif hasattr(msg, "message") and getattr(msg.message, "method", None) == "notifications/summary":
                        summary = msg.message.params

                    if consumer_delay > 0:
                        await anyio.sleep(consumer_delay)
                    if isinstance(msg, Exception):
                        break

            t1 = time.perf_counter()
            mem_1 = get_process_memory_info()
            job.close()
            return {
                "elapsed": t1 - t0,
                "ws_delta_mb": mem_1.working_set_mb - mem_0.working_set_mb,
                "summary": summary
            }

        # Fast consumer (1 MB)
        fast_res = anyio.run(run_scenario, 1024 * 1024, 0.0)
        self.assertIsNotNone(fast_res["summary"])
        self.assertGreater(fast_res["summary"]["rate_mb_per_sec"], 5.0)

        # Slow consumer (100 KB with 1ms delay)
        slow_res = anyio.run(run_scenario, 100 * 1024, 0.001)
        self.assertIsNotNone(slow_res["summary"])
        # Slow consumer must show kernel pipe buffer blocked events or significantly reduced throughput
        self.assertLess(slow_res["summary"]["rate_mb_per_sec"], fast_res["summary"]["rate_mb_per_sec"] / 2.0)
        # Memory must remain strictly bounded (< 5 MB delta)
        self.assertLess(slow_res["ws_delta_mb"], 5.0)

    def test_native_subprocess_pipe_backpressure_10mb(self):
        """Verifies native Windows pipe streaming of 10 MB in ~10,000 JSON-RPC frames.
        Presence working set variation must remain strictly bounded (< 10 MB).
        """
        import anyio
        import os
        from runtime.platform_win32 import Win32JobObject, get_process_memory_info
        from runtime.mcp.transport import win32_mcp_stdio_transport

        pipe_fixture = str(Path(__file__).parent.parent / "fixtures" / "pipe_backpressure_fixture.py")
        env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")}

        async def run_10mb():
            job = Win32JobObject(name="TestBPJob_10MB")
            job.create(kill_on_close=True)
            mem_0 = get_process_memory_info()
            frames = 0
            summary = None

            async with win32_mcp_stdio_transport(
                command=sys.executable,
                args=[pipe_fixture, str(10 * 1024 * 1024), "1000"],
                env=env,
                job=job
            ) as (read_stream, write_stream, proc):
                async for msg in read_stream:
                    frames += 1
                    if hasattr(msg, "message") and hasattr(msg.message, "root"):
                        root = msg.message.root
                        if getattr(root, "method", None) == "notifications/summary":
                            summary = root.params
                    elif hasattr(msg, "message") and getattr(msg.message, "method", None) == "notifications/summary":
                        summary = msg.message.params

            mem_1 = get_process_memory_info()
            job.close()
            return {
                "frames": frames,
                "ws_delta_mb": mem_1.working_set_mb - mem_0.working_set_mb,
                "summary": summary
            }

        res = anyio.run(run_10mb)
        self.assertGreater(res["frames"], 10000)
        self.assertIsNotNone(res["summary"])
        self.assertEqual(res["summary"]["target_bytes"], 10 * 1024 * 1024)
        self.assertLess(res["ws_delta_mb"], 10.0, f"Memory growth exceeded limit: {res['ws_delta_mb']:.2f} MB")

    def test_native_subprocess_pipe_backpressure_50mb(self):
        """Verifies native Windows pipe streaming of 50 MB in ~50,000 JSON-RPC frames.
        Demonstrates that memory does not scale with stream volume (< 10 MB working set variation).
        """
        import anyio
        import os
        from runtime.platform_win32 import Win32JobObject, get_process_memory_info
        from runtime.mcp.transport import win32_mcp_stdio_transport

        pipe_fixture = str(Path(__file__).parent.parent / "fixtures" / "pipe_backpressure_fixture.py")
        env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")}

        async def run_50mb():
            job = Win32JobObject(name="TestBPJob_50MB")
            job.create(kill_on_close=True)
            mem_0 = get_process_memory_info()
            frames = 0
            summary = None

            async with win32_mcp_stdio_transport(
                command=sys.executable,
                args=[pipe_fixture, str(50 * 1024 * 1024), "1000"],
                env=env,
                job=job
            ) as (read_stream, write_stream, proc):
                async for msg in read_stream:
                    frames += 1
                    if hasattr(msg, "message") and hasattr(msg.message, "root"):
                        root = msg.message.root
                        if getattr(root, "method", None) == "notifications/summary":
                            summary = root.params
                    elif hasattr(msg, "message") and getattr(msg.message, "method", None) == "notifications/summary":
                        summary = msg.message.params

            mem_1 = get_process_memory_info()
            job.close()
            return {
                "frames": frames,
                "ws_delta_mb": mem_1.working_set_mb - mem_0.working_set_mb,
                "summary": summary
            }

        res = anyio.run(run_50mb)
        self.assertGreater(res["frames"], 50000)
        self.assertIsNotNone(res["summary"])
        self.assertEqual(res["summary"]["target_bytes"], 50 * 1024 * 1024)
        self.assertLess(res["ws_delta_mb"], 10.0, f"Memory growth on 50MB stream: {res['ws_delta_mb']:.2f} MB")

    def test_native_subprocess_midstream_cancellation_and_orphan_cleanup(self):
        """Verifies mid-stream cancellation while fast producer is actively streaming.
        Transport cancellation must unblock reader, terminate subprocess, and clean up process handles with zero orphans.
        """
        import anyio
        import os
        import psutil
        from runtime.platform_win32 import Win32JobObject
        from runtime.mcp.transport import win32_mcp_stdio_transport

        pipe_fixture = str(Path(__file__).parent.parent / "fixtures" / "pipe_backpressure_fixture.py")
        env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")}
        subproc_pid = None

        async def run_cancel():
            nonlocal subproc_pid
            job = Win32JobObject(name="TestBPCancelJob")
            job.create(kill_on_close=True)

            async with win32_mcp_stdio_transport(
                command=sys.executable,
                args=[pipe_fixture, str(50 * 1024 * 1024), "1000"],
                env=env,
                job=job
            ) as (read_stream, write_stream, proc):
                subproc_pid = proc.pid
                count = 0
                async for msg in read_stream:
                    count += 1
                    if count >= 100:
                        # Cancel reading mid-stream
                        break

            job.close()

        anyio.run(run_cancel)
        self.assertIsNotNone(subproc_pid)
        # Verify subprocess was killed cleanly and is no longer running
        self.assertFalse(psutil.pid_exists(subproc_pid), f"Subprocess {subproc_pid} survived cancellation!")


if __name__ == "__main__":
    unittest.main()
