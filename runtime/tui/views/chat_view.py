"""Chat View Widget (View 01)
Primary conversational workspace for communicating with Presence.
Supports multi-turn interaction, streaming text, tool execution cards, and session controls.
"""

import time
from typing import Optional, Any
from rich.text import Text
from rich.panel import Panel
from textual.app import ComposeResult
from textual.widgets import Static, Input, Button
from textual.containers import Vertical, Horizontal, VerticalScroll

from runtime.agent_executor import CancellationToken, AgentExecutionEvent
from runtime.observability.redaction import sanitize_and_redact


class ChatView(Static):
    """View 01: Interactive Chat with Presence."""

    def __init__(self, service: Any, session: Any, **kwargs):
        super().__init__(**kwargs)
        self.service = service
        self.session = session
        self._current_cancel_token: Optional[CancellationToken] = None
        self._is_generating: bool = False
        self._thinking_bubble: Optional[Static] = None
        self._active_agent_bubble: Optional[Static] = None
        self._accumulated_text: str = ""

    def compose(self) -> ComposeResult:
        provider = getattr(self.session, "model_provider", None)
        provider_name = getattr(provider, "provider_name", "unknown") if provider else "unknown"
        model_id = getattr(provider, "model_id", "unknown") if provider else "unknown"
        is_mock = provider_name == "fake"
        is_freellm = provider_name == "freellmapi"
        from runtime.config import is_local_only_mode
        is_local = is_local_only_mode() or provider_name == "ollama"

        if is_local:
            status_tag = f"Model: {model_id} [LOCAL-ONLY: OLLAMA]"
            welcome_text = (
                f"🔒 [bold green]PRESENCE — LOCAL-FIRST PERSONAL COMPUTER COMPANION[/bold green]\n"
                f"[bold cyan]Local Inference Model:[/bold cyan] {model_id} via Ollama (100% offline local inference)\n"
                f"[dim]Strict Local-Only Mode: All cloud AI providers are disabled. Zero external telemetry.\n"
                f"Personal desktop assistance, screen awareness, and behavioral memory active.\n"
                f"Type a message below to interact with your local companion.[/dim]\n"
            )
        elif is_mock:
            status_tag = f"Model: {model_id} [MOCK MODE - NO API KEY]"
            welcome_text = (
                "⚠️ [bold yellow]NOTICE: Running in Offline Mock Mode (FakeModelProvider).[/bold yellow]\n"
                "[dim]No live LLM provider is currently configured.\n"
                "All responses are simulated test responses ('Hello! I am your Persistent Intelligent Presence.').\n\n"
                "To enable live AI inference:\n"
                "  1. Press F9 or click 'Configure Model Provider' below\n"
                "  2. Choose Google Gemini or FreeLLMAPI (local inference gateway)[/dim]\n"
            )
        elif is_freellm:
            base_url = getattr(provider, "base_url", "http://127.0.0.1:3001/v1")
            status_tag = f"Model: {model_id} (freellmapi)"
            welcome_text = f"Connected to FreeLLMAPI gateway: [bold cyan]{model_id}[/bold cyan] ({base_url}). Type a message below to start chatting.\n"
        else:
            status_tag = f"Model: {model_id} ({provider_name})"
            welcome_text = f"Connected to live Gemini model: [bold green]{model_id}[/bold green]. Type a message below to start chatting.\n"

        with Vertical(id="chat-view-container"):
            with Horizontal(id="chat-header-bar"):
                yield Static(f"Chat Session: {self.session.conversation_id[:8]}... | {status_tag}", id="chat-session-title")
            with VerticalScroll(id="chat-messages-scroll"):
                yield Static(welcome_text, id="chat-welcome-msg")
                if is_mock:
                    yield Button("🔑 Configure Model Provider", id="chat-configure-key-btn", variant="primary")
            with Horizontal(id="chat-input-bar"):
                yield Input(placeholder="Ask Presence anything... (Press Enter or click Send)", id="chat-input")
                yield Button("Send", id="chat-send-btn", variant="primary")


    def on_mount(self) -> None:
        """Loads historical messages if existing session has steps."""
        self._load_existing_history()
        self.query_one("#chat-input", Input).focus()

    def _load_existing_history(self) -> None:
        """Renders prior conversation turns if available."""
        try:
            turns = self.service.get_conversation_turns(self.session.conversation_id)
            scroll = self.query_one("#chat-messages-scroll", VerticalScroll)
            for t in turns:
                # User bubble
                u_text = Text(f"You: {t.user_input}", style="bold cyan")
                scroll.mount(Static(u_text, classes="chat-msg-user"))

                # Tool calls
                for tc in t.tool_calls:
                    card_text = Text()
                    card_text.append(f"⚙ TOOL: {tc.get('name')}\n", style="bold yellow")
                    card_text.append(f"Status: {tc.get('status')}\n")
                    if tc.get("artifact_id"):
                        card_text.append(f"Artifact: {tc.get('artifact_id')}\n", style="cyan")
                    scroll.mount(Static(card_text, classes="tool-invocation-card"))

                # Agent response
                if t.agent_output:
                    clean_output = t.agent_output
                    if "</think>" in clean_output:
                        clean_output = clean_output.split("</think>", 1)[1].strip()
                    a_text = Text(f"Presence:\n{clean_output}", style="white")
                    scroll.mount(Static(a_text, classes="chat-msg-agent"))
            scroll.scroll_end(animate=False)
        except Exception:
            pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handles sending a new user message via Enter key."""
        user_text = event.value.strip()
        if user_text:
            self.action_submit_chat(user_text)

    def action_submit_chat(self, user_text: str) -> None:
        """Submits user text to the active agent session."""
        if not user_text or self._is_generating:
            return

        input_box = self.query_one("#chat-input", Input)
        input_box.value = ""

        # Update button to Stop state
        try:
            btn = self.query_one("#chat-send-btn", Button)
            btn.label = "Stop"
            btn.variant = "error"
            btn.disabled = False
        except Exception:
            pass

        # Mount User Message Bubble
        scroll = self.query_one("#chat-messages-scroll", VerticalScroll)
        user_widget = Static(Text(f"You: {user_text}", style="bold cyan"), classes="chat-msg-user")
        scroll.mount(user_widget)

        # Mount ephemeral thinking indicator (will be replaced by tools or agent response)
        self._accumulated_text = ""
        self._active_agent_bubble = None
        self._thinking_bubble = Static(Text("Presence: thinking...", style="dim italic"), classes="chat-msg-agent")
        scroll.mount(self._thinking_bubble)
        scroll.scroll_end(animate=True)

        self._is_generating = True
        self._current_cancel_token = CancellationToken()

        # Run turn in background worker thread to never block Textual event loop
        self.app.run_worker(lambda: self._run_turn_worker(user_text, self._current_cancel_token), thread=True)

    def _run_turn_worker(self, user_text: str, token: CancellationToken) -> None:
        """Worker executing AgentSession.execute_turn in background thread."""
        scroll = self.query_one("#chat-messages-scroll", VerticalScroll)

        def _remove_thinking_indicator():
            if self._thinking_bubble:
                try:
                    self._thinking_bubble.remove()
                except Exception:
                    pass
                self._thinking_bubble = None

        def handle_event(event: AgentExecutionEvent):
            if event.event_type == "THINKING_DELTA":
                if self._thinking_bubble:
                    self._thinking_bubble.update(Text("Presence: [Thinking...]", style="dim italic"))
                return

            if event.event_type == "TEXT_DELTA":
                _remove_thinking_indicator()
                delta = event.payload.get("delta", "")
                self._accumulated_text += delta
                if self._active_agent_bubble is None:
                    self._active_agent_bubble = Static("", classes="chat-msg-agent")
                    scroll.mount(self._active_agent_bubble)

                disp_text = self._accumulated_text
                if "</think>" in disp_text:
                    disp_text = disp_text.split("</think>", 1)[1].lstrip()
                    if not disp_text:
                        self._active_agent_bubble.update(Text("Presence: [Thinking...]", style="dim italic"))
                        return
                elif "<think>" in disp_text:
                    self._active_agent_bubble.update(Text("Presence: [Thinking...]", style="dim italic"))
                    return

                if disp_text:
                    disp = sanitize_and_redact(disp_text)
                    self._active_agent_bubble.update(Text(f"Presence:\n{disp}", style="white"))
                    scroll.scroll_end(animate=False)

            elif event.event_type == "TOOL_REQUESTED":
                _remove_thinking_indicator()
                # Detach any previous text bubble so subsequent text appears after tool cards
                self._active_agent_bubble = None
                self._accumulated_text = ""

                name = event.payload.get("name", "tool")
                card = Static(Text(f"⚙ Tool Requested: {name}...", style="yellow italic"), classes="tool-invocation-card")
                scroll.mount(card)
                scroll.scroll_end(animate=False)

            elif event.event_type == "TOOL_COMPLETED":
                _remove_thinking_indicator()
                name = event.payload.get("name", "tool")
                art_str = f" [Artifact: {event.payload.get('artifact_id')}]" if event.payload.get("offloaded") else ""
                card = Static(Text(f"✓ Tool Completed: {name}{art_str}", style="bold green"), classes="tool-invocation-card")
                scroll.mount(card)
                scroll.scroll_end(animate=False)

            elif event.event_type == "TOOL_FAILED":
                _remove_thinking_indicator()
                name = event.payload.get("name", "tool")
                err = event.payload.get("error", "Error")
                card = Static(Text(f"✗ Tool Failed: {name} - {err}", style="bold red"), classes="tool-invocation-card")
                scroll.mount(card)
                scroll.scroll_end(animate=False)

            elif event.event_type == "PLAN_CREATED":
                _remove_thinking_indicator()
                title = event.payload.get("title", "Execution Plan")
                comp = event.payload.get("complexity", "COMPLEX")
                card = Static(Text(f"📋 Plan Initialized: {title} ({comp})\nStatus: IN_PROGRESS (Executing DAG...)", style="bold cyan"), classes="tool-invocation-card")
                scroll.mount(card)
                scroll.scroll_end(animate=False)

            elif event.event_type == "PLAN_COMPLETED":
                _remove_thinking_indicator()
                plan_id = event.payload.get("plan_id", "")
                dur = event.payload.get("duration_seconds", 0.0)
                nodes_done = event.payload.get("nodes_completed", 0)
                tot = event.payload.get("total_nodes", 0)
                card = Static(Text(f"✓ Plan Completed: [{plan_id}] ({nodes_done}/{tot} nodes in {dur:.2f}s)", style="bold green"), classes="tool-invocation-card")
                scroll.mount(card)
                scroll.scroll_end(animate=False)

            elif event.event_type == "PLAN_FAILED":
                _remove_thinking_indicator()
                plan_id = event.payload.get("plan_id", "")
                err = event.payload.get("reason", "Plan execution failed")
                card = Static(Text(f"✗ Plan Failed: [{plan_id}] - {err}", style="bold red"), classes="tool-invocation-card")
                scroll.mount(card)
                scroll.scroll_end(animate=False)

            elif event.event_type == "PROVIDER_FALLBACK_STARTED":
                from_p = event.payload.get("from_provider", "cloud")
                to_p = event.payload.get("to_provider", "local")
                card = Static(Text(f"⚡ Provider Fallback: Cloud provider '{from_p}' unavailable. Continuing locally with {to_p}...", style="bold yellow"), classes="tool-invocation-card")
                scroll.mount(card)
                scroll.scroll_end(animate=False)

            elif event.event_type == "TEXT_RESET":
                # Clear partially rendered text bubble from failed provider before rendering fallback stream
                if self._active_agent_bubble is not None:
                    self._active_agent_bubble.update(Text("", style="dim"))

            elif event.event_type == "TURN_CANCELLED":
                _remove_thinking_indicator()
                if self._active_agent_bubble is None:
                    self._active_agent_bubble = Static("", classes="chat-msg-agent")
                    scroll.mount(self._active_agent_bubble)
                self._active_agent_bubble.update(Text("Presence: [Turn Cancelled]", style="bold red"))
                scroll.scroll_end(animate=False)
                _reset_input_state()

            elif event.event_type == "TURN_FAILED":
                _remove_thinking_indicator()
                err = event.payload.get("error", "Failure")
                if self._active_agent_bubble is None:
                    self._active_agent_bubble = Static("", classes="chat-msg-agent")
                    scroll.mount(self._active_agent_bubble)
                self._active_agent_bubble.update(Text(f"Presence: Error - {err}", style="bold red"))
                scroll.scroll_end(animate=False)
                _reset_input_state()

            elif event.event_type == "TURN_COMPLETED":
                _remove_thinking_indicator()
                final_text = event.payload.get("final_text", "")
                if "</think>" in final_text:
                    final_text = final_text.split("</think>", 1)[1].strip()
                if final_text:
                    disp = sanitize_and_redact(final_text)
                    if self._active_agent_bubble:
                        self._active_agent_bubble.update(Text(f"Presence:\n{disp}", style="white"))
                    else:
                        self._active_agent_bubble = Static(Text(f"Presence:\n{disp}", style="white"), classes="chat-msg-agent")
                        scroll.mount(self._active_agent_bubble)
                scroll.scroll_end(animate=False)
                _reset_input_state()

        def _reset_input_state():
            _remove_thinking_indicator()
            self._is_generating = False
            self._current_cancel_token = None
            try:
                btn = self.query_one("#chat-send-btn", Button)
                btn.label = "Send"
                btn.variant = "primary"
                btn.disabled = False
            except Exception:
                pass
            try:
                self.query_one("#chat-input", Input).focus()
            except Exception:
                pass

        def _safe_dispatch(fn, *args):
            if hasattr(self.app, "safe_call"):
                self.app.safe_call(fn, *args)
            else:
                self.app.call_from_thread(fn, *args)

        try:
            for ev in self.session.execute_turn(user_input=user_text, cancellation_token=token):
                _safe_dispatch(handle_event, ev)
        except Exception as e:
            def on_error(err_msg: str):
                _remove_thinking_indicator()
                if not self._active_agent_bubble:
                    self._active_agent_bubble = Static("", classes="chat-msg-agent")
                    scroll.mount(self._active_agent_bubble)
                self._active_agent_bubble.update(Text(f"Presence Error: {err_msg}", style="bold red"))
                scroll.scroll_end(animate=False)
                _reset_input_state()
            _safe_dispatch(on_error, str(e))
        finally:
            _safe_dispatch(_reset_input_state)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handles button clicks in Chat view."""
        if event.button.id == "chat-send-btn":
            if self._is_generating:
                if self._current_cancel_token:
                    self._current_cancel_token.cancel()
                    try:
                        event.button.label = "Cancelling..."
                        event.button.disabled = True
                    except Exception:
                        pass
            else:
                input_box = self.query_one("#chat-input", Input)
                text = input_box.value.strip()
                if text:
                    self.action_submit_chat(text)
        elif event.button.id == "chat-configure-key-btn":
            if hasattr(self.app, "action_open_settings"):
                self.app.action_open_settings()

    def on_provider_switched(self, new_provider: Any) -> None:
        """Invoked when model provider is hot-swapped."""
        model_id = getattr(new_provider, "model_id", "unknown")
        provider_name = getattr(new_provider, "provider_name", "unknown")
        try:
            title_widget = self.query_one("#chat-session-title", Static)
            title_widget.update(f"Chat Session: {self.session.conversation_id[:8]}... | Model: {model_id} ({provider_name})")
        except Exception:
            pass

        try:
            scroll = self.query_one("#chat-messages-scroll", VerticalScroll)
            note = Text()
            if provider_name == "ollama":
                note.append("🔒 Operating in Local-Only Companion Mode: ", style="bold green")
                note.append(f"{model_id}\n", style="bold cyan")
                note.append("Inference endpoint: http://127.0.0.1:11434. Running 100% locally with zero cloud transmission.", style="dim")
            elif provider_name == "freellmapi":
                base_url = getattr(new_provider, "base_url", "http://127.0.0.1:3001/v1")
                note.append("✅ Successfully connected to FreeLLMAPI Gateway: ", style="bold green")
                note.append(f"{model_id}\n", style="bold cyan")
                note.append(f"Inference gateway endpoint: {base_url}. Messages will route through FreeLLMAPI.", style="dim")
            else:
                note.append("✅ Successfully connected to live Google Gemini: ", style="bold green")
                note.append(f"{model_id}\n", style="bold cyan")
                note.append("Your messages will now be processed directly by Google Gemini API.", style="dim")
            scroll.mount(Static(note, classes="chat-msg-agent"))
            scroll.scroll_end(animate=False)
        except Exception:
            pass

        try:
            btn = self.query_one("#chat-configure-key-btn")
            btn.display = False
        except Exception:
            pass

