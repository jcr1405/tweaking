"""MCP Server Monitor View Widget (View 03)
Operational monitoring, process status, Job Object containment, tool catalog,
and administrative control over configured MCP servers.
"""

from typing import Optional, Any, List
from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Static, DataTable, Button
from textual.containers import Vertical, Horizontal, VerticalScroll

from runtime.observability.models import MCPServerViewDTO


class MCPView(Static):
    """View 03: MCP Server Monitor & Tool Catalog."""

    def __init__(self, service: Any, **kwargs):
        super().__init__(**kwargs)
        self.service = service
        self._servers: List[MCPServerViewDTO] = []
        self._selected_server_id: Optional[str] = None

    def compose(self) -> ComposeResult:
        with Vertical(id="mcp-container"):
            with Horizontal(classes="mcp-actions-bar"):
                yield Button("Refresh Status", id="mcp-refresh-btn", variant="primary")
                yield Button("Toggle Enable/Disable", id="mcp-toggle-btn", variant="default")
                yield Button("Stop Process", id="mcp-stop-btn", variant="error")

            yield Static("CONFIGURED MCP SERVERS", classes="panel-header")
            with Vertical(id="mcp-table-container"):
                yield DataTable(id="mcp-table")

            yield Static("SERVER DETAILS & TOOL CATALOG", classes="panel-header")
            with VerticalScroll(id="mcp-details-container"):
                yield Static(id="mcp-details-text")

    def on_mount(self) -> None:
        table = self.query_one("#mcp-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("Server ID", "Name", "Config", "Process", "PID", "Job Object", "Memory", "Tools (Appr/Disc)")
        self.refresh_servers()

    def refresh_servers(self) -> None:
        """Queries MCPSupervisor catalog without launching stopped processes."""
        self._servers = self.service.get_mcp_catalog()
        table = self.query_one("#mcp-table", DataTable)
        table.clear()

        for s in self._servers:
            cfg_state = "ENABLED" if s.enabled else "DISABLED"
            proc_state = "RUNNING" if s.process_alive else s.state
            pid_str = str(s.process_id) if s.process_id else "--"
            job_str = "YES" if s.in_job_object else "NO"
            mem_str = f"{s.memory_mb:.1f} MB" if s.memory_mb > 0 else "--"
            tools_str = f"{s.approved_tool_count}/{s.discovered_tool_count}"

            table.add_row(
                s.server_id,
                s.name,
                cfg_state,
                proc_state,
                pid_str,
                job_str,
                mem_str,
                tools_str,
                key=s.server_id
            )

        if self._servers:
            first_sid = self._servers[0].server_id
            self._display_server_details(first_sid)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handles selecting a server row from the table."""
        if event.row_key and event.row_key.value:
            self._display_server_details(str(event.row_key.value))

    def _display_server_details(self, server_id: str) -> None:
        """Displays details and tool catalog for the selected server."""
        self._selected_server_id = server_id
        server = next((s for s in self._servers if s.server_id == server_id), None)
        detail_widget = self.query_one("#mcp-details-text", Static)

        if not server:
            detail_widget.update("Server not found.")
            return

        body = Text()
        body.append(f"Server ID:    {server.server_id} ({server.name})\n", style="bold cyan")
        body.append(f"Executable:   {server.executable}\n")
        body.append(f"Arguments:    {' '.join(server.args)}\n")
        body.append(f"Transport:    {server.transport}\n")
        body.append(f"Process PID:  {server.process_id or 'None'} (Alive: {server.process_alive})\n")
        body.append(f"Job Object:   {'Enforced (Restricted Sandbox)' if server.in_job_object else 'None'}\n")
        body.append(f"Restarts:     {server.restarts}\n")
        if server.last_error:
            body.append(f"Last Error:   {server.last_error}\n", style="bold red")

        body.append("\nTOOL CATALOG:\n", style="bold yellow")
        if not server.tools:
            body.append("  No tools discovered or cached for this server.\n", style="dim")
        else:
            for t in server.tools:
                app_str = "APPROVED" if t.approved else "UNAPPROVED (Default Deny)"
                style = "green" if t.approved else "yellow"
                body.append(f"  • {t.namespaced_name} [{t.authority_tier}] - {app_str}\n", style=style)
                if t.description:
                    body.append(f"    {t.description}\n", style="dim")

        detail_widget.update(body)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handles administrative controls."""
        if event.button.id == "mcp-refresh-btn":
            self.refresh_servers()
        elif event.button.id == "mcp-toggle-btn" and self._selected_server_id:
            server = next((s for s in self._servers if s.server_id == self._selected_server_id), None)
            if server:
                new_state = not server.enabled
                self.service.set_mcp_server_enabled(self._selected_server_id, new_state)
                self.refresh_servers()
        elif event.button.id == "mcp-stop-btn" and self._selected_server_id:
            self.service.stop_mcp_server(self._selected_server_id)
            self.refresh_servers()
