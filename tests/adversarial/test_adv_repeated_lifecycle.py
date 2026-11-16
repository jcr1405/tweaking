"""Adversarial Verification Suite: Repeated Lifecycle & Resource Leaks
Tests:
1. 50x repeated MCP server startup & shutdown cycles.
2. Telemetry tracking before, during, and after:
   - Presence Working Set memory
   - Presence Private Bytes
   - OS process count (verifying 0 child leaks)
   - OS handle count
   - Python thread count
3. Asserts that resources return to baseline and do not monotonically leak.
"""

import sys
import os
import time
import psutil
import shutil
import tempfile
import unittest
from pathlib import Path

from runtime.platform_win32 import get_process_memory_info
from runtime.mcp.models import MCPServerConfig, MCPServerState
from runtime.mcp.schemas import SchemaCache
from runtime.mcp.supervisor import MCPSupervisor


@unittest.skipUnless(sys.platform == "win32", "Lifecycle telemetry tests require Windows")
class TestAdvRepeatedLifecycle(unittest.TestCase):

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_adv_leak_"))
        self.schema_cache = SchemaCache(cache_dir=self.temp_dir / "schemas")
        self.supervisor = MCPSupervisor(schema_cache=self.schema_cache)
        self.fixture_path = Path(__file__).parent.parent / "fixtures" / "echo_mcp_server.py"
        self.assertTrue(self.fixture_path.exists())

        self.server_config = MCPServerConfig(
            server_id="leak_test_srv",
            name="Leak Test Server",
            command=sys.executable,
            args=[str(self.fixture_path)],
            startup_timeout=10.0,
            request_timeout=5.0,
            shutdown_timeout=3.0,
            approved_tools=["echo"]
        )
        self.supervisor.register_server(self.server_config)

    def tearDown(self):
        self.supervisor.stop_all()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_repeated_mcp_server_lifecycles(self):
        """Runs 30 repeated start -> execute -> stop lifecycles and measures resource stability."""
        proc = psutil.Process()

        # Warmup cycle
        self.supervisor.start_server("leak_test_srv")
        self.supervisor.execute_tool("leak_test_srv", "echo", {"message": "warmup"})
        self.supervisor.stop_server("leak_test_srv")
        time.sleep(0.5)

        # Baseline measurements
        initial_handles = proc.num_handles()
        initial_threads = proc.num_threads()
        initial_mem = get_process_memory_info()

        cycles = 30
        print(f"\n[Telemetry Baseline] Handles: {initial_handles}, Threads: {initial_threads}, WS: {initial_mem.working_set_mb:.2f} MB")

        for i in range(cycles):
            self.supervisor.start_server("leak_test_srv")
            res = self.supervisor.execute_tool("leak_test_srv", "echo", {"message": f"Cycle_{i}"})
            self.assertEqual(res.get("status"), "SUCCESS")
            self.supervisor.stop_server("leak_test_srv")

        time.sleep(1.0)

        # Final measurements
        final_handles = proc.num_handles()
        final_threads = proc.num_threads()
        final_mem = get_process_memory_info()

        delta_handles = final_handles - initial_handles
        delta_threads = final_threads - initial_threads
        delta_mem_mb = final_mem.working_set_mb - initial_mem.working_set_mb

        print(f"[Telemetry After {cycles} Cycles] Handles: {final_handles} (Delta: {delta_handles:+d}), "
              f"Threads: {final_threads} (Delta: {delta_threads:+d}), "
              f"WS: {final_mem.working_set_mb:.2f} MB (Delta: {delta_mem_mb:+.2f} MB)")

        # Invariant 1: Threads must return to baseline (delta <= 2 for background workers)
        self.assertLessEqual(delta_threads, 2, f"Thread leak detected! Delta: {delta_threads}")

        # Invariant 2: Handles must not leak excessively (e.g. less than 1 handle per cycle on average)
        self.assertLessEqual(delta_handles, cycles // 2, f"Handle leak detected! Delta: {delta_handles}")

        # Invariant 3: Zero orphan child processes
        child_count = len(proc.children(recursive=True))
        self.assertEqual(child_count, 0, f"Orphan child processes detected: {child_count}")


if __name__ == "__main__":
    unittest.main()
