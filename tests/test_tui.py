"""Headless Unit and Integration Tests for Presence Terminal Control Center
Verifies layout composition, view switching, keyboard shortcuts, chat interaction,
and clean shutdown using Textual's headless test pilot.
"""

import unittest
import time
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from textual.widgets import Input
from runtime.lifecycle import PresenceRuntime
from runtime.providers.base import ModelStreamChunk
from runtime.providers.fake import FakeModelProvider
from runtime.tui.app import PresenceControlCenterApp
from runtime.tui.views import (
    ChatView,
    ExplorerView,
    MCPView,
    TasksView,
    TraceView,
    ContextView,
    HealthView,
    ActivityPanel
)


class TestPresenceTUI(unittest.IsolatedAsyncioTestCase):
    """Headless integration tests for the full terminal control center."""

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "runtime.db"
        self.summary_db = Path(self.temp_dir.name) / "summaries.db"
        self.artifact_dir = Path(self.temp_dir.name) / "artifacts"
        self.schema_dir = Path(self.temp_dir.name) / "schemas"

        self.fake_provider = FakeModelProvider(scripted_responses=[
            [ModelStreamChunk(text_delta="Hello from TUI!")]
        ])

        self.runtime = PresenceRuntime(
            mutex_name=f"Local\\PresenceTest_TUI_{int(time.time()*1000)}",
            db_path=self.db_path,
            summary_db_path=self.summary_db,
            artifact_dir=self.artifact_dir,
            schema_cache_dir=self.schema_dir,
        )
        self.runtime.start()

        # Session with fake provider
        self.session = self.runtime.create_session(
            title="TUI Headless Test Session",
            model_provider=self.fake_provider
        )
        self.app = PresenceControlCenterApp(runtime=self.runtime, active_session=self.session)

    async def asyncTearDown(self):
        self.runtime.stop()
        self.temp_dir.cleanup()

    async def test_01_app_mount_and_view_switching(self):
        """Verifies layout composition and switching across all 7 views."""
        async with self.app.run_test() as pilot:
            # 1. Verify initial state is Chat view
            switcher = self.app.query_one("#workspace-switcher")
            self.assertEqual(switcher.current, "view-chat")

            # 2. Switch to View 02: Explorer (F2)
            await pilot.press("f2")
            self.assertEqual(switcher.current, "view-explorer")
            explorer = self.app.query_one("#view-explorer", ExplorerView)
            self.assertIsNotNone(explorer)

            # 3. Switch to View 03: MCP (F3)
            await pilot.press("f3")
            self.assertEqual(switcher.current, "view-mcp")
            mcp_view = self.app.query_one("#view-mcp", MCPView)
            self.assertIsNotNone(mcp_view)

            # 4. Switch to View 04: Tasks (F4)
            await pilot.press("f4")
            self.assertEqual(switcher.current, "view-tasks")
            tasks_view = self.app.query_one("#view-tasks", TasksView)
            self.assertIsNotNone(tasks_view)

            # 5. Switch to View 05: Trace (F5)
            await pilot.press("f5")
            self.assertEqual(switcher.current, "view-trace")
            trace_view = self.app.query_one("#view-trace", TraceView)
            self.assertIsNotNone(trace_view)

            # 6. Switch to View 06: Context (F6)
            await pilot.press("f6")
            self.assertEqual(switcher.current, "view-context")
            context_view = self.app.query_one("#view-context", ContextView)
            self.assertIsNotNone(context_view)

            # 7. Switch to View 07: Health (F7)
            await pilot.press("f7")
            self.assertEqual(switcher.current, "view-health")
            health_view = self.app.query_one("#view-health", HealthView)
            self.assertIsNotNone(health_view)

            # 8. Switch back to Chat (F1)
            await pilot.press("f1")
            self.assertEqual(switcher.current, "view-chat")

    async def test_02_toggle_activity_panel(self):
        """Verifies F8 toggles visibility of the persistent right sidebar."""
        async with self.app.run_test() as pilot:
            sidebar = self.app.query_one("#activity-sidebar")
            self.assertTrue(sidebar.display)

            await pilot.press("f8")
            self.assertFalse(sidebar.display)

            await pilot.press("f8")
            self.assertTrue(sidebar.display)

    async def test_03_telemetry_tick_updates_activity_panel(self):
        """Verifies periodic background telemetry refreshes without error."""
        async with self.app.run_test() as pilot:
            panel = self.app.query_one("#activity-panel-widget", ActivityPanel)
            self.assertIsNotNone(panel)
            self.app._on_telemetry_tick()
            await pilot.pause()

    async def test_04_chat_input_submission(self):
        """Verifies typing into the chat input bar triggers turn execution."""
        async with self.app.run_test() as pilot:
            chat_view = self.app.query_one("#view-chat", ChatView)
            input_box = self.app.query_one("#chat-input", Input)

            # Trigger submission
            chat_view.on_input_submitted(Input.Submitted(input_box, "Hello Presence!"))
            await pilot.pause()

            # Check that user bubble and agent bubble mounted
            messages_scroll = self.app.query_one("#chat-messages-scroll")
            self.assertGreater(len(messages_scroll.children), 1)

    async def test_05_sidebar_button_navigation(self):
        """Verifies clicking left navigation buttons switches workspace views."""
        async with self.app.run_test() as pilot:
            switcher = self.app.query_one("#workspace-switcher")

            # Click Explorer
            await pilot.click("#nav-btn-explorer")
            self.assertEqual(switcher.current, "view-explorer")

            # Click MCP
            await pilot.click("#nav-btn-mcp")
            self.assertEqual(switcher.current, "view-mcp")

            # Click Tasks
            await pilot.click("#nav-btn-tasks")
            self.assertEqual(switcher.current, "view-tasks")

            # Click Trace
            await pilot.click("#nav-btn-trace")
            self.assertEqual(switcher.current, "view-trace")

            # Click Context
            await pilot.click("#nav-btn-context")
            self.assertEqual(switcher.current, "view-context")

            # Click Health
            await pilot.click("#nav-btn-health")
            self.assertEqual(switcher.current, "view-health")

            # Click Chat
            await pilot.click("#nav-btn-chat")
            self.assertEqual(switcher.current, "view-chat")

    async def test_06_explorer_conversation_selection(self):
        """Verifies selecting a conversation in Explorer updates detail panes."""
        async with self.app.run_test() as pilot:
            await pilot.press("f2")
            explorer = self.app.query_one("#view-explorer", ExplorerView)
            self.assertIsNotNone(explorer)

            cid = self.session.conversation_id
            explorer._select_conversation(cid)

            meta_widget = self.app.query_one("#explorer-conv-meta")
            self.assertIn(cid[:8], str(meta_widget.render()))

    async def test_07_mcp_view_actions(self):
        """Verifies MCP view refresh button and table contents."""
        async with self.app.run_test() as pilot:
            await pilot.press("f3")
            mcp_view = self.app.query_one("#view-mcp", MCPView)
            self.assertIsNotNone(mcp_view)

            # Click refresh button
            await pilot.click("#mcp-refresh-btn")
            table = self.app.query_one("#mcp-table")
            self.assertEqual(len(table.columns), 8)

    async def test_08_tasks_and_trace_navigation(self):
        """Verifies Task monitor and Execution Trace view controls."""
        async with self.app.run_test(size=(120, 36)) as pilot:
            # Tasks View
            await pilot.press("f4")
            await pilot.click("#tasks-refresh-btn")
            tree = self.app.query_one("#tasks-tree")
            self.assertIsNotNone(tree.root)

            # Trace View
            await pilot.press("f5")
            trace_view = self.app.query_one("#view-trace", TraceView)
            self.assertIsNotNone(trace_view)
            await pilot.click("#trace-next-btn")
            await pilot.pause()
            self.assertEqual(trace_view._current_turn_idx, 2)
            await pilot.click("#trace-prev-btn")
            await pilot.pause()
            self.assertEqual(trace_view._current_turn_idx, 1)

    async def test_09_health_view_buttons(self):
        """Verifies System Health view refresh and circuit breaker reset."""
        async with self.app.run_test() as pilot:
            await pilot.press("f7")
            health_view = self.app.query_one("#view-health", HealthView)
            self.assertIsNotNone(health_view)

            await pilot.click("#health-refresh-btn")
            await pilot.click("#health-reset-circuit-btn")
            content = self.app.query_one("#health-content-text")
            self.assertIn("HOST RUNTIME", str(content.render()))

    async def test_10_chronological_tool_card_mounting(self):
        """Verifies tool cards are mounted chronologically before the assistant response text."""
        self.fake_provider._scripted_responses = [
            [ModelStreamChunk(tool_calls=[{"name": "presence.get_runtime_status", "args": {}}])],
            [ModelStreamChunk(text_delta="Status check complete.")]
        ]
        async with self.app.run_test() as pilot:
            chat_view = self.app.query_one("#view-chat", ChatView)
            input_box = self.app.query_one("#chat-input", Input)
            from textual.widgets import Button
            send_btn = self.app.query_one("#chat-send-btn", Button)
            self.assertEqual(str(send_btn.label), "Send")

            input_box.value = "Check runtime status"
            await pilot.click("#chat-send-btn")

            # Wait for turn worker execution
            for _ in range(30):
                await pilot.pause(0.1)
                if not chat_view._is_generating:
                    break

            self.assertEqual(str(send_btn.label), "Send")

            messages_scroll = self.app.query_one("#chat-messages-scroll")
            children = list(messages_scroll.children)

            user_indices = [i for i, c in enumerate(children) if "chat-msg-user" in c.classes]
            tool_indices = [i for i, c in enumerate(children) if "tool-invocation-card" in c.classes]
            agent_indices = [i for i, c in enumerate(children) if "chat-msg-agent" in c.classes]

            self.assertTrue(len(user_indices) > 0, "No user message bubble mounted")
            self.assertTrue(len(tool_indices) > 0, "No tool card mounted")
            self.assertTrue(len(agent_indices) > 0, "No agent response bubble mounted")

            # User bubble precedes tool cards, and tool cards precede final agent response bubble
            self.assertLess(user_indices[-1], tool_indices[0])
            self.assertLess(tool_indices[-1], agent_indices[-1])

    async def test_11_stop_button_cancellation(self):
        """Verifies clicking Stop during active generation cancels the turn and resets button to Send."""
        from textual.widgets import Button

        class SlowFakeProvider(FakeModelProvider):
            def generate_stream(self, *args, **kwargs):
                token = kwargs.get("cancellation_token")
                for i in range(30):
                    if token and getattr(token, "is_cancelled", False):
                        break
                    time.sleep(0.05)
                    yield ModelStreamChunk(text_delta=f"chunk {i} ")

        slow_p = SlowFakeProvider()
        self.session.model_provider = slow_p
        self.session.agent_executor.provider = slow_p

        async with self.app.run_test() as pilot:
            chat_view = self.app.query_one("#view-chat", ChatView)
            input_box = self.app.query_one("#chat-input", Input)
            send_btn = self.app.query_one("#chat-send-btn", Button)
            self.assertEqual(str(send_btn.label), "Send")

            input_box.value = "Test slow message"
            await pilot.click("#chat-send-btn")
            await pilot.pause(0.1)

            # While generating, button should be Stop
            self.assertTrue(chat_view._is_generating)
            self.assertIn(str(send_btn.label), ("Stop", "Stopping..."))

            # Click Stop to cancel
            await pilot.click("#chat-send-btn")

            # Wait for cancellation cleanup
            for _ in range(30):
                await pilot.pause(0.05)
                if not chat_view._is_generating:
                    break

            self.assertFalse(chat_view._is_generating)
            self.assertEqual(str(send_btn.label), "Send")


if __name__ == "__main__":
    unittest.main()
