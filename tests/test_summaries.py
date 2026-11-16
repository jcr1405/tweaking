"""Automated Unit Tests for Global Summaries & SQLite FTS5 Full-Text Index"""

import unittest
import tempfile
from pathlib import Path

from runtime.summaries import GlobalSummaryManager


class TestSummaries(unittest.TestCase):
    """Validates GlobalSummaryManager metadata and FTS5 search indexing."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_summaries.db"
        self.manager = GlobalSummaryManager(db_path=self.db_path)

    def tearDown(self):
        self.manager.close()
        self.temp_dir.cleanup()

    def test_summary_crud_and_status_updates(self):
        conv_id = "conv-summary-001"
        self.manager.upsert_summary(
            conversation_id=conv_id,
            title="Database Setup Session",
            preview="Configuring SQLite WAL mode",
            step_count=5,
            workspace_path=r"C:\workspace\db",
            status="ACTIVE",
            not_fully_idle=True
        )

        rec = self.manager.get_summary(conv_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["title"], "Database Setup Session")
        self.assertTrue(rec["not_fully_idle"])
        self.assertFalse(rec["killed"])

        # Update idle state
        self.manager.update_idle_state(conv_id, not_fully_idle=False)
        rec2 = self.manager.get_summary(conv_id)
        self.assertFalse(rec2["not_fully_idle"])

        # Update raw summary
        self.manager.update_raw_summary(conv_id, "User requested SQLite configuration. WAL mode enabled.")
        rec3 = self.manager.get_summary(conv_id)
        self.assertIn("WAL mode enabled", rec3["raw_summary"])

        # Update killed state
        self.manager.update_killed_state(conv_id, killed=True)
        rec4 = self.manager.get_summary(conv_id)
        self.assertTrue(rec4["killed"])
        self.assertEqual(rec4["status"], "KILLED")

    def test_subagent_lineage_listing(self):
        parent_id = "parent-conv-100"
        self.manager.upsert_summary(
            conversation_id=parent_id,
            title="Parent Coordinator",
            nesting_depth=0
        )

        self.manager.upsert_summary(
            conversation_id="child-conv-101",
            title="Research Child",
            parent_conversation_id=parent_id,
            nesting_depth=1
        )
        self.manager.upsert_summary(
            conversation_id="child-conv-102",
            title="Coding Child",
            parent_conversation_id=parent_id,
            nesting_depth=1
        )

        children = self.manager.list_subagents(parent_conversation_id=parent_id)
        self.assertEqual(len(children), 2)
        child_titles = [c["title"] for c in children]
        self.assertIn("Research Child", child_titles)
        self.assertIn("Coding Child", child_titles)

    def test_fts5_indexing_and_search_recall(self):
        conv_1 = "conv-fts-001"
        conv_2 = "conv-fts-002"

        self.manager.index_step_text(
            conversation_id=conv_1,
            step_idx=0,
            title="Rust vs Python Architecture",
            workspace_path=r"C:\projects\core",
            user_body="How does Python compare to Rust for our Windows desktop companion?",
            agent_body="Python 3.12 with Win32 ctypes achieves 23 MB RAM and 0.0% CPU."
        )

        self.manager.index_step_text(
            conversation_id=conv_2,
            step_idx=1,
            title="Browser Automation Setup",
            workspace_path=r"C:\projects\browser",
            user_body="Set up Playwright with Chrome Native Messaging.",
            agent_body="Configured manifest.json for extension communication."
        )

        # Query 1: Search for 'ctypes'
        results1 = self.manager.search_conversations("ctypes")
        self.assertGreaterEqual(len(results1), 1)
        self.assertEqual(results1[0]["conversation_id"], conv_1)
        self.assertIn("<b>ctypes</b>", results1[0]["agent_snippet"])

        # Query 2: Search for 'manifest.json'
        results2 = self.manager.search_conversations("manifest.json")
        self.assertGreaterEqual(len(results2), 1)
        self.assertEqual(results2[0]["conversation_id"], conv_2)

        # Query 3: Search with punctuation / quotes (resilience against syntax errors)
        results3 = self.manager.search_conversations('"Python 3.12"')
        self.assertGreaterEqual(len(results3), 1)
        self.assertEqual(results3[0]["conversation_id"], conv_1)

        # Query 4: Search for non-existent text
        results4 = self.manager.search_conversations("nonexistenttermXYZ")
        self.assertEqual(len(results4), 0)


if __name__ == "__main__":
    unittest.main()
