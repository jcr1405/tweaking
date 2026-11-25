"""Task Monitor View Widget (View 04)
Application-wide task tree tracking, parent-child hierarchies, duration telemetry,
and safe cancellation controls across all conversations and tools.
"""

from typing import Optional, Any, List
from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Static, Tree, Button
from textual.containers import Vertical, Horizontal, VerticalScroll

from runtime.observability.models import TaskNode, TaskState


class TasksView(Static):
    """View 04: Application-wide Task Monitor & Tree."""

    def __init__(self, service: Any, **kwargs):
        super().__init__(**kwargs)
        self.service = service
        self._selected_task_id: Optional[str] = None

    def compose(self) -> ComposeResult:
        with Vertical(id="tasks-main-container"):
            with Horizontal(classes="tasks-actions-bar"):
                yield Button("Refresh Tasks", id="tasks-refresh-btn", variant="primary")
                yield Button("Cancel Selected Task", id="tasks-cancel-btn", variant="error")

            with Horizontal(id="tasks-container"):
                with Vertical(id="tasks-tree-container"):
                    yield Static("TASK HIERARCHY", classes="panel-header")
                    yield Tree("Presence Tasks", id="tasks-tree")

                with Vertical(id="tasks-details-container"):
                    yield Static("TASK DETAILS", classes="panel-header")
                    yield VerticalScroll(Static(id="tasks-detail-text"))

    def on_mount(self) -> None:
        self.refresh_tasks()
        self._subscribe_live_events()

    def on_unmount(self) -> None:
        self._unsubscribe_live_events()

    def _subscribe_live_events(self) -> None:
        """Subscribes to EventBus for automatic reactive updates without manual refresh."""
        event_bus = getattr(getattr(self.service, "runtime", None), "events", None)
        if not event_bus or not hasattr(event_bus, "subscribe"):
            return

        def _on_event(event: Any) -> None:
            try:
                if self.app:
                    self.app.call_from_thread(self.refresh_tasks)
            except Exception:
                pass

        self._live_listener = _on_event
        self._subscribed_events = (
            "WORKFLOW_STARTED",
            "WORKFLOW_NODE_STARTED",
            "WORKFLOW_NODE_COMPLETED",
            "WORKFLOW_NODE_FAILED",
            "WORKFLOW_COMPLETED",
            "WORKFLOW_FAILED",
            "TASK_CANCELLED",
            "TASK_COMPLETED",
            "TASK_FAILED",
            "PLAN_CREATED",
            "PLAN_STATUS_CHANGED",
        )
        for ev in self._subscribed_events:
            try:
                event_bus.subscribe(ev, self._live_listener)
            except Exception:
                pass

    def _unsubscribe_live_events(self) -> None:
        """Cleans up EventBus subscriptions upon view unmount."""
        event_bus = getattr(getattr(self.service, "runtime", None), "events", None)
        if not event_bus or not hasattr(event_bus, "unsubscribe") or not hasattr(self, "_live_listener"):
            return
        for ev in getattr(self, "_subscribed_events", ()):
            try:
                event_bus.unsubscribe(ev, self._live_listener)
            except Exception:
                pass

    def refresh_tasks(self) -> None:
        """Populates the task tree from ActiveTaskRegistry."""
        tree = self.query_one("#tasks-tree", Tree)
        tree.clear()
        tree.root.set_label("Presence Runtime Tasks")

        active_tasks = self.service.task_registry.get_active_tasks()
        completed_tasks = self.service.task_registry.get_completed_tasks(limit=30)

        # Active tasks branch
        active_branch = tree.root.add(f"ACTIVE TASKS ({len(active_tasks)})", expand=True)
        if not active_tasks:
            active_branch.add_leaf("No active tasks (System Idle)")
        else:
            # Build tree by roots
            root_tasks = self.service.task_registry.get_active_roots()
            for r in root_tasks:
                self._add_task_node_to_tree(active_branch, r)

        # Completed tasks branch
        completed_branch = tree.root.add(f"COMPLETED / RECENT ({len(completed_tasks)})", expand=True)
        if not completed_tasks:
            completed_branch.add_leaf("No completed tasks.")
        else:
            for c in completed_tasks:
                state_color = "green" if c.state == TaskState.COMPLETED else "red"
                label = Text(f"[{c.state.value}] {c.display_name} ({c.elapsed_seconds:.2f}s)", style=state_color)
                node = completed_branch.add_leaf(label)
                node.data = c.task_id

    def _add_task_node_to_tree(self, parent_tree_node: Any, task_node: TaskNode) -> None:
        """Recursively builds tree nodes for tasks and their children."""
        label = Text(f"● [{task_node.state.value}] {task_node.display_name} ({task_node.elapsed_seconds:.1f}s)", style="bold cyan")
        tree_node = parent_tree_node.add(label, expand=True)
        tree_node.data = task_node.task_id

        for child in task_node.children:
            self._add_task_node_to_tree(tree_node, child)

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        """Displays details when a task is selected in the tree."""
        if event.node and event.node.data:
            self._display_task_details(str(event.node.data))

    def _display_task_details(self, task_id: str) -> None:
        """Inspects properties of the selected task."""
        self._selected_task_id = task_id
        task = self.service.task_registry.get_task(task_id)
        detail_widget = self.query_one("#tasks-detail-text", Static)

        if not task:
            detail_widget.update("Task not found.")
            return

        body = Text()
        body.append(f"Task ID:       {task.task_id}\n", style="bold cyan")
        body.append(f"Display Name:  {task.display_name}\n", style="bold")
        body.append(f"Type:          {task.task_type.value}\n")
        body.append(f"State:         {task.state.value}\n", style="bold yellow")
        body.append(f"Parent Task:   {task.parent_task_id or 'None (Root Task)'}\n")
        body.append(f"Conversation:  {task.conversation_id or 'Application Scope'}\n")
        body.append(f"Elapsed:       {task.elapsed_seconds:.2f} seconds\n")
        body.append(f"Cancellable:   {task.cancellable}\n")

        if task.error_message:
            body.append(f"\nError Details:\n{task.error_message}\n", style="bold red")

        if task.artifact_id:
            body.append(f"\nAssociated Artifact: {task.artifact_id}\n", style="cyan")

        if task.metadata:
            body.append(f"\nMetadata: {task.metadata}\n", style="dim")

        detail_widget.update(body)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "tasks-refresh-btn":
            self.refresh_tasks()
        elif event.button.id == "tasks-cancel-btn" and self._selected_task_id:
            success = self.service.request_task_cancellation(self._selected_task_id)
            if success:
                self.refresh_tasks()
                self._display_task_details(self._selected_task_id)
