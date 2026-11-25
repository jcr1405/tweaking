"""Proactive Environmental Briefing Service
Synthesizes hardware sensor data, active desktop foreground window state,
and learned behavioral routines into situational greetings and morning/startup briefings.
"""

import time
import logging
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field

from runtime.proactive.sensors import SystemSensors, SystemHealthSnapshot
from runtime.desktop.awareness import ScreenAwarenessMonitor, DesktopStateSnapshot
from runtime.memory.store import BehavioralMemoryStore

logger = logging.getLogger("presence.proactive.briefing")


@dataclass
class BriefingSummary:
    """Structured situational companion briefing."""
    timestamp: float
    greeting: str
    health_snapshot: SystemHealthSnapshot
    desktop_state: Optional[DesktopStateSnapshot]
    learned_routines_count: int
    briefing_text: str
    active_warnings: List[str] = field(default_factory=list)


class StartupBriefingService:
    """Coordinates environmental sensors and desktop memory into proactive briefings."""

    def __init__(
        self,
        memory_store: Optional[BehavioralMemoryStore] = None,
        awareness_monitor: Optional[ScreenAwarenessMonitor] = None,
    ):
        self.memory_store = memory_store or BehavioralMemoryStore()
        self.awareness_monitor = awareness_monitor or ScreenAwarenessMonitor()

    def generate_briefing(self) -> BriefingSummary:
        """Constructs an integrated situational environmental briefing."""
        now = time.time()
        health = SystemSensors.sample()
        desktop = self.awareness_monitor.sample_current_state(capture_frame=False)
        routines = self.memory_store.list_routines()

        # Determine time-of-day greeting
        hour = time.localtime(now).tm_hour
        if hour < 12:
            time_greeting = "Good morning"
        elif hour < 18:
            time_greeting = "Good afternoon"
        else:
            time_greeting = "Good evening"

        # Formulate companion narrative
        parts = []
        parts.append(f"{time_greeting}! I am your personal computer companion, running 100% locally on Qwen 3.5 9B.")

        # System health narrative
        if health.is_healthy:
            parts.append(
                f"Your PC is running smoothly (CPU: {health.cpu_percent:.0f}%, "
                f"RAM: {health.ram_available_gb:.1f} GB free of {health.ram_total_gb:.1f} GB, "
                f"Disk: {health.disk_free_gb:.1f} GB free)."
            )
        else:
            parts.append(f"Attention needed: {'; '.join(health.warnings)}.")

        # Foreground application awareness
        if desktop and desktop.process_name and desktop.process_name != "none":
            if desktop.is_sensitive:
                parts.append("You are currently focused on a sensitive window; screen observation is automatically masked for your privacy.")
            else:
                title_desc = f" ({desktop.window_title})" if desktop.window_title else ""
                parts.append(f"You are currently working in {desktop.process_name}{title_desc}.")

        # Learned routines
        if routines:
            routine_names = ", ".join(r.name for r in routines[:3])
            parts.append(f"I have {len(routines)} personal routine(s) ready: {routine_names}.")

        briefing_text = " ".join(parts)

        return BriefingSummary(
            timestamp=now,
            greeting=time_greeting,
            health_snapshot=health,
            desktop_state=desktop,
            learned_routines_count=len(routines),
            briefing_text=briefing_text,
            active_warnings=health.warnings,
        )
