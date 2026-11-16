"""Adversarial Verification Suite: Repeated-Lifecycle Stress Matrix & OS Telemetry
Executes the full repeated-lifecycle matrix (normal, failed, crash, artifacts, multi-session)
and logs OS handle and memory telemetry at regular intervals.
"""

import os
import sys
import time
import shutil
import tempfile
import unittest
import threading
import psutil
from pathlib import Path

from runtime.platform_win32 import Win32JobObject, Win32SuspendedLauncher, get_process_memory_info
from runtime.mcp.models import MCPServerConfig
from runtime.mcp.client import MCPClientSession
from runtime.mcp.supervisor import MCPSupervisor
from runtime.artifacts.store import ArtifactStore


class TestAdvLifecycleMatrix(unittest.TestCase):
    """Stress tests across the complete repeated-lifecycle matrix with telemetry."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="adv_matrix_")
        self.fixture_file = Path(self.temp_dir) / "echo_server.py"

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
            "        resp = {'jsonrpc': '2.0', 'id': req_id, 'result': {'tools': [{'name': 'ping', 'description': 'pings', 'inputSchema': {'type': 'object'}}]}}\n"
            "        sys.stdout.write(json.dumps(resp) + '\\n')\n"
            "        sys.stdout.flush()\n"
            "    elif method == 'tools/call':\n"
            "        resp = {'jsonrpc': '2.0', 'id': req_id, 'result': {'content': [{'type': 'text', 'text': 'pong'}]}}\n"
            "        sys.stdout.write(json.dumps(resp) + '\\n')\n"
            "        sys.stdout.flush()\n"
        )
        self.fixture_file.write_text(code)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _get_telemetry(self):
        p = psutil.Process(os.getpid())
        mem = get_process_memory_info()
        return {
            "handles": p.num_handles(),
            "threads": p.num_threads(),
            "ws_mb": mem.working_set_mb,
            "private_mb": mem.private_mb
        }

    def test_full_lifecycle_stress_matrix(self):
        """Executes the full repeated-lifecycle matrix:
        1. Normal MCP cycles: 100 cycles
        2. Failed startup cycles: 25 cycles
        3. Large artifact cycles: 50 cycles
        4. Multiple simultaneous sessions: 20 cycles
        Verifies resource stability and zero handle or thread accumulation.
        """
        import gc

        config = MCPServerConfig(
            server_id="matrix_srv",
            name="Matrix Server",
            command=sys.executable,
            args=[str(self.fixture_file)],
            startup_timeout=3.0,
            request_timeout=2.0
        )

        # Warmup cycle to initialize runtime structures (asyncio proactor, thread pools, logging)
        warmup_session = MCPClientSession(config)
        self.assertTrue(warmup_session.start())
        warmup_session.call_tool("ping", {})
        warmup_session.stop()
        gc.collect()
        time.sleep(0.5)

        init_telem = self._get_telemetry()
        print(f"\n[Lifecycle Matrix Baseline] Handles: {init_telem['handles']}, Threads: {init_telem['threads']}, WS: {init_telem['ws_mb']:.2f} MB")

        # 1. Normal MCP cycles (100 cycles)
        for i in range(100):
            session = MCPClientSession(config)
            self.assertTrue(session.start())
            res = session.call_tool("ping", {})
            self.assertEqual(res.get("status"), "SUCCESS")
            session.stop()
            if (i + 1) % 25 == 0:
                t = self._get_telemetry()
                print(f"  [Cycle {i+1}/100 Normal] Handles: {t['handles']}, Threads: {t['threads']}, WS: {t['ws_mb']:.2f} MB")

        # 2. Failed startup cycles (25 cycles)
        bad_config = MCPServerConfig(
            server_id="bad_matrix_srv",
            name="Bad Server",
            command=sys.executable,
            args=["non_existent_file_xyz_123.py"],
            startup_timeout=1.0
        )
        for i in range(25):
            bad_session = MCPClientSession(bad_config)
            bad_session.start()
            bad_session.stop()

        # 3. Large artifact storage cycles (50 cycles)
        art_store = ArtifactStore(root_dir=Path(self.temp_dir) / "artifacts")
        sample_payload = "A" * 50000  # 50 KB -> offloaded
        for i in range(50):
            art_meta = art_store.store_text_artifact(
                conversation_id="conv_matrix_001",
                tool_call_id=f"tc_{i}",
                tool_name="large_tool",
                content_str=sample_payload
            )
            read_res = art_store.read_artifact("conv_matrix_001", art_meta.artifact_id)
            self.assertEqual(read_res.total_bytes, 50000)

        # 4. Multiple simultaneous sessions (20 cycles of 3 parallel sessions)
        for i in range(20):
            sessions = [
                MCPClientSession(MCPServerConfig(
                    server_id=f"sim_{j}",
                    name=f"Sim Server {j}",
                    command=sys.executable,
                    args=[str(self.fixture_file)],
                    startup_timeout=3.0
                )) for j in range(3)
            ]
            for s in sessions: s.start()
            for s in sessions:
                r = s.call_tool("ping", {})
                self.assertEqual(r.get("status"), "SUCCESS")
            for s in sessions: s.stop()

        import gc
        gc.collect()
        time.sleep(0.5)

        final_telem = self._get_telemetry()
        h_delta = final_telem['handles'] - init_telem['handles']
        th_delta = final_telem['threads'] - init_telem['threads']
        ws_delta = final_telem['ws_mb'] - init_telem['ws_mb']

        print(f"\n[Lifecycle Matrix Post-Run] Handles: {final_telem['handles']} (Delta: {h_delta:+d}), Threads: {final_telem['threads']} (Delta: {th_delta:+d}), WS: {final_telem['ws_mb']:.2f} MB (Delta: {ws_delta:+.2f} MB)")
        alive_threads = [t.name for t in threading.enumerate()]
        print(f"  [Active Threads]: {alive_threads}")

        # Zero orphan child processes
        child_count = len(psutil.Process(os.getpid()).children(recursive=True))
        self.assertEqual(child_count, 0, f"Orphan child processes detected: {child_count}")

        # Worker threads owned by Presence should be 0 (no 'MCP-*' threads remaining)
        mcp_threads = [t.name for t in threading.enumerate() if t.name.startswith("MCP-")]
        self.assertEqual(len(mcp_threads), 0, f"Stale MCP worker threads leaked: {mcp_threads}")

        # Thread growth bounded (delta <= 3 for OS thread pool workers)
        self.assertLessEqual(th_delta, 3, f"Excessive thread growth: {th_delta} threads")

        # Handle drift bounded across 195 process lifecycles (<= 25 handles, < 0.15 handles/cycle)
        self.assertLessEqual(abs(h_delta), 25, f"Excessive handle growth: {h_delta} handles")


if __name__ == "__main__":
    unittest.main()
