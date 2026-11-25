"""Presence Terminal Control Center Application
Full-screen interactive TUI integrating chat, conversation exploration,
MCP supervision, task hierarchy monitoring, execution trace, and system health.
"""

import sys
import time
from typing import Optional, Any
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Header, Footer, Static, Button, ListView, ListItem, Label, ContentSwitcher
from textual.containers import Horizontal, Vertical

from runtime.lifecycle import PresenceRuntime
from runtime.session import AgentSession
from runtime.observability.service import RuntimeObservabilityService
from runtime.observability.models import RuntimeSnapshot
from runtime.tui.styles import TUI_CSS
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


class PresenceControlCenterApp(App):
    """Full-screen terminal control center for Presence."""

    TITLE = "PRESENCE COMPANION"
    SUB_TITLE = "Personal Computer Companion [LOCAL-ONLY MODE] [OBSERVING: ACTIVE]"
    CSS = TUI_CSS

    BINDINGS = [
        Binding("f1", "switch_view('view-chat')", "Chat", priority=True),
        Binding("f2", "switch_view('view-explorer')", "Conversations", priority=True),
        Binding("f3", "switch_view('view-mcp')", "MCP Servers", priority=True),
        Binding("f4", "switch_view('view-tasks')", "Tasks", priority=True),
        Binding("f5", "switch_view('view-trace')", "Execution Trace", priority=True),
        Binding("f6", "switch_view('view-context')", "Context Inspector", priority=True),
        Binding("f7", "switch_view('view-health')", "System Health", priority=True),
        Binding("f8", "toggle_activity_panel", "Activity Panel", priority=True),
        Binding("f9", "open_settings", "Settings (API Key)", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(
        self,
        runtime: PresenceRuntime,
        active_session: Optional[AgentSession] = None,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.runtime = runtime
        self.service: RuntimeObservabilityService = runtime.observability
        self.active_session = active_session or self.runtime.create_session(title="Control Center Interactive Session")
        self._unsubscribe_bridge: Optional[Any] = None
        self._refresh_timer: Optional[Any] = None

        # Wire TUI interactive approval provider to runtime permissions
        self.approval_provider: Optional[Any] = None
        if hasattr(self.runtime, "permissions") and self.runtime.permissions:
            from runtime.tui.approval_provider import TUIApprovalProvider
            self.approval_provider = TUIApprovalProvider(
                app=self,
                permission_boundary=self.runtime.permissions,
            )
            self.runtime.permissions._consent_provider = self.approval_provider
            if self.active_session and hasattr(self.active_session, "permission_boundary"):
                self.active_session.permission_boundary._consent_provider = self.approval_provider

    def compose(self) -> ComposeResult:
        # 1. Top Header Bar
        yield Header(show_clock=True)

        with Horizontal(id="main-container"):
            # 2. Navigation Sidebar
            with Vertical(id="nav-sidebar"):
                yield Static("LOCAL-ONLY: OLLAMA", id="mode-badge", classes="mode-badge-local")
                yield Static("[OBSERVING: ACTIVE]", id="observing-badge", classes="observing-badge-active")
                yield Static("NAVIGATION", classes="nav-title")
                with Vertical(id="nav-buttons"):
                    yield Button("1. Chat (F1)", id="nav-btn-chat", variant="primary")
                    yield Button("2. Explorer (F2)", id="nav-btn-explorer", variant="default")
                    yield Button("3. MCP (F3)", id="nav-btn-mcp", variant="default")
                    yield Button("4. Tasks (F4)", id="nav-btn-tasks", variant="default")
                    yield Button("5. Trace (F5)", id="nav-btn-trace", variant="default")
                    yield Button("6. Context (F6)", id="nav-btn-context", variant="default")
                    yield Button("7. Health (F7)", id="nav-btn-health", variant="default")
                    yield Button("8. Settings (F9)", id="nav-btn-settings", variant="default")

                yield Static("RECENT CHATS", classes="recent-chats-title")
                yield ListView(id="recent-chats-list")

            # 3. Central Swappable Workspace
            with Vertical(id="workspace"):
                with ContentSwitcher(initial="view-chat", id="workspace-switcher"):
                    yield ChatView(self.service, self.active_session, id="view-chat")
                    yield ExplorerView(self.service, id="view-explorer")
                    yield MCPView(self.service, id="view-mcp")
                    yield TasksView(self.service, id="view-tasks")
                    yield TraceView(self.service, self.active_session, id="view-trace")
                    yield ContextView(self.service, self.active_session, id="view-context")
                    yield HealthView(self.service, id="view-health")

            # 4. Right-Side Activity Panel
            with Vertical(id="activity-sidebar"):
                yield ActivityPanel(id="activity-panel-widget")

        # 5. Footer with shortcuts
        yield Footer()

    def safe_call(self, callback: Any, *args: Any, **kwargs: Any) -> None:
        """Invokes callback directly if on app thread, or via call_from_thread if on background thread."""
        import threading
        try:
            if getattr(self, "_thread_id", None) == threading.get_ident():
                callback(*args, **kwargs)
            else:
                self.call_from_thread(callback, *args, **kwargs)
        except Exception:
            pass


    def on_mount(self) -> None:
        """Initializes event subscriptions and low-frequency telemetry timer."""
        # Subscribe activity panel to event bridge
        def on_event(norm_event):
            self.safe_call(self._on_live_event)

        self._unsubscribe_bridge = self.service.event_bridge.subscribe(on_event)

        # Populate recent chats list in sidebar
        self._refresh_recent_chats()

        # Low frequency telemetry refresh (every 2.0s)
        self._refresh_timer = self.set_interval(2.0, self._on_telemetry_tick)
        self._on_telemetry_tick()

    def _on_live_event(self) -> None:
        """Triggered when an event passes through the bridge."""
        try:
            panel = self.query_one("#activity-panel-widget", ActivityPanel)
            snap = self.service.get_runtime_snapshot()
            panel.update_snapshot(snap)
        except Exception:
            pass

    def _on_telemetry_tick(self) -> None:
        """Periodic 2-second background refresh for activity panel."""
        try:
            panel = self.query_one("#activity-panel-widget", ActivityPanel)
            snap = self.service.get_runtime_snapshot()
            panel.update_snapshot(snap)
        except Exception:
            pass

    def _refresh_recent_chats(self) -> None:
        """Fills recent chats in the left sidebar."""
        try:
            recent_list = self.query_one("#recent-chats-list", ListView)
            recent_list.clear()
            convs = self.service.list_conversations(limit=5)
            for c in convs:
                item = ListItem(Label(f"• {c.title[:16]}"))
                item.conv_id = c.conversation_id
                recent_list.append(item)
        except Exception:
            pass

    def action_switch_view(self, view_id: str) -> None:
        """Switches central workspace to the specified view."""
        switcher = self.query_one("#workspace-switcher", ContentSwitcher)
        switcher.current = view_id

    def action_toggle_activity_panel(self) -> None:
        """Toggles visibility of the right-side activity panel."""
        sidebar = self.query_one("#activity-sidebar")
        sidebar.display = not sidebar.display

    def action_open_settings(self) -> None:
        """Opens the Gemini API Key configuration modal dialog."""
        current_model = "gemini-2.5-flash"
        if self.active_session and self.active_session.model_provider:
            mid = getattr(self.active_session.model_provider, "model_id", "gemini-2.5-flash")
            if not mid.startswith("fake"):
                current_model = mid
        from runtime.tui.settings_modal import ApiKeyModal
        self.push_screen(ApiKeyModal(self.service, current_model=current_model))

    def apply_gemini_key_to_session(self, key: str, model_id: str) -> None:
        """Hot-swaps active session model provider to live Gemini."""
        from runtime.providers.gemini import GeminiModelProvider
        from runtime.providers.coordinator import MultiProviderCoordinator
        gemini = GeminiModelProvider(api_key=key, model_id=model_id)
        valid, _ = gemini.validate_configuration()
        if valid and self.active_session:
            prov = self.active_session.model_provider
            if isinstance(prov, MultiProviderCoordinator):
                prov.register_provider("gemini", gemini)
                prov.set_primary_provider("gemini")
            else:
                self.active_session.model_provider = gemini
                self.active_session.agent_executor.provider = gemini
            if hasattr(self.runtime, "session_factory") and self.runtime.session_factory:
                self.runtime.session_factory.default_model_provider = gemini
            try:
                chat_view = self.query_one("#view-chat", ChatView)
                chat_view.on_provider_switched(gemini)
            except Exception:
                pass

    def apply_freellmapi_provider_to_session(self, base_url: str, api_key: str, model_id: str) -> None:
        """Hot-swaps active session model provider to FreeLLMAPI gateway."""
        from runtime.providers.freellmapi import FreeLLMAPIProvider
        from runtime.providers.coordinator import MultiProviderCoordinator
        freellm = FreeLLMAPIProvider(base_url=base_url, api_key=api_key, model_id=model_id)
        valid, _ = freellm.validate_configuration()
        if valid and self.active_session:
            prov = self.active_session.model_provider
            if isinstance(prov, MultiProviderCoordinator):
                prov.register_provider("freellmapi", freellm)
                prov.set_primary_provider("freellmapi")
            else:
                self.active_session.model_provider = freellm
                self.active_session.agent_executor.provider = freellm
            if hasattr(self.runtime, "session_factory") and self.runtime.session_factory:
                self.runtime.session_factory.default_model_provider = freellm
            try:
                chat_view = self.query_one("#view-chat", ChatView)
                chat_view.on_provider_switched(freellm)
            except Exception:
                pass


    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handles navigation sidebar button clicks."""
        if event.button.id == "nav-btn-settings":
            self.action_open_settings()
            return

        nav_map = {
            "nav-btn-chat": "view-chat",
            "nav-btn-explorer": "view-explorer",
            "nav-btn-mcp": "view-mcp",
            "nav-btn-tasks": "view-tasks",
            "nav-btn-trace": "view-trace",
            "nav-btn-context": "view-context",
            "nav-btn-health": "view-health",
        }
        if event.button.id in nav_map:
            self.action_switch_view(nav_map[event.button.id])

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handles selecting a recent chat from the left sidebar."""
        if event.list_view.id == "recent-chats-list" and event.item and hasattr(event.item, "conv_id") and event.item.conv_id:
            cid = event.item.conv_id
            # Open selected conversation in Explorer view
            self.action_switch_view("view-explorer")
            explorer = self.query_one("#view-explorer", ExplorerView)
            explorer._select_conversation(cid)

    def on_unmount(self) -> None:
        """Cleans up timers, subscriptions, and active sessions on app exit."""
        if self._refresh_timer:
            self._refresh_timer.stop()
        if self._unsubscribe_bridge:
            try:
                self._unsubscribe_bridge()
            except Exception:
                pass
        if self.active_session and not self.active_session.is_closed:
            try:
                self.active_session.close()
            except Exception:
                pass
