"""System Health View Widget (View 07)
Real-time operational dashboard: host process resources, MCP process tree metrics,
and SearchGovernor traffic telemetry, rate limits, and circuit breaker status.
"""

from typing import Optional, Any
from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Static, Button
from textual.containers import Vertical, Horizontal, VerticalScroll

from runtime.observability.models import RuntimeSnapshot


class HealthView(Static):
    """View 07: System Health & Diagnostic Telemetry."""

    def __init__(self, service: Any, **kwargs):
        super().__init__(**kwargs)
        self.service = service
        self._snapshot: Optional[RuntimeSnapshot] = None

    def compose(self) -> ComposeResult:
        with Vertical(id="health-main-container"):
            with Horizontal(classes="health-nav-bar"):
                yield Button("Refresh Health", id="health-refresh-btn", variant="primary")
                yield Button("Reset Search Circuit Breaker", id="health-reset-circuit-btn", variant="warning")

            with VerticalScroll(id="health-container"):
                yield Static(id="health-content-text")

    def on_mount(self) -> None:
        self.refresh_health()

    def refresh_health(self) -> None:
        """Collects fresh telemetry from RuntimeObservabilityService."""
        self._snapshot = self.service.get_runtime_snapshot()
        content_widget = self.query_one("#health-content-text", Static)

        if not self._snapshot:
            content_widget.update("Health snapshot unavailable.")
            return

        body = Text()

        # 1. Host Runtime Card
        body.append("═══ 1. HOST RUNTIME & SYSTEM PROCESS ═══\n", style="bold cyan")
        body.append(f"Instance ID:       {self._snapshot.instance_id}\n")
        body.append(f"PID:               {self._snapshot.pid}\n")
        body.append(f"State:             {self._snapshot.state}\n")
        uptime_min = int(self._snapshot.uptime_seconds // 60)
        uptime_sec = int(self._snapshot.uptime_seconds % 60)
        body.append(f"Uptime:            {uptime_min}m {uptime_sec}s\n")
        body.append(f"CPU Utilization:   {self._snapshot.cpu_percent:.1f}%\n")
        body.append(f"Working Set RAM:   {self._snapshot.memory_working_set_mb:.1f} MB\n")
        body.append(f"Private Memory:    {self._snapshot.memory_private_mb:.1f} MB\n")
        body.append(f"Peak Working Set:  {self._snapshot.memory_peak_mb:.1f} MB\n")
        body.append(f"Active OS Threads: {self._snapshot.thread_count}\n")
        body.append(f"Active Tasks:      {self._snapshot.active_tasks_count}\n")
        body.append(f"Clean Recovery:    {'YES' if self._snapshot.clean_recovery else 'UNCLEAN DETECTED'}\n\n")

        # 2. Search Governor Card
        sg = self._snapshot.search_telemetry or {}
        circuit_state = sg.get("circuit_state", "CLOSED")
        c_color = "green" if circuit_state == "CLOSED" else ("yellow" if circuit_state == "HALF_OPEN" else "red")

        body.append("═══ 2. SEARCH TRAFFIC GOVERNOR & CIRCUIT TELEMETRY ═══\n", style="bold yellow")
        body.append(f"Provider:             {sg.get('provider', 'duckduckgo')}\n")
        body.append("Circuit Breaker:      ")
        body.append(f"[{circuit_state}]\n", style=f"bold {c_color}")
        if sg.get("cooldown_remaining_seconds", 0) > 0:
            body.append(f"Cooldown Remaining:   {sg.get('cooldown_remaining_seconds'):.1f}s\n", style="bold red")
        body.append(f"Searches Last Minute: {sg.get('search_requests_last_minute', 0)} / 5 (Capacity Limit)\n")
        body.append(f"In-Flight Searches:   {sg.get('search_in_flight', 0)} (Concurrency Max: 1)\n")
        body.append(f"Queued Searches:      {sg.get('search_queued', 0)} / 4 (Queue Limit)\n")
        body.append(f"Total Searches:       {sg.get('total_searches', 0)} (Success: {sg.get('successful_searches', 0)}, Failed: {sg.get('failed_searches', 0)})\n")
        body.append(f"Total Page Fetches:   {sg.get('total_fetches', 0)} (Success: {sg.get('successful_fetches', 0)}, Failed: {sg.get('failed_fetches', 0)})\n")
        body.append(f"Cache Performance:    {sg.get('cache_hits', 0)} Hits / {sg.get('cache_misses', 0)} Misses ({sg.get('cache_entries', 0)} cached entries)\n")
        body.append(f"Deduplicated Calls:   {sg.get('deduplicated_requests', 0)} duplicate requests collapsed in-flight\n")
        if sg.get("last_error_category"):
            body.append(f"Last Error Category:  {sg.get('last_error_category')}\n", style="red")
        body.append("\n")

        # 3. MCP Subsystem Card
        body.append("═══ 3. MCP PROCESS SUPERVISION ═══\n", style="bold green")
        total_mcp = len(self._snapshot.mcp_servers)
        running_mcp = sum(1 for m in self._snapshot.mcp_servers if m.process_alive)
        body.append(f"Configured Servers:   {total_mcp}\n")
        body.append(f"Running Processes:    {running_mcp}\n")
        for m in self._snapshot.mcp_servers:
            p_str = f"PID {m.process_id}" if m.process_alive else "STOPPED"
            j_str = "Job Object Enforced" if m.in_job_object else "No Job"
            body.append(f"  • {m.server_id} ({m.name}): {p_str} | {j_str} | Restarts: {m.restarts} | Tools: {m.approved_tool_count}/{m.discovered_tool_count}\n")
        body.append("\n")

        # 4. Memory Budget Assessment
        body.append("═══ 4. PERFORMANCE & RESOURCE EVALUATION ═══\n", style="bold magenta")
        body.append("Target Ceiling:       500.0 MB peak memory\n")
        total_tracked_mb = self._snapshot.memory_working_set_mb
        body.append(f"Current Working Set:  {total_tracked_mb:.1f} MB ({(total_tracked_mb / 500.0) * 100:.1f}% of budget)\n", style="green" if total_tracked_mb < 300 else "yellow")
        body.append("Monitoring Overhead:  < 20 MB bounded telemetry & cache\n")

        content_widget.update(body)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "health-refresh-btn":
            self.refresh_health()
        elif event.button.id == "health-reset-circuit-btn":
            self.service.reset_search_circuit("duckduckgo")
            self.refresh_health()
