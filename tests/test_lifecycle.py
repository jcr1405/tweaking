"""Integration Tests for Full Presence Runtime Lifecycle & Concurrency
"""

import os
import unittest
import tempfile
import time
from pathlib import Path
from runtime.lifecycle import PresenceRuntime, RuntimeState
from runtime.logging import close_logger


class TestLifecycle(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_lifecycle.db"

    def tearDown(self):
        close_logger()
        time.sleep(0.05)
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_start_and_stop_lifecycle(self):
        mutex_name = f"Local\\TestLifecycleMutex_{os.getpid()}_{int(time.time())}"
        runtime = PresenceRuntime(mutex_name=mutex_name, db_path=self.db_path)

        self.assertEqual(runtime.state, RuntimeState.CREATED)

        # Start runtime
        started = runtime.start()
        self.assertTrue(started, "Runtime must start successfully")
        self.assertEqual(runtime.state, RuntimeState.RUNNING)
        self.assertTrue(runtime.is_running)

        # Health check
        health = runtime.get_health_status()
        self.assertEqual(health["state"], "RUNNING")
        self.assertGreater(health["memory"]["working_set_mb"], 1.0)
        self.assertGreaterEqual(health["events_published"], 1)

        # Stop runtime
        runtime.stop()
        self.assertEqual(runtime.state, RuntimeState.TERMINATED)
        self.assertFalse(runtime.is_running)

    def test_duplicate_instance_prevention(self):
        mutex_name = f"Local\\TestDuplicateMutex_{os.getpid()}_{int(time.time())}"
        r1 = PresenceRuntime(mutex_name=mutex_name, db_path=self.db_path)
        started_1 = r1.start()
        self.assertTrue(started_1, "Instance 1 must start")

        # Second instance with same mutex must fail to start
        db_path_2 = Path(self.temp_dir.name) / "test_lifecycle_2.db"
        r2 = PresenceRuntime(mutex_name=mutex_name, db_path=db_path_2)
        started_2 = r2.start()
        self.assertFalse(started_2, "Instance 2 must be rejected by single-instance mutex")
        self.assertEqual(r2.state, RuntimeState.TERMINATED)

        # Clean shutdown r1
        r1.stop()

        # Now r2 can start
        started_2_retry = r2.start()
        # Reset state from terminated to created for clean test if desired, or instantiate r3
        r3 = PresenceRuntime(mutex_name=mutex_name, db_path=db_path_2)
        started_3 = r3.start()
        self.assertTrue(started_3, "Instance 3 must start after Instance 1 released mutex")
        r3.stop()


if __name__ == "__main__":
    unittest.main()
