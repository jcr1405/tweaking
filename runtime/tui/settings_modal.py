"""Settings & Inference Provider Configuration Modal Dialog
Enables entering, testing, and saving Google Gemini and FreeLLMAPI credentials directly within the TUI.
"""

import os
from typing import Any
from rich.text import Text
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Static, Input, Button, Label, TabbedContent, TabPane
from textual.containers import Vertical, Horizontal

from runtime.providers.freellmapi import detect_freellmapi_credentials


class ApiKeyModal(ModalScreen[bool]):
    """Modal dialog for configuring Google Gemini and FreeLLMAPI model providers."""

    def __init__(self, service: Any, current_model: str = "gemini-3.5-flash-lite", **kwargs):
        super().__init__(**kwargs)
        self.service = service
        self.current_model = current_model

        # Auto-detect ambient or stored FreeLLMAPI credentials
        det_url, det_key = detect_freellmapi_credentials()
        self.default_freellm_url = det_url or "http://127.0.0.1:3001/v1"
        self.default_freellm_key = det_key or ""
        self.default_freellm_model = os.environ.get("FREELLMAPI_MODEL", "auto")
        self.current_provider = os.environ.get("PRESENCE_PROVIDER", "gemini").lower()

    def compose(self) -> ComposeResult:
        init_tab = "tab-freellmapi" if self.current_provider == "freellmapi" else "tab-gemini"

        with Vertical(id="api-key-dialog"):
            yield Static("MODEL PROVIDER CONFIGURATION", id="api-key-dialog-title")
            yield Static(
                "Configure inference providers without restarting Presence.\n"
                "Switch seamlessly between Google Gemini and FreeLLMAPI (local inference gateway).\n"
                "Credentials are saved to .env and applied to your active session immediately.",
                classes="dialog-desc"
            )

            with TabbedContent(initial=init_tab, id="provider-tabs"):
                with TabPane("Google Gemini", id="tab-gemini"):
                    yield Label("Gemini API Key:", classes="dialog-label")
                    yield Input(
                        value=os.environ.get("GEMINI_API_KEY", ""),
                        placeholder="Paste AIzaSy... key here",
                        password=True,
                        id="input-api-key"
                    )

                    yield Label("Model ID (Recommended: gemini-3.5-flash-lite):", classes="dialog-label")
                    yield Input(
                        value=self.current_model if not self.current_model.startswith("auto") else "gemini-3.5-flash-lite",
                        placeholder="gemini-3.5-flash-lite",
                        id="input-model-id"
                    )

                with TabPane("FreeLLMAPI (Gateway)", id="tab-freellmapi"):
                    yield Label("Gateway Base URL:", classes="dialog-label")
                    yield Input(
                        value=self.default_freellm_url,
                        placeholder="http://127.0.0.1:3001/v1",
                        id="input-freellm-url"
                    )

                    yield Label("FreeLLMAPI API Key (Auto-detected if blank):", classes="dialog-label")
                    yield Input(
                        value=self.default_freellm_key,
                        placeholder="Paste FreeLLMAPI unified key here",
                        password=True,
                        id="input-freellm-key"
                    )

                    yield Label("Model ID (default: auto, or e.g. qwen-3.8-27b):", classes="dialog-label")
                    yield Input(
                        value=self.default_freellm_model,
                        placeholder="auto",
                        id="input-freellm-model"
                    )

            yield Static(id="api-key-status")

            with Horizontal(id="dialog-buttons"):
                yield Button("Test Connection", id="btn-test-key", variant="default")
                yield Button("Save & Connect", id="btn-save-key", variant="primary")
                yield Button("Cancel", id="btn-cancel-key", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        status = self.query_one("#api-key-status", Static)

        if event.button.id == "btn-cancel-key":
            self.dismiss(False)
            return

        # Determine which provider tab is currently active
        tabs = self.query_one("#provider-tabs", TabbedContent)
        active_tab = getattr(tabs, "active", "tab-gemini")

        if active_tab == "tab-gemini":
            self._handle_gemini_action(event.button.id, status)
        else:
            self._handle_freellmapi_action(event.button.id, status)

    def _handle_gemini_action(self, button_id: str, status: Static) -> None:
        key_input = self.query_one("#input-api-key", Input).value.strip()
        model_input = self.query_one("#input-model-id", Input).value.strip() or "gemini-3.5-flash-lite"

        if not key_input:
            status.update(Text("⚠️ Please enter a valid Gemini API key.", style="bold red"))
            return

        if button_id == "btn-test-key":
            status.update(Text("Testing connection with Google Gemini API...", style="dim yellow"))

            def _test_gemini_worker():
                success, msg = self.service.configure_gemini_api_key(
                    key_input, model_input, test_live=True, persist=False
                )

                def _update():
                    if success:
                        status.update(Text(f"✅ Handshake successful! Model '{model_input}' is accessible.", style="bold green"))
                    else:
                        status.update(Text(f"❌ {msg}", style="bold red"))

                if hasattr(self.app, "safe_call"):
                    self.app.safe_call(_update)
                else:
                    _update()

            self.run_worker(_test_gemini_worker, thread=True)

        elif button_id == "btn-save-key":
            status.update(Text("Saving credentials and connecting to Gemini...", style="dim yellow"))

            def _save_gemini_worker():
                success, msg = self.service.configure_gemini_api_key(
                    key_input, model_input, test_live=False, persist=True
                )

                def _finish():
                    if success:
                        if hasattr(self.app, "apply_gemini_key_to_session"):
                            self.app.apply_gemini_key_to_session(key_input, model_input)
                        self.dismiss(True)
                    else:
                        status.update(Text(f"❌ {msg}", style="bold red"))

                if hasattr(self.app, "safe_call"):
                    self.app.safe_call(_finish)
                else:
                    _finish()

            self.run_worker(_save_gemini_worker, thread=True)

    def _handle_freellmapi_action(self, button_id: str, status: Static) -> None:
        url_input = self.query_one("#input-freellm-url", Input).value.strip() or "http://127.0.0.1:3001/v1"
        key_input = self.query_one("#input-freellm-key", Input).value.strip()
        model_input = self.query_one("#input-freellm-model", Input).value.strip() or "auto"

        if not key_input:
            status.update(Text("⚠️ Please enter a FreeLLMAPI key or configure one.", style="bold red"))
            return

        if button_id == "btn-test-key":
            status.update(Text(f"Testing connection with FreeLLMAPI at {url_input}...", style="dim yellow"))

            def _test_freellm_worker():
                healthy, msg, meta = self.service.test_freellmapi_health(url_input, key_input)

                def _update():
                    if healthy:
                        count = meta.get("model_count", 0)
                        status.update(Text(f"✅ FreeLLMAPI is online! ({count} models available)", style="bold green"))
                    else:
                        status.update(Text(f"❌ {msg}", style="bold red"))

                if hasattr(self.app, "safe_call"):
                    self.app.safe_call(_update)
                else:
                    _update()

            self.run_worker(_test_freellm_worker, thread=True)

        elif button_id == "btn-save-key":
            status.update(Text("Saving FreeLLMAPI settings and connecting...", style="dim yellow"))

            def _save_freellm_worker():
                success, msg = self.service.configure_freellmapi_provider(
                    url_input, key_input, model_input, test_live=False, persist=True
                )

                def _finish():
                    if success:
                        if hasattr(self.app, "apply_freellmapi_provider_to_session"):
                            self.app.apply_freellmapi_provider_to_session(url_input, key_input, model_input)
                        self.dismiss(True)
                    else:
                        status.update(Text(f"❌ {msg}", style="bold red"))

                if hasattr(self.app, "safe_call"):
                    self.app.safe_call(_finish)
                else:
                    _finish()

            self.run_worker(_save_freellm_worker, thread=True)
