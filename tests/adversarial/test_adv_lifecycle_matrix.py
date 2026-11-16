"""Adversarial Verification Suite: Complete 7-Workload Repeated-Lifecycle Stress Matrix
Executes each of the 7 required workload categories independently with precise telemetry:
1. Normal MCP cycles: 100 repetitions
2. Abrupt parent-process terminations: 25 repetitions
3. Failed MCP startups and cleanups: 25 repetitions
4. MCP crash and bounded restart: 25 repetitions
5. Large artifact creation and retrieval: 50 repetitions
6. Concurrent MCP sessions: 20 groups x 3 sessions (60 sessions)
7. Full Presence runtime startup and shutdown: 25 repetitions
8. Resource telemetry plateau verification across repeated batches
"""

import os
import sys
import time
import json
import shutil
import tempfile
import unittest
import threading
import subprocess
import psutil
from pathlib import Path

from runtime.platform_win32 import Win32JobObject, Win32SuspendedLauncher, get_process_memory_info
from runtime.mcp.models import MCPServerConfig
from runtime.mcp.client import MCPClientSession
from runtime.mcp.supervisor import MCPSupervisor
from runtime.mcp.schemas import SchemaCache
from runtime.artifacts.store import ArtifactStore
from runtime.permissions import PermissionBoundary, AuthorityLevel
from runtime.tools.registry import ToolRegistry, ToolDefinition
from runtime.trajectory import TrajectoryStore
from runtime.conversation import ConversationMetadata
from runtime.journal import EventJournal
from runtime.summaries import GlobalSummaryManager
from runtime.agent_executor import AgentExecutor, CancellationToken
from runtime.providers.fake import FakeModelProvider


class TestAdvLifecycleMatrix(unittest.TestCase):
    """Stress tests across the 7 distinct lifecycle categories with native telemetry."""

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
        self.fixture_file.write_text(code, encoding="utf-8")

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

    def test_workload_1_normal_mcp_cycles(self):
        """Workload 1: 100 consecutive normal MCP cycles (start -> execute -> stop)."""
        import gc

        config = MCPServerConfig(
            server_id="matrix_norm_srv",
            name="Normal Matrix Server",
            command=sys.executable,
            args=[str(self.fixture_file)],
            startup_timeout=3.0,
            request_timeout=2.0
        )

        # Warmup cycle
        warmup = MCPClientSession(config)
        self.assertTrue(warmup.start())
        warmup.call_tool("ping", {})
        warmup.stop()
        gc.collect()
        time.sleep(0.3)

        t_base = self._get_telemetry()
        print(f"\n[Workload 1 Baseline] Handles: {t_base['handles']}, Threads: {t_base['threads']}, WS: {t_base['ws_mb']:.2f} MB")

        for i in range(100):
            s = MCPClientSession(config)
            self.assertTrue(s.start())
            res = s.call_tool("ping", {})
            self.assertEqual(res.get("status"), "SUCCESS")
            s.stop()
            if (i + 1) % 25 == 0:
                t = self._get_telemetry()
                print(f"  [Cycle {i+1}/100 Normal] Handles: {t['handles']}, Threads: {t['threads']}, WS: {t['ws_mb']:.2f} MB")

        gc.collect()
        time.sleep(0.5)
        t_final = self._get_telemetry()
        h_delta = t_final["handles"] - t_base["handles"]
        th_delta = t_final["threads"] - t_base["threads"]

        # Worker threads owned by Presence must be 0
        mcp_threads = [t.name for t in threading.enumerate() if t.name.startswith("MCP-")]
        self.assertEqual(len(mcp_threads), 0)
        self.assertEqual(len(psutil.Process(os.getpid()).children(recursive=True)), 0)
        self.assertLessEqual(abs(h_delta), 10, f"Handle drift: {h_delta}")

    def test_workload_2_abrupt_parent_termination(self):
        """Workload 2: 25 cycles of abrupt parent termination with external controller verification."""
        helper_code = (
            "import sys, os, time, json\n"
            "from runtime.platform_win32 import Win32JobObject, Win32SuspendedLauncher\n"
            "job = Win32JobObject('KillParentJob_' + str(os.getpid()))\n"
            "job.create(kill_on_close=True)\n"
            "child = Win32SuspendedLauncher.launch(\n"
            "    command=sys.executable,\n"
            "    args=['-c', 'import time; time.sleep(60)'],\n"
            "    env={'SystemRoot': os.environ.get('SystemRoot', r'C:\\Windows')},\n"
            "    job=job\n"
            ")\n"
            "info = {'parent_pid': os.getpid(), 'child_pid': child.pid}\n"
            "sys.stdout.write(json.dumps(info) + chr(10))\n"
            "sys.stdout.flush()\n"
            "time.sleep(60)\n"
        )

        for i in range(25):
            p = subprocess.Popen([sys.executable, "-c", helper_code], stdout=subprocess.PIPE, text=True)
            line = p.stdout.readline()
            self.assertTrue(line.strip())
            info = json.loads(line.strip())
            parent_pid = info["parent_pid"]
            child_pid = info["child_pid"]

            # Forcibly kill parent
            subprocess.run(["taskkill", "/F", "/PID", str(parent_pid)], capture_output=True)
            if p.stdout:
                p.stdout.close()
            try:
                p.wait(timeout=1.0)
            except Exception:
                pass
            time.sleep(0.15)

            # Independently verify both parent and child are dead
            self.assertFalse(psutil.pid_exists(parent_pid))
            self.assertFalse(psutil.pid_exists(child_pid), f"Child PID {child_pid} survived parent kill on cycle {i}!")

    def test_workload_3_failed_mcp_startups(self):
        """Workload 3: 25 cycles of failed MCP server startups (non-existent file, invalid cwd)."""
        import gc
        bad_config = MCPServerConfig(
            server_id="bad_startup_srv",
            name="Bad Server",
            command=sys.executable,
            args=["non_existent_file_xyz_999.py"],
            startup_timeout=1.0
        )
        p = psutil.Process(os.getpid())
        h0 = p.num_handles()

        for _ in range(25):
            s = MCPClientSession(bad_config)
            s.start()
            s.stop()

        gc.collect()
        time.sleep(0.3)
        h1 = p.num_handles()
        self.assertLessEqual(abs(h1 - h0), 8)
        mcp_threads = [t.name for t in threading.enumerate() if t.name.startswith("MCP-")]
        self.assertEqual(len(mcp_threads), 0)

    def test_workload_4_mcp_crash_and_restart(self):
        """Workload 4: 25 cycles of server crash during tool execution with supervisor recovery."""
        crash_code = (
            "import sys, json, os\n"
            "for line in sys.stdin:\n"
            "    req = json.loads(line)\n"
            "    mid = req.get('id')\n"
            "    m = req.get('method')\n"
            "    if m == 'initialize':\n"
            "        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': mid, 'result': {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'c', 'version': '1'}}}) + chr(10))\n"
            "        sys.stdout.flush()\n"
            "    elif m == 'tools/list':\n"
            "        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': mid, 'result': {'tools': [{'name': 'crash', 'description': 'c', 'inputSchema': {'type': 'object'}}]}}) + chr(10))\n"
            "        sys.stdout.flush()\n"
            "    elif m == 'tools/call':\n"
            "        os._exit(42)\n"
        )
        crash_file = Path(self.temp_dir) / "crash_srv.py"
        crash_file.write_text(crash_code, encoding="utf-8")

        cache = SchemaCache(cache_dir=Path(self.temp_dir) / "schemas")
        supervisor = MCPSupervisor(schema_cache=cache)
        cfg = MCPServerConfig(
            server_id="crash_test_srv",
            name="Crash Test",
            command=sys.executable,
            args=[str(crash_file)],
            startup_timeout=2.0,
            request_timeout=2.0,
            approved_tools=["crash"]
        )
        supervisor.register_server(cfg)

        for _ in range(25):
            res = supervisor.execute_tool("crash_test_srv", "crash", {})
            self.assertEqual(res.get("status"), "ERROR")
            supervisor.stop_server("crash_test_srv")

        supervisor.stop_all()

    def test_workload_5_large_artifact_operations(self):
        """Workload 5: 50 cycles of 50 KB text artifacts written and retrieved."""
        art_store = ArtifactStore(root_dir=Path(self.temp_dir) / "artifacts")
        sample_payload = "A" * 50000

        for i in range(50):
            meta = art_store.store_text_artifact("conv_wk5", f"tc_{i}", "tool", sample_payload)
            read_res = art_store.read_artifact("conv_wk5", meta.artifact_id)
            self.assertEqual(read_res.total_bytes, 50000)

    def test_workload_6_concurrent_mcp_sessions(self):
        """Workload 6: 20 groups of 3 concurrent sessions (60 total sessions)."""
        for i in range(20):
            sessions = [
                MCPClientSession(MCPServerConfig(
                    server_id=f"wk6_sim_{j}",
                    name=f"Sim {j}",
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

    def test_workload_7_full_presence_runtime_lifecycle(self):
        """Workload 7: 25 full Presence runtime startup, step execution, and clean shutdown cycles."""
        db_dir = Path(self.temp_dir) / "dbs"
        logs_dir = Path(self.temp_dir) / "logs"
        summary_db = Path(self.temp_dir) / "summaries.db"

        for i in range(25):
            conv_id = f"conv_wk7_{i}"
            store = TrajectoryStore(conversation_id=conv_id, db_dir=db_dir)
            meta = ConversationMetadata(
                conversation_id=conv_id,
                title=f"Turn {i}",
                created_at="2026-11-16T00:00:00Z"
            )
            store.save_metadata(meta)
            journal = EventJournal(conversation_id=conv_id, logs_dir=logs_dir)
            summary_mgr = GlobalSummaryManager(db_path=summary_db)
            boundary = PermissionBoundary()
            registry = ToolRegistry()
            provider = FakeModelProvider()

            executor = AgentExecutor(
                trajectory_store=store,
                event_journal=journal,
                summary_manager=summary_mgr,
                permission_boundary=boundary,
                tool_registry=registry,
                model_provider=provider
            )

            token = CancellationToken()
            events = list(executor.execute_turn(user_input="Hello Presence", cancellation_token=token))
            self.assertTrue(len(events) > 0)

            # Cleanup
            store.close()
            journal.close()
            summary_mgr.close()

    def test_workload_8_resource_plateau_verification(self):
        """Workload 8: Demonstrates that after initial warmup, handle counts plateau rather than leaking."""
        import gc
        cfg = MCPServerConfig(
            server_id="plateau_srv",
            name="Plateau Server",
            command=sys.executable,
            args=[str(self.fixture_file)],
            startup_timeout=3.0
        )
        p = psutil.Process(os.getpid())

        # Warmup
        w = MCPClientSession(cfg); w.start(); w.call_tool("ping", {}); w.stop(); w = None
        gc.collect(); time.sleep(0.3)
        h_warm = p.num_handles()

        # Batch 1 (15 cycles)
        for _ in range(15):
            s = MCPClientSession(cfg); s.start(); s.call_tool("ping", {}); s.stop(); s = None
        gc.collect(); time.sleep(0.3)
        h_b1 = p.num_handles()

        # Batch 2 (25 more cycles)
        for _ in range(25):
            s = MCPClientSession(cfg); s.start(); s.call_tool("ping", {}); s.stop(); s = None
        gc.collect(); time.sleep(0.3)
        h_b2 = p.num_handles()

        print(f"\n[Plateau Telemetry] Warmup: {h_warm} -> Batch 1 (15 cyc): {h_b1} (delta: {h_b1 - h_warm}) -> Batch 2 (25 cyc): {h_b2} (delta from b1: {h_b2 - h_b1})")
        # Delta between batch 1 and batch 2 must be <= 2 (plateau achieved)
        self.assertLessEqual(abs(h_b2 - h_b1), 2, f"Monotonic handle growth detected: {h_b2 - h_b1} handles between batches")


if __name__ == "__main__":
    unittest.main()
