"""Unit and Integration Tests for Settings & API Key Modal in Presence TUI
Verifies configuration persistence, validation, hot-swapping model provider, and TUI dialog interaction.
"""

import os
import time
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from runtime.lifecycle import PresenceRuntime
from runtime.observability.service import RuntimeObservabilityService
from runtime.providers.fake import FakeModelProvider
from runtime.providers.gemini import GeminiModelProvider
from runtime.tui.app import PresenceControlCenterApp
from runtime.tui.settings_modal import ApiKeyModal
from runtime.tui.views.chat_view import ChatView


class TestSettingsModalAndApiKeyConfig(unittest.IsolatedAsyncioTestCase):
    """Tests API key configuration, persistence, and live hot-swapping."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.original_cwd = os.getcwd()
        os.chdir(self.temp_dir.name)

        # Clear any ambient env vars
        self.original_api_key = os.environ.get("GEMINI_API_KEY")
        self.original_model = os.environ.get("GEMINI_MODEL")
        self.original_provider = os.environ.get("PRESENCE_PROVIDER")
        for k in ["GEMINI_API_KEY", "GEMINI_MODEL", "FREELLMAPI_BASE_URL", "FREELLMAPI_API_KEY", "FREELLMAPI_MODEL", "PRESENCE_PROVIDER"]:
            if k in os.environ:
                del os.environ[k]


        self.runtime = PresenceRuntime(
            mutex_name=f"Local\\PresenceTest_Settings_{int(time.time()*1000)}",
            db_path=Path(self.temp_dir.name) / "runtime.db",
            summary_db_path=Path(self.temp_dir.name) / "summaries.db",
            artifact_dir=Path(self.temp_dir.name) / "artifacts",
            schema_cache_dir=Path(self.temp_dir.name) / "schemas",
        )
        self.runtime.start()
        self.session = self.runtime.create_session(
            title="Settings Test Session",
            model_provider=FakeModelProvider()
        )
        self.service = self.runtime.observability

    def tearDown(self):
        if self.session and not self.session.is_closed:
            self.session.close()
        self.runtime.stop()
        os.chdir(self.original_cwd)
        self.temp_dir.cleanup()

        # Restore env vars
        if self.original_api_key:
            os.environ["GEMINI_API_KEY"] = self.original_api_key
        elif "GEMINI_API_KEY" in os.environ:
            del os.environ["GEMINI_API_KEY"]

        if self.original_model:
            os.environ["GEMINI_MODEL"] = self.original_model
        elif "GEMINI_MODEL" in os.environ:
            del os.environ["GEMINI_MODEL"]

        if self.original_provider:
            os.environ["PRESENCE_PROVIDER"] = self.original_provider
        elif "PRESENCE_PROVIDER" in os.environ:
            del os.environ["PRESENCE_PROVIDER"]



    def test_01_configure_gemini_api_key_empty(self):
        """Verifies empty API keys are rejected."""
        success, msg = self.service.configure_gemini_api_key("")
        self.assertFalse(success)
        self.assertIn("cannot be empty", msg)

    def test_02_configure_gemini_api_key_persists_to_dotenv(self):
        """Verifies saving an API key writes to .env and sets environment variables."""
        test_key = "AIzaSyTestKey1234567890abcdef"
        test_model = "gemini-2.5-flash"

        success, msg = self.service.configure_gemini_api_key(
            api_key=test_key,
            model_id=test_model,
            test_live=False,
            persist=True
        )
        self.assertTrue(success)
        self.assertEqual(os.environ.get("GEMINI_API_KEY"), test_key)
        self.assertEqual(os.environ.get("GEMINI_MODEL"), test_model)

        env_file = Path(self.temp_dir.name) / ".env"
        self.assertTrue(env_file.exists())
        content = env_file.read_text(encoding="utf-8")
        self.assertIn(f"GEMINI_API_KEY={test_key}", content)
        self.assertIn(f"GEMINI_MODEL={test_model}", content)

    async def test_03_tui_settings_modal_open_and_cancel(self):
        """Verifies opening settings modal with F9 and dismissing via Cancel."""
        app = PresenceControlCenterApp(runtime=self.runtime, active_session=self.session)
        async with app.run_test(size=(120, 36)) as pilot:
            # Press F9 to open settings
            await pilot.press("f9")
            await pilot.pause()

            # Ensure ApiKeyModal is the active screen
            self.assertIsInstance(app.screen, ApiKeyModal)

            # Click Cancel
            await pilot.click("#btn-cancel-key")
            await pilot.pause()

            # Screen dismissed back to default
            self.assertNotIsInstance(app.screen, ApiKeyModal)

    async def test_04_tui_settings_save_and_hotswap(self):
        """Verifies entering key in modal updates session provider to Gemini."""
        app = PresenceControlCenterApp(runtime=self.runtime, active_session=self.session)
        async with app.run_test(size=(120, 36)) as pilot:
            # Initially fake provider
            self.assertEqual(self.session.model_provider.provider_name, "fake")

            # Open settings
            await pilot.press("f9")
            await pilot.pause()

            from textual.widgets import TabbedContent
            tabs = app.screen.query_one("#provider-tabs", TabbedContent)
            tabs.active = "tab-gemini"
            await pilot.pause()

            # Type key

            key_input = app.screen.query_one("#input-api-key")
            key_input.value = "AIzaSyLiveHotSwapTestKey"

            # Click Save & Connect
            await pilot.click("#btn-save-key")
            await pilot.pause()

            # Verify session provider was hot-swapped
            self.assertEqual(self.session.model_provider.provider_name, "gemini")
            self.assertEqual(self.session.agent_executor.provider.provider_name, "gemini")

            # Verify chat view header updated
            chat_view = app.query_one("#view-chat", ChatView)
            title_text = str(chat_view.query_one("#chat-session-title").render())
            self.assertIn("gemini-2.5-flash", title_text)

    async def test_05_tui_settings_freellmapi_save_and_hotswap(self):
        """Verifies entering FreeLLMAPI configuration in modal updates session provider to FreeLLMAPI."""
        from textual.widgets import TabbedContent
        app = PresenceControlCenterApp(runtime=self.runtime, active_session=self.session)
        async with app.run_test(size=(120, 36)) as pilot:
            # Initially fake provider
            self.assertEqual(self.session.model_provider.provider_name, "fake")

            # Open settings modal
            await pilot.press("f9")
            await pilot.pause()

            # Switch to FreeLLMAPI tab
            tabs = app.screen.query_one("#provider-tabs", TabbedContent)
            tabs.active = "tab-freellmapi"
            await pilot.pause()

            # Fill in FreeLLMAPI details
            url_input = app.screen.query_one("#input-freellm-url")
            url_input.value = "http://127.0.0.1:3001/v1"

            key_input = app.screen.query_one("#input-freellm-key")
            key_input.value = "free-test-key-5678"

            model_input = app.screen.query_one("#input-freellm-model")
            model_input.value = "qwen-3.8-27b"

            # Click Save & Connect
            await pilot.click("#btn-save-key")
            await pilot.pause()

            # Verify session provider was hot-swapped to freellmapi
            self.assertEqual(self.session.model_provider.provider_name, "freellmapi")
            self.assertEqual(self.session.agent_executor.provider.provider_name, "freellmapi")
            self.assertEqual(self.session.model_provider.model_id, "qwen-3.8-27b")

            # Verify chat view header updated
            chat_view = app.query_one("#view-chat", ChatView)
            title_text = str(chat_view.query_one("#chat-session-title").render())
            self.assertIn("qwen-3.8-27b", title_text)
            self.assertIn("freellmapi", title_text)



if __name__ == "__main__":
    unittest.main()
