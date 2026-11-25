"""Right-Side Activity Panel Widget
Displays continuous runtime telemetry, active tasks, MCP summary, search circuit status,
and recent event ticker across all major views.
"""

from typing import Optional
from rich.text import Text
from textual.widgets import Static
from textual.containers import VerticalScroll

from runtime.observability.models import RuntimeSnapshot


class ActivityPanel(Static):
    """Compact right-side runtime telemetry panel."""

    def compose(self):
        yield Static("RUNTIME STATUS", classes="panel-header")
        yield Static(id="activity-runtime-info")
        yield Static("ACTIVE WORKLOAD", classes="panel-header")
        yield Static(id="activity-workload-info")
        yield Static("MCP STATUS", classes="panel-header")
        yield Static(id="activity-mcp-info")
        yield Static("SEARCH GOVERNOR", classes="panel-header")
        yield Static(id="activity-search-info")
        yield Static("RECENT EVENTS", classes="panel-header")
        yield VerticalScroll(Static(id="activity-recent-events"), id="activity-events-scroll")

    def update_snapshot(self, snapshot: RuntimeSnapshot) -> None:
        """Refreshes panel content from a RuntimeSnapshot."""
        # 1. Runtime Info
        rt_text = Text()
        state_color = "green" if snapshot.state == "RUNNING" else "yellow"
        rt_text.append(f"State:   ", style="bold")
        rt_text.append(f"● {snapshot.state}\n", style=f"bold {state_color}")
        rt_text.append(f"PID:     {snapshot.pid}\n")
        uptime_min = int(snapshot.uptime_seconds // 60)
        uptime_sec = int(snapshot.uptime_seconds % 60)
        rt_text.append(f"Uptime:  {uptime_min}m {uptime_sec}s\n")
        rt_text.append(f"Memory:  {snapshot.memory_working_set_mb:.1f} MB\n")
        rt_text.append(f"CPU:     {snapshot.cpu_percent:.1f}%\n")
        rt_text.append(f"Threads: {snapshot.thread_count}")
        self.query_one("#activity-runtime-info", Static).update(rt_text)

        # 2. Workload Info
        wl_text = Text()
        wl_text.append(f"Active Tasks:    {snapshot.active_tasks_count}\n", style="bold cyan")
        wl_text.append(f"Active Sessions: {snapshot.active_sessions_count}\n")
        wl_text.append(f"Events Streamed: {snapshot.total_events_published}")
        self.query_one("#activity-workload-info", Static).update(wl_text)

        # 3. MCP Info
        mcp_text = Text()
        running_mcp = sum(1 for m in snapshot.mcp_servers if m.process_alive)
        total_mcp = len(snapshot.mcp_servers)
        total_tools = sum(m.approved_tool_count for m in snapshot.mcp_servers)
        mcp_text.append(f"Servers: {running_mcp}/{total_mcp} Running\n")
        mcp_text.append(f"Approved Tools: {total_tools}")
        self.query_one("#activity-mcp-info", Static).update(mcp_text)

        # 4. Search Governor Info
        sg_text = Text()
        search_data = snapshot.search_telemetry or {}
        circuit_state = search_data.get("circuit_state", "CLOSED")
        c_color = "green" if circuit_state == "CLOSED" else ("yellow" if circuit_state == "HALF_OPEN" else "red")
        sg_text.append(f"Circuit:  ", style="bold")
        sg_text.append(f"[{circuit_state}]\n", style=f"bold {c_color}")
        cooldown = search_data.get("cooldown_remaining_seconds", 0)
        if cooldown > 0:
            sg_text.append(f"Cooldown: {cooldown:.1f}s\n", style="red")
        sg_text.append(f"In-Flight Searches: {search_data.get('search_in_flight', 0)}\n")
        sg_text.append(f"Cache Hits: {search_data.get('cache_hits', 0)} / {search_data.get('cache_misses', 0)}")
        self.query_one("#activity-search-info", Static).update(sg_text)

        # 5. Recent Events
        ev_text = Text()
        events = snapshot.recent_events[-6:] if snapshot.recent_events else []
        if not events:
            ev_text.append("No recent events.", style="dim")
        else:
            for ev in reversed(events):
                time_str = ev.event_type.split(".")[-1]
                style = "red" if ev.level == "ERROR" else ("yellow" if ev.level == "WARNING" else "white")
                ev_text.append(f"• {ev.event_type}\n", style=f"bold {style}")
                if ev.message:
                    ev_text.append(f"  {ev.message[:32]}\n", style="dim")
        self.query_one("#activity-recent-events", Static).update(ev_text)
