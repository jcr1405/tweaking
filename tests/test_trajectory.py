"""Automated Unit Tests for Trajectory Storage & Step Monotonicity"""

import unittest
import tempfile
import sqlite3
from pathlib import Path

from runtime.conversation import ConversationMetadata
from runtime.trajectory import TrajectoryStore, Step, StepType, StepStatus


class TestTrajectory(unittest.TestCase):
    """Validates TrajectoryStore database operations and monotonic step integrity."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_dir = Path(self.temp_dir.name)
        self.conv_id = "test-conv-001"
        self.store = TrajectoryStore(conversation_id=self.conv_id, db_dir=self.db_dir)

    def tearDown(self):
        self.store.close()
        self.temp_dir.cleanup()

    def test_wal_mode_and_pragmas(self):
        cursor = self.store._conn.execute("PRAGMA journal_mode;")
        journal_mode = cursor.fetchone()[0]
        self.assertEqual(journal_mode.lower(), "wal")

    def test_metadata_persistence(self):
        meta = ConversationMetadata(
            conversation_id=self.conv_id,
            title="Trajectory Test Session",
            workspace_path=r"C:\test\workspace"
        )
        self.store.save_metadata(meta)

        retrieved = self.store.get_metadata()
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.conversation_id, self.conv_id)
        self.assertEqual(retrieved.title, "Trajectory Test Session")
        self.assertEqual(retrieved.workspace_path, r"C:\test\workspace")

    def test_monotonic_step_indexing(self):
        idx0 = self.store.get_next_step_index()
        self.assertEqual(idx0, 0)

        step0 = Step(
            idx=idx0,
            step_type=StepType.USER_INPUT,
            status=StepStatus.DONE,
            step_payload={"content": "List files in directory"}
        )
        committed_idx0 = self.store.append_step(step0)
        self.assertEqual(committed_idx0, 0)

        idx1 = self.store.get_next_step_index()
        self.assertEqual(idx1, 1)

        step1 = Step(
            idx=idx1,
            step_type=StepType.AGENT_TURN,
            status=StepStatus.DONE,
            step_payload={"content": "I will examine the folder contents.", "thinking": "Planning command"}
        )
        committed_idx1 = self.store.append_step(step1)
        self.assertEqual(committed_idx1, 1)

        self.assertEqual(self.store.get_step_count(), 2)

    def test_duplicate_step_index_rejection(self):
        step0 = Step(
            idx=0,
            step_type=StepType.USER_INPUT,
            step_payload={"content": "Original prompt"}
        )
        self.store.append_step(step0)

        # Attempting to insert another step with idx=0 must fail closed
        duplicate = Step(
            idx=0,
            step_type=StepType.AGENT_TURN,
            step_payload={"content": "Duplicate step"}
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.append_step(duplicate)

    def test_step_replay_and_ordering(self):
        for i in range(5):
            self.store.append_step(Step(
                idx=i,
                step_type=StepType.USER_INPUT if i % 2 == 0 else StepType.AGENT_TURN,
                status=StepStatus.DONE,
                step_payload={"content": f"Message turn {i}"}
            ))

        replayed = self.store.list_steps(start_idx=0)
        self.assertEqual(len(replayed), 5)
        for i, step in enumerate(replayed):
            self.assertEqual(step.idx, i)
            self.assertEqual(step.step_payload["content"], f"Message turn {i}")

        subset = self.store.list_steps(start_idx=2, limit=2)
        self.assertEqual(len(subset), 2)
        self.assertEqual(subset[0].idx, 2)
        self.assertEqual(subset[1].idx, 3)

    def test_step_status_update(self):
        step = Step(
            idx=0,
            step_type=StepType.TOOL_CALL,
            status=StepStatus.PENDING,
            step_payload={"action": "run_command", "args": {"cmd": "dir"}}
        )
        self.store.append_step(step)

        success = self.store.update_step_status(0, StepStatus.DONE)
        self.assertTrue(success)

        retrieved = self.store.get_step(0)
        self.assertEqual(retrieved.status, StepStatus.DONE)

    def test_generation_metadata_and_permission_audit(self):
        step = Step(
            idx=0,
            step_type=StepType.AGENT_TURN,
            status=StepStatus.DONE,
            step_payload={"content": "Executed tool"}
        )
        self.store.append_step(step)

        self.store.record_generation_metadata(
            step_idx=0,
            prompt_tokens=150,
            completion_tokens=45,
            cached_tokens=100,
            duration_ms=420.5,
            model_name="gemini-flash",
            provider_name="google"
        )

        self.store.record_permission_audit(
            permission_id="perm-001",
            step_idx=0,
            action="fs.read_file",
            tier="L0_OBSERVE",
            decision="GRANTED",
            justification="Read-only operation allowed autonomously"
        )


if __name__ == "__main__":
    unittest.main()
