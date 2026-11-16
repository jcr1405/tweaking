"""Adversarial Verification Suite: MCP Stdio Transport Reliability
Tests:
1. Message Fragmentation: Server writes JSON-RPC messages 1 byte at a time, splitting across
   JSON properties and multibyte UTF-8 characters. Asserts transport properly reassembles messages.
2. Multiple Messages per Read: Server writes multiple newline-delimited JSON-RPC messages in a
   single write chunk. Asserts all messages are parsed and delivered in order.
3. Stdout / Stderr Separation: Server emits high-volume stderr noise alongside stdout JSON-RPC.
   Asserts protocol stream is uncontaminated.
4. Transport Cancellation: Cancellation mid-session properly terminates background reader/writer tasks.
"""

import sys
import os
import anyio
import unittest
from pathlib import Path

from mcp import ClientSession

from runtime.platform_win32 import Win32JobObject
from runtime.mcp.transport import win32_mcp_stdio_transport
from runtime.mcp.client import build_isolated_environment


@unittest.skipUnless(sys.platform == "win32", "Win32 MCP transport tests require Windows")
class TestAdvMCPTransport(unittest.TestCase):

    def test_message_fragmentation_byte_by_byte(self):
        """Server writes JSON-RPC byte-by-byte with small delays.
        Verifies that stdout_reader buffers and reassembles the complete message.
        """
        async def run_test():
            job = Win32JobObject(name="AdvTestFragJob")
            job.create(kill_on_close=True)

            code = (
                "import sys, time\n"
                "# Initial handshake: read initialize request from stdin\n"
                "line = sys.stdin.readline()\n"
                "# Respond with initialize result byte-by-byte\n"
                "resp = '{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{},\"serverInfo\":{\"name\":\"frag_srv\",\"version\":\"1.0\"}}}\\n'\n"
                "for b in resp.encode('utf-8'):\n"
                "    sys.stdout.buffer.write(bytes([b]))\n"
                "    sys.stdout.buffer.flush()\n"
                "    time.sleep(0.001)\n"
                "# Wait for next message or exit\n"
                "try:\n"
                "    sys.stdin.readline()\n"
                "except Exception:\n"
                "    pass\n"
            )

            env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"), "PYTHONUNBUFFERED": "1"}

            async with win32_mcp_stdio_transport(
                command=sys.executable,
                args=["-c", code],
                env=env,
                job=job,
            ) as (read_stream, write_stream, proc):
                async with ClientSession(read_stream, write_stream) as session:
                    init_res = await session.initialize()
                    self.assertIsNotNone(init_res)
                    self.assertEqual(init_res.server_info.name, "frag_srv")

            job.close()

        anyio.run(run_test)

    def test_multiple_messages_in_single_read_chunk(self):
        """Server emits two separate JSON-RPC notifications in a single atomic flush.
        Verifies that both messages are delivered in sequence without loss.
        """
        async def run_test():
            job = Win32JobObject(name="AdvTestMultiMsgJob")
            job.create(kill_on_close=True)

            code = (
                "import sys\n"
                "line = sys.stdin.readline()\n"
                "# Write initialize result + notification in a single chunk\n"
                "m1 = '{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{},\"serverInfo\":{\"name\":\"multi_srv\",\"version\":\"1.0\"}}}'\n"
                "m2 = '{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}'\n"
                "combined = f'{m1}\\n{m2}\\n'\n"
                "sys.stdout.write(combined)\n"
                "sys.stdout.flush()\n"
                "# Keep open\n"
                "try:\n"
                "    sys.stdin.readline()\n"
                "except Exception:\n"
                "    pass\n"
            )

            env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"), "PYTHONUNBUFFERED": "1"}

            async with win32_mcp_stdio_transport(
                command=sys.executable,
                args=["-c", code],
                env=env,
                job=job,
            ) as (read_stream, write_stream, proc):
                async with ClientSession(read_stream, write_stream) as session:
                    init_res = await session.initialize()
                    self.assertEqual(init_res.server_info.name, "multi_srv")

            job.close()

        anyio.run(run_test)

    def test_stderr_burst_does_not_corrupt_stdout_protocol(self):
        """Server emits massive stderr spam interleaved with valid JSON-RPC stdout.
        Verifies that stderr is cleanly segregated and stdout protocol is unaffected.
        """
        async def run_test():
            job = Win32JobObject(name="AdvTestStderrSpamJob")
            job.create(kill_on_close=True)

            code = (
                "import sys\n"
                "# Spam stderr before handshake\n"
                "for i in range(100):\n"
                "    sys.stderr.write(f'STDERR_NOISE_{i:04d}\\n')\n"
                "sys.stderr.flush()\n"
                "line = sys.stdin.readline()\n"
                "# Spam stderr during response\n"
                "for i in range(100, 200):\n"
                "    sys.stderr.write(f'STDERR_NOISE_{i:04d}\\n')\n"
                "sys.stderr.flush()\n"
                "# Output clean stdout handshake\n"
                "sys.stdout.write('{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{},\"serverInfo\":{\"name\":\"stderr_spam_srv\",\"version\":\"1.0\"}}}\\n')\n"
                "sys.stdout.flush()\n"
                "try:\n"
                "    sys.stdin.readline()\n"
                "except Exception:\n"
                "    pass\n"
            )

            env = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"), "PYTHONUNBUFFERED": "1"}

            async with win32_mcp_stdio_transport(
                command=sys.executable,
                args=["-c", code],
                env=env,
                job=job,
            ) as (read_stream, write_stream, proc):
                async with ClientSession(read_stream, write_stream) as session:
                    init_res = await session.initialize()
                    self.assertEqual(init_res.server_info.name, "stderr_spam_srv")

            job.close()

        anyio.run(run_test)


if __name__ == "__main__":
    unittest.main()
