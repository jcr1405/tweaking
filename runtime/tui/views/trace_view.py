"""Execution Trace View Widget (View 05)
High-precision chronological timeline of an individual agent turn:
context assembly, model requests, permission checks, tool runs, and completion.
"""

from typing import Optional, Any, List
from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Static, ListView, ListItem, Label, Button
from textual.containers import Vertical, Horizontal, VerticalScroll

from runtime.observability.models import TraceEventDTO
from runtime.observability.redaction import sanitize_and_redact


class TraceView(Static):
    """View 05: Execution Trace & Chronological Event Timeline."""

    def __init__(self, service: Any, session: Any, **kwargs):
        super().__init__(**kwargs)
        self.service = service
        self.session = session
        self._trace_events: List[TraceEventDTO] = []
        self._current_turn_idx: int = 1

    def compose(self) -> ComposeResult:
        with Vertical(id="trace-main-container"):
            with Horizontal(classes="trace-nav-bar"):
                yield Button("Prev Turn", id="trace-prev-btn", variant="default")
                yield Static(f"Turn #{self._current_turn_idx}", id="trace-turn-label")
                yield Button("Next Turn", id="trace-next-btn", variant="default")
                yield Button("Reload Trace", id="trace-reload-btn", variant="primary")

            with Horizontal(id="trace-container"):
                with Vertical(id="trace-timeline-container"):
                    yield Static("EXECUTION TIMELINE", classes="panel-header")
                    yield ListView(id="trace-event-list")

                with Vertical(id="trace-detail-container"):
                    yield Static("EVENT INSPECTION", classes="panel-header")
                    yield VerticalScroll(Static(id="trace-detail-text"))

    def on_mount(self) -> None:
        self.load_turn_trace(self._current_turn_idx)

    def load_turn_trace(self, turn_idx: int) -> None:
        """Loads trace events for the given turn index."""
        self._current_turn_idx = max(1, turn_idx)
        self.query_one("#trace-turn-label", Static).update(f"Turn #{self._current_turn_idx}")

        cid = self.session.conversation_id
        self._trace_events = self.service.get_turn_trace(cid, self._current_turn_idx)
        list_view = self.query_one("#trace-event-list", ListView)
        list_view.clear()

        if not self._trace_events:
            list_view.append(ListItem(Label("No trace recorded for this turn.")))
            self.query_one("#trace-detail-text", Static).update("No events to inspect.")
            return

        for ev in self._trace_events:
            item_text = Text()
            item_text.append(f"+{ev.offset_seconds:.3f}s ", style="dim")
            style = "green" if ev.status == "COMPLETED" else ("red" if ev.status == "FAILED" else "yellow")
            item_text.append(f"{ev.event_name}\n", style=f"bold {style}")
            item_text.append(f"  [{ev.component}]", style="dim cyan")
            item = ListItem(Label(item_text))
            item.event_idx = ev.event_idx
            list_view.append(item)

        self._display_event_details(self._trace_events[0].event_idx)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item and hasattr(event.item, "event_idx"):
            self._display_event_details(event.item.event_idx)

    def _display_event_details(self, event_idx: int) -> None:
        """Renders metadata and payload for a selected trace step."""
        ev = next((e for e in self._trace_events if e.event_idx == event_idx), None)
        detail_widget = self.query_one("#trace-detail-text", Static)

        if not ev:
            detail_widget.update("Event not found.")
            return

        body = Text()
        body.append(f"Event:       {ev.event_name}\n", style="bold cyan")
        body.append(f"Component:   {ev.component}\n")
        body.append(f"Timeline:    +{ev.offset_seconds:.3f}s from turn start\n")
        body.append(f"Status:      {ev.status}\n")

        if ev.duration_ms > 0:
            body.append(f"Duration:    {ev.duration_ms:.2f} ms\n")

        if ev.artifact_id:
            body.append(f"Artifact:    {ev.artifact_id}\n", style="cyan")

        if ev.error:
            body.append(f"Error:       {ev.error}\n", style="bold red")

        if ev.details:
            body.append("\nDETAILS:\n", style="bold yellow")
            for k, v in ev.details.items():
                body.append(f"  {k}: {sanitize_and_redact(str(v))}\n")

        detail_widget.update(body)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "trace-prev-btn":
            if self._current_turn_idx > 1:
                self.load_turn_trace(self._current_turn_idx - 1)
        elif event.button.id == "trace-next-btn":
            self.load_turn_trace(self._current_turn_idx + 1)
        elif event.button.id == "trace-reload-btn":
            self.load_turn_trace(self._current_turn_idx)
