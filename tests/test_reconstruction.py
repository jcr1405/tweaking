"""Automated Unit Tests for Deterministic Context Reconstruction"""

import unittest
import tempfile
from pathlib import Path

from runtime.conversation import ConversationMetadata
from runtime.trajectory import TrajectoryStore, Step, StepType, StepStatus
from runtime.summaries import GlobalSummaryManager
from runtime.reconstruction import ContextAssembler, estimate_token_count


class TestReconstruction(unittest.TestCase):
    """Validates local deterministic context assembly and sliding-window compaction."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.conv_id = "test-recon-001"
        self.store = TrajectoryStore(conversation_id=self.conv_id, db_dir=self.base_dir / "conversations")
        self.summary_manager = GlobalSummaryManager(db_path=self.base_dir / "summaries.db")

        # Set up conversation metadata
        meta = ConversationMetadata(
            conversation_id=self.conv_id,
            title="Reconstruction Test Conversation",
            workspace_path=r"C:\test\workspace"
        )
        self.store.save_metadata(meta)
        self.summary_manager.upsert_summary(
            conversation_id=self.conv_id,
            title="Reconstruction Test Conversation",
            raw_summary="Earlier in this conversation, user and agent discussed SQLite WAL mode and FTS5."
        )

    def tearDown(self):
        self.store.close()
        self.summary_manager.close()
        self.temp_dir.cleanup()

    def test_full_context_reconstruction_within_budget(self):
        # Insert 3 short turns
        self.store.append_step(Step(
            idx=0,
            step_type=StepType.USER_INPUT,
            status=StepStatus.DONE,
            step_payload={"content": "What is the status of the database?"}
        ))
        self.store.append_step(Step(
            idx=1,
            step_type=StepType.AGENT_TURN,
            status=StepStatus.DONE,
            step_payload={"content": "Database is running in WAL mode.", "thinking": "Checked pragma"}
        ))
        self.store.append_step(Step(
            idx=2,
            step_type=StepType.USER_INPUT,
            status=StepStatus.DONE,
            step_payload={"content": "Great, continue."}
        ))

        assembler = ContextAssembler(
            trajectory_store=self.store,
            summary_manager=self.summary_manager,
            max_context_tokens=4096
        )

        ctx = assembler.assemble(
            system_instructions="You are an autonomous desktop companion.",
            desktop_ambient_context={"active_window": "PowerShell", "battery": 95}
        )

        self.assertFalse(ctx.is_compacted)
        self.assertEqual(ctx.total_steps, 3)
        self.assertEqual(ctx.replayed_steps, 3)
        self.assertEqual(len(ctx.messages), 3)
        self.assertEqual(ctx.messages[0]["role"], "user")
        self.assertEqual(ctx.messages[0]["content"], "What is the status of the database?")
        self.assertEqual(ctx.messages[1]["role"], "assistant")
        self.assertEqual(ctx.messages[2]["role"], "user")
        self.assertIn("PowerShell", ctx.system_prompt)

    def test_sliding_window_compaction_when_budget_exceeded(self):
        # Insert 10 turns with substantial text to exceed a small token budget
        for i in range(10):
            self.store.append_step(Step(
                idx=i,
                step_type=StepType.USER_INPUT if i % 2 == 0 else StepType.AGENT_TURN,
                status=StepStatus.DONE,
                step_payload={"content": f"Turn index {i}: " + ("Detailed reasoning data " * 20)}
            ))

        # Constrain budget to 250 tokens
        assembler = ContextAssembler(
            trajectory_store=self.store,
            summary_manager=self.summary_manager,
            max_context_tokens=250
        )

        ctx = assembler.assemble(system_instructions="Base companion prompt.")

        # Must trigger compaction
        self.assertTrue(ctx.is_compacted)
        self.assertEqual(ctx.total_steps, 10)
        self.assertLess(ctx.replayed_steps, 10)

        # First message must be the injected historical summary
        self.assertEqual(ctx.messages[0]["role"], "system")
        self.assertIn("[Historical Conversation Summary]", ctx.messages[0]["content"])
        self.assertIn("SQLite WAL mode and FTS5", ctx.messages[0]["content"])

        # The subsequent messages must be the newest turns (e.g. turn 9)
        last_msg = ctx.messages[-1]
        self.assertIn("Turn index 9", last_msg["content"])


if __name__ == "__main__":
    unittest.main()
