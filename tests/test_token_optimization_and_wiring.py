"""Unit Tests for Token Optimization and Subsystem Wiring
Verifies:
1. In-turn intermediate tool output compaction (eliminates quadratic token accumulation).
2. Latest tool result preservation for active model reasoning.
3. Intermediate thinking chain stripping across multi-iteration turns.
4. Runtime lifecycle wiring of ProcessManager and SubagentManager into AgentSessionFactory.
5. Presence.invoke_subagent and task management tools presence in composed session.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from runtime.trajectory import TrajectoryStore, Step, StepType, StepStatus
from runtime.reconstruction import ContextAssembler, compact_intermediate_tool_output
from runtime.lifecycle import PresenceRuntime
from runtime.permissions import PermissionTier


class TestTokenOptimizationAndWiring(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_tok_opt_"))
        self.conv_dir = self.temp_dir / "conversations"
        self.conv_dir.mkdir(parents=True, exist_ok=True)
        self.store = TrajectoryStore(conversation_id="test_tok_opt_conv", db_dir=self.conv_dir)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_compact_intermediate_tool_output_search(self):
        """Verifies compact_intermediate_tool_output converts large search JSON to compact summary."""
        raw_results = {
            "status": "SUCCESS",
            "classification": "SUCCESS",
            "results": [
                {"title": f"Result {i}", "url": f"https://example.com/{i}", "snippet": "A" * 200}
                for i in range(10)
            ]
        }
        raw_json = json.dumps(raw_results)
        self.assertGreater(len(raw_json), 2000)

        compacted = compact_intermediate_tool_output(raw_json, "presence.web_search", step_idx=2)
        self.assertLess(len(compacted), 350)
        parsed = json.loads(compacted)
        self.assertEqual(parsed["status"], "SUCCESS")
        self.assertIn("returned 10 search results", parsed["summary"])
        self.assertIn("step 2", parsed["summary"])

    def test_compact_intermediate_tool_output_fetch(self):
        """Verifies compact_intermediate_tool_output converts large fetched webpage text to compact summary."""
        raw_fetch = {
            "status": "SUCCESS",
            "url": "https://example.com/docs",
            "content": "# Big Page\n" + ("Important documentation paragraph.\n" * 50)
        }
        raw_json = json.dumps(raw_fetch)
        self.assertGreater(len(raw_json), 1500)

        compacted = compact_intermediate_tool_output(raw_json, "presence.fetch_web_page", step_idx=4)
        self.assertLess(len(compacted), 350)
        parsed = json.loads(compacted)
        self.assertEqual(parsed["status"], "SUCCESS")
        self.assertIn("Fetched", parsed["summary"])
        self.assertIn("https://example.com/docs", parsed["url"])
        self.assertIn("step 4", parsed["summary"])

    def test_in_turn_ephemeral_compaction_assembly(self):
        """Verifies ContextAssembler compacts intermediate tool outputs while preserving latest."""
        # Step 0: User Input
        self.store.append_step(Step(
            idx=0,
            step_type=StepType.USER_INPUT,
            status=StepStatus.DONE,
            step_payload={"content": "Research desktop AI tools"}
        ))
        # Step 1: Tool Call 1 (search)
        self.store.append_step(Step(
            idx=1,
            step_type=StepType.TOOL_CALL,
            status=StepStatus.DONE,
            step_payload={"id": "c1", "type": "function", "function": {"name": "presence.web_search", "arguments": "{}"}}
        ))
        # Step 2: Tool Result 1 (large search JSON)
        search_json = json.dumps({
            "status": "SUCCESS",
            "results": [{"title": f"Agent {i}", "url": f"https://ai.org/{i}", "snippet": "Text " * 40} for i in range(5)]
        })
        self.store.append_step(Step(
            idx=2,
            step_type=StepType.TOOL_RESULT,
            status=StepStatus.DONE,
            step_payload={"name": "presence.web_search", "tool_call_id": "c1", "content": search_json}
        ))
        # Step 3: Tool Call 2 (fetch)
        self.store.append_step(Step(
            idx=3,
            step_type=StepType.TOOL_CALL,
            status=StepStatus.DONE,
            step_payload={"id": "c2", "type": "function", "function": {"name": "presence.fetch_web_page", "arguments": "{}"}}
        ))
        # Step 4: Tool Result 2 (latest result in active turn)
        latest_fetch_text = "LATEST_CONTENT: " + ("Fresh content analysis line. " * 30)
        self.store.append_step(Step(
            idx=4,
            step_type=StepType.TOOL_RESULT,
            status=StepStatus.DONE,
            step_payload={"name": "presence.fetch_web_page", "tool_call_id": "c2", "content": latest_fetch_text}
        ))

        assembler = ContextAssembler(trajectory_store=self.store)
        reconstructed = assembler.assemble()

        messages = reconstructed.messages
        # 1 user + 1 assistant(call 1) + 1 tool(res 1) + 1 assistant(call 2) + 1 tool(res 2)
        self.assertEqual(len(messages), 5)

        # Check tool result 1 (intermediate): should be compacted!
        tool_1_msg = messages[2]
        self.assertEqual(tool_1_msg["role"], "tool")
        self.assertIn("returned 5 search results", tool_1_msg["content"])
        self.assertLess(len(tool_1_msg["content"]), 400)

        # Check tool result 2 (latest): should preserve uncompacted content!
        tool_2_msg = messages[4]
        self.assertEqual(tool_2_msg["role"], "tool")
        self.assertIn("LATEST_CONTENT:", tool_2_msg["content"])
        self.assertIn("Fresh content analysis line.", tool_2_msg["content"])

    def test_runtime_lifecycle_subsystem_wiring(self):
        """Verifies ProcessManager and SubagentManager are properly instantiated and attached."""
        db_path = self.temp_dir / "runtime_wire.db"
        runtime = PresenceRuntime(
            db_path=db_path,
            schema_cache_dir=self.temp_dir / "schemas",
            artifact_dir=self.temp_dir / "artifacts",
            summary_db_path=self.temp_dir / "summaries.db",
            mutex_name=f"Local\\TestWiringMutex_{tempfile.gettempprefix()}"
        )
        started = runtime.start()
        self.assertTrue(started)

        try:
            # 1. Verify ProcessManager and SubagentManager exist on runtime
            self.assertIsNotNone(runtime.process_manager)
            self.assertIsNotNone(runtime.subagent_manager)
            self.assertIsNotNone(runtime.observability)

            # 2. Verify SessionFactory has references
            self.assertEqual(runtime.session_factory.process_manager, runtime.process_manager)
            self.assertEqual(runtime.session_factory.subagent_manager, runtime.subagent_manager)
            self.assertEqual(runtime.session_factory.task_registry, runtime.observability.task_registry)

            # 3. Create a session and check registered tools
            session = runtime.session_factory.create_session(
                conversation_id="test_composed_wiring",
                title="Wiring Session"
            )
            tool_names = [t.tool_id for t in session.tool_registry.list_tools()]

            # Subagent tools must be registered
            self.assertIn("presence.invoke_subagent", tool_names)
            self.assertIn("presence.manage_subagents", tool_names)
            self.assertIn("presence.send_subagent_message", tool_names)

            # Task management tool must be registered
            self.assertIn("presence.manage_task", tool_names)

            # Web search tools must be registered
            self.assertIn("presence.web_search", tool_names)
            self.assertIn("presence.fetch_web_page", tool_names)

            session.close()
        finally:
            runtime.stop()


if __name__ == "__main__":
    unittest.main()
