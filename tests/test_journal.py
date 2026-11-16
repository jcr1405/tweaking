"""Automated Unit Tests for Streaming Event Journal (transcript.jsonl)"""

import unittest
import tempfile
from pathlib import Path

from runtime.journal import EventJournal
from runtime.trajectory import Step, StepType, StepStatus


class TestJournal(unittest.TestCase):
    """Validates EventJournal streaming operations and dual-journal reconciliation."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.logs_dir = Path(self.temp_dir.name)
        self.conv_id = "test-journal-001"
        self.journal = EventJournal(conversation_id=self.conv_id, logs_dir=self.logs_dir)

    def tearDown(self):
        self.journal.close()
        self.temp_dir.cleanup()

    def test_append_and_read_event(self):
        rec = self.journal.append_event(
            step_index=0,
            source="USER_EXPLICIT",
            event_type="USER_INPUT",
            status="DONE",
            content="Hello companion"
        )
        self.assertEqual(rec["step_index"], 0)
        self.assertEqual(rec["content"], "Hello companion")

        records = self.journal.read_transcript()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["step_index"], 0)
        self.assertEqual(records[0]["content"], "Hello companion")

    def test_append_step_event(self):
        step = Step(
            idx=1,
            step_type=StepType.AGENT_TURN,
            status=StepStatus.DONE,
            step_payload={
                "content": "Analyzing request",
                "thinking": "Need to inspect environment",
                "tool_calls": [{"name": "read_file", "args": {"path": "config.json"}}]
            }
        )
        self.journal.append_step_event(step)

        records = self.journal.read_transcript()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["step_index"], 1)
        self.assertEqual(records[0]["type"], "PLANNER_RESPONSE")
        self.assertEqual(records[0]["thinking"], "Need to inspect environment")
        self.assertEqual(len(records[0]["tool_calls"]), 1)

    def test_reconciliation_with_sqlite_steps(self):
        # Step 0 is already in transcript
        self.journal.append_event(0, "USER_EXPLICIT", "USER_INPUT", "DONE", "Step 0 in transcript")

        # SQLite has Steps 0, 1, 2
        steps = [
            Step(idx=0, step_type=StepType.USER_INPUT, status=StepStatus.DONE, step_payload={"content": "Step 0 in SQLite"}),
            Step(idx=1, step_type=StepType.AGENT_TURN, status=StepStatus.DONE, step_payload={"content": "Step 1 in SQLite"}),
            Step(idx=2, step_type=StepType.TOOL_RESULT, status=StepStatus.DONE, step_payload={"content": "Step 2 in SQLite"}),
        ]

        # Reconcile: should backfill Steps 1 and 2 (count = 2)
        backfilled = self.journal.reconcile_with_steps(steps)
        self.assertEqual(backfilled, 2)

        records = self.journal.read_transcript()
        self.assertEqual(len(records), 3)
        self.assertEqual([r["step_index"] for r in records], [0, 1, 2])

        # Second reconciliation must be idempotent (count = 0)
        backfilled_again = self.journal.reconcile_with_steps(steps)
        self.assertEqual(backfilled_again, 0)


if __name__ == "__main__":
    unittest.main()
