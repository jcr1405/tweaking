"""Unit Tests for Persistence Engine (SQLite WAL Mode)
"""

import os
import unittest
import tempfile
import sqlite3
from pathlib import Path
from runtime.persistence import PersistenceManager


class TestPersistence(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_runtime.db"
        self.pm = PersistenceManager(self.db_path)

    def tearDown(self):
        self.pm.close()
        self.temp_dir.cleanup()

    def test_wal_mode_pragmas(self):
        cursor = self.pm._conn.execute("PRAGMA journal_mode;")
        mode = cursor.fetchone()[0]
        self.assertEqual(mode.lower(), "wal", "Database must initialize in WAL mode")

    def test_config_crud(self):
        # Set config
        self.pm.set_config("presence.mode", "ambient")
        self.pm.set_config("presence.hotkey", "Alt+Space")

        # Get config
        val1 = self.pm.get_config("presence.mode")
        val2 = self.pm.get_config("presence.hotkey")
        val3 = self.pm.get_config("nonexistent", default="default_val")

        self.assertEqual(val1, "ambient")
        self.assertEqual(val2, "Alt+Space")
        self.assertEqual(val3, "default_val")

        # Overwrite config
        self.pm.set_config("presence.mode", "focused")
        self.assertEqual(self.pm.get_config("presence.mode"), "focused")

        # Get all
        all_cfg = self.pm.get_all_config()
        self.assertIn("presence.mode", all_cfg)
        self.assertIn("presence.hotkey", all_cfg)

    def test_recovery_lifecycle_and_unclean_shutdown(self):
        instance_1 = "inst-001"
        self.pm.record_start(instance_1, pid=1234)
        self.pm.record_heartbeat(instance_1)

        # Instance 2 starts while Instance 1 did NOT cleanly shut down
        instance_2 = "inst-002"
        unclean = self.pm.detect_unclean_shutdowns(current_instance_id=instance_2)
        self.assertEqual(len(unclean), 1)
        self.assertEqual(unclean[0]["instance_id"], instance_1)
        self.assertEqual(unclean[0]["pid"], 1234)

        # Now clean shutdown instance 1
        self.pm.record_clean_shutdown(instance_1)
        unclean_after = self.pm.detect_unclean_shutdowns(current_instance_id=instance_2)
        self.assertEqual(len(unclean_after), 0)


if __name__ == "__main__":
    unittest.main()
