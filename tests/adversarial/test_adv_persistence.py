"""Adversarial Verification Suite: State Persistence, Crash Consistency & Transaction Safety
Verifies SQLite WAL durability, journal crash recovery, and monotonic indexing.
"""

import os
import sys
import shutil
import tempfile
import unittest
import sqlite3
from pathlib import Path

from runtime.persistence import PersistenceManager
from runtime.trajectory import TrajectoryStore, Step, StepType, StepStatus
from runtime.conversation import ConversationMetadata
from runtime.journal import EventJournal


class TestAdvPersistence(unittest.TestCase):
    """Adversarial stress and fault injection for the persistence layer."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="adv_persist_")
        self.db_dir = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_trajectory_wal_mode_and_crash_durability(self):
        """Verifies that WAL mode is strictly enforced and committed steps survive abrupt connection drop."""
        conv_id = "test-conv-crash-001"
        store = TrajectoryStore(conversation_id=conv_id, db_dir=self.db_dir)
        store.save_metadata(ConversationMetadata(conversation_id=conv_id, title="Crash Test"))

        # Write 5 steps
        for i in range(5):
            s = Step(
                idx=i,
                step_type=StepType.AGENT_TURN,
                status=StepStatus.DONE,
                step_payload={"message": f"turn_{i}"}
            )
            store.append_step(s)

        # Check WAL mode
        cursor = store._conn.execute("PRAGMA journal_mode;")
        mode = cursor.fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

        # Simulate abrupt process crash / connection severance without graceful close()
        store._conn.close()

        # Reopen with fresh store instance
        store2 = TrajectoryStore(conversation_id=conv_id, db_dir=self.db_dir)
        steps = store2.list_steps()
        self.assertEqual(len(steps), 5)
        self.assertEqual(steps[4].step_payload["message"], "turn_4")
        store2.close()

    def test_monotonic_step_index_integrity(self):
        """Verifies that inserting duplicate step index fails immediately with IntegrityError."""
        conv_id = "test-conv-monotonic-002"
        store = TrajectoryStore(conversation_id=conv_id, db_dir=self.db_dir)
        store.save_metadata(ConversationMetadata(conversation_id=conv_id, title="Monotonic Test"))

        s0 = Step(idx=0, step_type=StepType.USER_INPUT, status=StepStatus.DONE, step_payload={"input": "hi"})
        store.append_step(s0)

        # Attempt duplicate insertion of idx=0
        s0_dup = Step(idx=0, step_type=StepType.AGENT_TURN, status=StepStatus.DONE, step_payload={"input": "conflict"})
        with self.assertRaises(sqlite3.IntegrityError):
            store.append_step(s0_dup)

        store.close()

    def test_journal_partial_line_corruption_recovery(self):
        """Verifies that EventJournal handles truncated or half-written lines gracefully without crashing."""
        conv_id = "test-conv-journal-003"
        journal = EventJournal(conversation_id=conv_id, logs_dir=self.db_dir)

        # Append valid event
        journal.append_event(step_index=0, source="USER_EXPLICIT", event_type="USER_INPUT", status="DONE", content="hello")
        journal.close()

        # Inject truncated corrupt JSONL line (simulating power-cut / process kill mid-write)
        with open(journal.transcript_path, "a", encoding="utf-8") as f:
            f.write('{"event_type": "STEP_MUTATED", "payload": {"corrupt": true\n')  # Unclosed JSON

        # Append another valid event
        journal.append_event(step_index=1, source="MODEL", event_type="PLANNER_RESPONSE", status="DONE", content="world")
        journal.close()

        # Read events using reader
        records = journal.read_transcript()
        # EventJournal should return the 2 valid records and skip the corrupt line
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["step_index"], 0)
        self.assertEqual(records[1]["step_index"], 1)


if __name__ == "__main__":
    unittest.main()
