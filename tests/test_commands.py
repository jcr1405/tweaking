"""Unit Tests for Deterministic Command Dispatcher
"""

import unittest
import tempfile
from pathlib import Path
from runtime.permissions import PermissionBoundary
from runtime.commands import CommandDispatcher
from runtime.persistence import PersistenceManager


class MockRuntime:
    def __init__(self, persistence):
        self.persistence = persistence
        self.stopped = False

    def get_health_status(self):
        return {
            "state": "RUNNING",
            "instance_id": "test-mock-inst",
            "pid": 9999,
            "uptime_seconds": 12.34,
            "memory": {"working_set_mb": 28.5, "private_mb": 14.2},
            "events_published": 5,
            "clean_recovery": True,
        }

    def stop(self):
        self.stopped = True


class TestCommands(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_cmd.db"
        self.persistence = PersistenceManager(self.db_path)
        self.permissions = PermissionBoundary()
        self.mock_runtime = MockRuntime(self.persistence)
        self.dispatcher = CommandDispatcher(self.permissions, runtime_ref=self.mock_runtime)

    def tearDown(self):
        self.persistence.close()
        self.temp_dir.cleanup()

    def test_status_command(self):
        res = self.dispatcher.execute("status")
        self.assertTrue(res.success)
        self.assertIn("RUNNING", res.output)
        self.assertIn("28.50 MB", res.output)

    def test_help_command(self):
        res = self.dispatcher.execute("help")
        self.assertTrue(res.success)
        self.assertIn("status", res.output)
        self.assertIn("config", res.output)
        self.assertIn("shutdown", res.output)

    def test_config_commands(self):
        # Set config
        res_set = self.dispatcher.execute("config workspace.root C:\\projects\\aethertime")
        self.assertTrue(res_set.success)
        self.assertIn("workspace.root = C:\\projects\\aethertime", res_set.output)

        # Get config
        res_get = self.dispatcher.execute("config workspace.root")
        self.assertTrue(res_get.success)
        self.assertEqual(res_get.data["value"], "C:\\projects\\aethertime")

        # Get all
        res_all = self.dispatcher.execute("config")
        self.assertTrue(res_all.success)
        self.assertIn("workspace.root", res_all.output)

    def test_shutdown_command(self):
        res = self.dispatcher.execute("shutdown")
        self.assertTrue(res.success)
        self.assertTrue(self.mock_runtime.stopped)

    def test_unknown_command(self):
        res = self.dispatcher.execute("nonexistent_command_xyz")
        self.assertFalse(res.success)
        self.assertEqual(res.error, "COMMAND_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
