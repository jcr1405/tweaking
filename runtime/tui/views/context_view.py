"""Context Inspector View Widget (View 06)
Structural breakdown of assembled prompt context:
system instructions, conversation history, summaries, tool schemas, and token estimates.
"""

from typing import Optional, Any
from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Static, Button
from textual.containers import Vertical, Horizontal, VerticalScroll

from runtime.observability.models import ContextInspectionDTO
from runtime.observability.redaction import sanitize_and_redact


class ContextView(Static):
    """View 06: Context Inspector & Prompt Replay Breakdown."""

    def __init__(self, service: Any, session: Any, **kwargs):
        super().__init__(**kwargs)
        self.service = service
        self.session = session
        self._dto: Optional[ContextInspectionDTO] = None

    def compose(self) -> ComposeResult:
        with Vertical(id="context-main-container"):
            with Horizontal(classes="context-nav-bar"):
                yield Button("Re-Inspect Context", id="context-reload-btn", variant="primary")
                yield Static("Prompt Context Reconstruction", id="context-status-label")

            with VerticalScroll(id="context-container"):
                yield Static(id="context-content-text")

    def on_mount(self) -> None:
        self.refresh_context()

    def refresh_context(self) -> None:
        """Inspects assembled context from ContextAssembler and TrajectoryStore."""
        cid = self.session.conversation_id
        self._dto = self.service.inspect_turn_context(cid, turn_idx=1)
        content_widget = self.query_one("#context-content-text", Static)

        if not self._dto:
            content_widget.update("Context inspection unavailable.")
            return

        body = Text()
        body.append("═══ 1. ESTIMATED TOKEN BUDGET & METRICS ═══\n", style="bold cyan")
        body.append(f"Estimated Prompt Tokens: ~{self._dto.estimated_tokens} (Heuristic estimate: ~4 chars/token)\n")
        body.append(f"Compaction Active:       {'YES (Sliding Window Compressed)' if self._dto.is_compacted else 'NO (Full History Fits)'}\n")
        body.append(f"Replayed History Steps:  {self._dto.conversation_history_steps}\n")
        body.append(f"Replayed Messages:       {self._dto.messages_count}\n\n")

        body.append("═══ 2. SYSTEM INSTRUCTIONS ═══\n", style="bold green")
        body.append(f"{self._dto.system_instructions}\n\n", style="dim")

        body.append("═══ 3. PERSISTENT CONVERSATION SUMMARY ═══\n", style="bold yellow")
        if self._dto.summary_text:
            body.append(f"{self._dto.summary_text}\n\n")
        else:
            body.append("No persistent summary generated yet (Conversation within initial sliding window).\n\n", style="dim")

        body.append("═══ 4. RETRIEVED ARTIFACTS & DATA ═══\n", style="bold magenta")
        if not self._dto.retrieved_data:
            body.append("No offloaded artifacts attached to this conversation context.\n\n", style="dim")
        else:
            for d in self._dto.retrieved_data:
                body.append(f"  • Artifact: {d.get('artifact_id')} | Tool: {d.get('tool')} | Size: {d.get('size')} bytes\n")
            body.append("\n")

        body.append("═══ 5. ASSEMBLED TOOL DECLARATIONS ═══\n", style="bold blue")
        if not self._dto.tool_declarations:
            body.append("No tools declared.\n\n", style="dim")
        else:
            for t in self._dto.tool_declarations:
                body.append(f"  • {t}\n", style="bold cyan")
            body.append("\n")

        body.append("═══ 6. MESSAGES PREVIEW (LATEST 5) ═══\n", style="bold white")
        for m in self._dto.messages_preview:
            role = m.get("role", "user").upper()
            content = sanitize_and_redact(str(m.get("content", "")))[:200]
            body.append(f"[{role}]: {content}\n", style="dim")

        content_widget.update(body)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "context-reload-btn":
            self.refresh_context()
