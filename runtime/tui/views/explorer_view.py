"""Conversation Explorer View Widget (View 02)
Hierarchical browsing, FTS5 full-text search, and detailed turn inspection across
all historical conversations stored in TrajectoryStore and GlobalSummaryManager.
"""

from typing import Optional, Any, List
from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Static, Input, ListView, ListItem, Label
from textual.containers import Vertical, Horizontal, VerticalScroll

from runtime.observability.models import ConversationSummaryDTO, TurnDetailDTO
from runtime.observability.redaction import sanitize_and_redact


class ExplorerView(Static):
    """View 02: Conversation Explorer & Turn Inspection."""

    def __init__(self, service: Any, **kwargs):
        super().__init__(**kwargs)
        self.service = service
        self._conversations: List[ConversationSummaryDTO] = []
        self._selected_cid: Optional[str] = None
        self._turns: List[TurnDetailDTO] = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="explorer-container"):
            # Left pane: Search and Conversation List
            with Vertical(id="explorer-left"):
                yield Static("CONVERSATIONS", classes="panel-header")
                yield Input(placeholder="Search conversations (FTS5)...", id="explorer-search-input")
                yield ListView(id="explorer-conv-list")

            # Right pane: Conversation Details & Turn List
            with Vertical(id="explorer-right"):
                yield Static("CONVERSATION DETAILS", classes="panel-header")
                yield Static("Select a conversation from the left to inspect turns.", id="explorer-conv-meta")
                yield Static("TURNS", classes="panel-header")
                yield VerticalScroll(Static(id="explorer-turns-content"), id="explorer-turns-scroll")

    def on_mount(self) -> None:
        self.load_conversations()

    def load_conversations(self, search_query: Optional[str] = None) -> None:
        """Loads conversation list from GlobalSummaryManager."""
        self._conversations = self.service.list_conversations(limit=50, search_query=search_query)
        list_view = self.query_one("#explorer-conv-list", ListView)
        list_view.clear()

        if not self._conversations:
            list_view.append(ListItem(Label("No conversations found.")))
            return

        for conv in self._conversations:
            title_text = Text()
            title_text.append(f"{conv.title[:24]}\n", style="bold")
            title_text.append(f"Steps: {conv.step_count} | {conv.created_at[:10]}", style="dim")
            item = ListItem(Label(title_text))
            item.conv_id = conv.conversation_id
            list_view.append(item)

        # Select first by default if available
        if self._conversations:
            self._select_conversation(self._conversations[0].conversation_id)

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filters conversations as user types in search bar."""
        if event.input.id == "explorer-search-input":
            self.load_conversations(search_query=event.value.strip())

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handles selecting a conversation from the list."""
        if event.item and hasattr(event.item, "conv_id") and event.item.conv_id:
            self._select_conversation(event.item.conv_id)

    def _select_conversation(self, conversation_id: str) -> None:
        """Loads and displays turns for the selected conversation."""
        self._selected_cid = conversation_id
        meta_rec = next((c for c in self._conversations if c.conversation_id == conversation_id), None)

        meta_widget = self.query_one("#explorer-conv-meta", Static)
        if meta_rec:
            meta_text = Text()
            meta_text.append(f"Title:     {meta_rec.title}\n", style="bold cyan")
            meta_text.append(f"ID:        {meta_rec.conversation_id}\n")
            meta_text.append(f"Created:   {meta_rec.created_at}\n")
            meta_text.append(f"Modified:  {meta_rec.last_modified_at}\n")
            meta_text.append(f"Steps:     {meta_rec.step_count} | Status: {meta_rec.status}\n")
            if meta_rec.workspace_path:
                meta_text.append(f"Workspace: {meta_rec.workspace_path}\n")
            meta_widget.update(meta_text)
        else:
            meta_widget.update(f"ID: {conversation_id}")

        # Fetch turns
        self._turns = self.service.get_conversation_turns(conversation_id)
        turns_widget = self.query_one("#explorer-turns-content", Static)

        if not self._turns:
            turns_widget.update(Text("No turns recorded for this conversation.", style="dim"))
            return

        body = Text()
        for t in self._turns:
            body.append(f"═══ TURN #{t.turn_idx} ═══\n", style="bold yellow")
            body.append(f"User:  {sanitize_and_redact(t.user_input)}\n", style="bold cyan")

            if t.tool_calls:
                body.append(f"Tools ({len(t.tool_calls)}):\n", style="bold")
                for tc in t.tool_calls:
                    body.append(f"  • {tc.get('name')} [{tc.get('status')}]\n", style="yellow")
                    if tc.get("artifact_id"):
                        body.append(f"    Artifact: {tc.get('artifact_id')}\n", style="dim cyan")

            if t.agent_output:
                preview = sanitize_and_redact(t.agent_output[:300])
                if len(t.agent_output) > 300:
                    preview += "..."
                body.append(f"Agent:\n{preview}\n\n", style="white")

            if t.prompt_tokens or t.completion_tokens:
                body.append(f"Tokens: Prompt {t.prompt_tokens} / Completion {t.completion_tokens}\n", style="dim")
            body.append("\n")

        turns_widget.update(body)
